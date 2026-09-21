#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "playwright==1.55.0",
# ]
#
# [tool.ty.rules]
# unresolved-import = "ignore"
# ///
"""Browser contracts for both MCP Apps under the compact tool profile."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "server" / "ui" / "pipeline-runs" / "index.html"
CHART = ROOT / "server" / "ui" / "run-activity-chart" / "index.html"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

FAKE_APP_MODULE = """
export class App {
  constructor() { window.__mcpApp = this; }
  async connect() {}
  async callServerTool(request) {
    window.__toolCalls.push(request);
    return window.__toolHandler(request);
  }
}
"""


async def _new_page(browser: Browser, handler_source: str) -> Page:
    page = await browser.new_page()
    await page.route(
        "https://unpkg.com/**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/javascript",
            body=FAKE_APP_MODULE,
        ),
    )
    await page.add_init_script(
        "window.__toolCalls = []; window.__toolHandler = " + handler_source
    )
    return page


async def _calls(page: Page) -> list[dict[str, Any]]:
    return await page.evaluate("window.__toolCalls")


async def test_dashboard(browser: Browser) -> None:
    handler = """async (request) => {
      if (request.name === "zenml_list_resources" && request.arguments.resource_type === "pipeline_run") {
        return {structuredContent: {items: [{
          id: "run-12345678", name: "compact-run",
          body: {status: "completed", created: "2026-09-12T10:00:00Z"},
          resources: {pipeline: {name: "training"}, stack: {name: "local"}}
        }], total: 51, page: request.arguments.page, size: request.arguments.size}};
      }
      if (request.name === "zenml_list_resources" && request.arguments.resource_type === "run_step") {
        return {structuredContent: {items: [{id: "step-12345678", name: "train", body: {status: "completed"}}], total: 1, page: 1, size: 200}};
      }
      if (request.name === "get_step_logs") {
        return {structuredContent: {logs: [{message: "browser-log-marker"}]}};
      }
      throw new Error(`unexpected tool ${request.name}`);
    }"""
    page = await _new_page(browser, handler)
    await page.goto(DASHBOARD.as_uri())
    await page.locator(".run-name", has_text="compact-run").wait_for()
    calls = await _calls(page)
    assert calls[0] == {
        "name": "zenml_list_resources",
        "arguments": {
            "resource_type": "pipeline_run",
            "filters": {"sort_by": "desc:created"},
            "page": 1,
            "size": 25,
        },
    }

    await page.locator("#pipelineFilter").fill("training")
    await page.wait_for_timeout(500)
    await page.locator("#statusFilter").select_option("completed")
    await page.wait_for_timeout(100)
    calls = await _calls(page)
    assert calls[-1]["arguments"]["filters"] == {
        "sort_by": "desc:created",
        "pipeline_name": "training",
        "status": "completed",
    }

    await page.locator("#nextBtn").click()
    await page.wait_for_timeout(100)
    assert (await _calls(page))[-1]["arguments"]["page"] == 2

    await page.locator(".expand-btn").click()
    await page.locator(".step-item", has_text="train").wait_for()
    step_call = (await _calls(page))[-1]
    assert step_call == {
        "name": "zenml_list_resources",
        "arguments": {
            "resource_type": "run_step",
            "filters": {"pipeline_run_id": "run-12345678"},
            "size": 200,
        },
    }
    await page.locator(".step-item", has_text="train").click()
    await page.locator(".logs-content", has_text="browser-log-marker").wait_for()
    assert (await _calls(page))[-1] == {
        "name": "get_step_logs",
        "arguments": {"step_run_id": "step-12345678"},
    }
    await page.close()


async def test_chart(browser: Browser) -> None:
    handler = """async (request) => {
      if (request.name !== "zenml_list_resources") throw new Error("legacy tool call");
      const page = request.arguments.page;
      const makeRun = (index) => ({
        id: `run-${page}-${index}`,
        body: {status: index % 2 ? "failed" : "completed", created: new Date().toISOString()}
      });
      const oldRun = {id: "old-run", body: {status: "running", created: "2000-01-01T00:00:00Z"}};
      const items = page === 1 ? Array.from({length: 100}, (_, i) => makeRun(i)) : [makeRun(100), oldRun];
      return {structuredContent: {items, total: 102, page, size: 100}};
    }"""
    page = await _new_page(browser, handler)
    await page.goto(CHART.as_uri())
    await page.locator("#totalCount", has_text="101").wait_for()
    calls = await _calls(page)
    assert [call["arguments"]["page"] for call in calls] == [1, 2]
    assert all(
        call
        == {
            "name": "zenml_list_resources",
            "arguments": {
                "resource_type": "pipeline_run",
                "filters": {"sort_by": "desc:created"},
                "size": 100,
                "page": index,
            },
        }
        for index, call in enumerate(calls, start=1)
    )
    assert await page.locator('rect[fill="var(--color-success-500)"]').count() == 1
    assert await page.locator('rect[fill="var(--color-error-500)"]').count() == 1
    assert await page.locator('rect[fill="var(--color-warning-500)"]').count() == 0
    await page.close()


async def test_visible_errors(browser: Browser) -> None:
    error = {
        "error": {
            "tool": "zenml_list_resources",
            "message": "Sanitized server error marker",
            "type": "UnexpectedError",
        }
    }
    handler = f"async () => ({{structuredContent: {json.dumps(error)}}})"
    for path in (DASHBOARD, CHART):
        page = await _new_page(browser, handler)
        await page.goto(path.as_uri())
        await page.get_by_text("Sanitized server error marker").wait_for()
        await page.close()


async def main() -> int:
    async with async_playwright() as playwright:
        executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if executable is None and CHROME.exists():
            executable = str(CHROME)
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=executable,
        )
        try:
            await test_dashboard(browser)
            print("PASS: compact dashboard browser flow")
            await test_chart(browser)
            print("PASS: compact activity chart browser flow")
            await test_visible_errors(browser)
            print("PASS: sanitized App errors are visible")
        finally:
            await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
