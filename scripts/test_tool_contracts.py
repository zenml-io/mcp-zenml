#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "mcp[cli]",
#     "zenml~=0.93.0",
#     "setuptools",
#     "requests>=2.32.0",
# ]
#
# [tool.ty.rules]
# unresolved-import = "ignore"
#
# [tool.ty.environment]
# extra-paths = ["../server", "."]
# ///
"""Credential-free characterization tests for the public MCP contract."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyUrl

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SERVER_PATH = REPO_ROOT / "server" / "zenml_server.py"
CONTRACT_PATH = SCRIPT_DIR / "fixtures" / "legacy_tool_schemas.json"

sys.path.insert(0, str(REPO_ROOT / "server"))
sys.path.insert(0, str(SCRIPT_DIR))

os.environ.setdefault("ZENML_MCP_ANALYTICS_ENABLED", "false")

import zenml_server as server  # noqa: E402
from test_mcp_server import (  # noqa: E402
    _detect_tool_error,
    _extract_call_tool_output,
    _profile_inventory_errors,
)


def _normalize_schema(value: Any) -> Any:
    """Keep semantic JSON Schema fields while ignoring generated titles."""
    if isinstance(value, dict):
        normalized = {
            key: _normalize_schema(item)
            for key, item in value.items()
            if key not in {"description", "title"}
        }
        any_of = normalized.get("anyOf")
        if isinstance(any_of, list) and all(
            isinstance(item, dict) and set(item) == {"type"} for item in any_of
        ):
            normalized.pop("anyOf")
            normalized["type"] = sorted(
                (item["type"] for item in any_of), key=lambda item: item == "null"
            )
        return normalized
    if isinstance(value, list):
        return [_normalize_schema(item) for item in value]
    return value


def _structured_payload(result: Any) -> dict[str, Any]:
    """Extract a successful structured tool result across MCP field spellings."""
    is_error = getattr(result, "isError", None)
    if is_error is None:
        is_error = getattr(result, "is_error", False)
    assert not is_error, result

    kind, payload = _extract_call_tool_output(result)
    assert _detect_tool_error("contract_test", kind, payload) is None
    assert kind == "structured", result
    assert isinstance(payload, dict), result
    return payload


class FakeResponse:
    """Minimal ZenML response object used behind the real MCP protocol."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return self.payload


class FakePipelineResponse(FakeResponse):
    def __init__(self) -> None:
        super().__init__({"id": "pipeline-1", "name": "training"})
        self.runs = [type("Run", (), {"status": "completed"})()]


class FakeZenMLClient:
    """Small fake covering the legacy shapes that differ from plain get/list."""

    def __init__(self) -> None:
        self.calls: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}

    def _record(
        self, call_name: str, payload: dict[str, Any], *args: Any, **kwargs: Any
    ) -> FakeResponse:
        self.calls[call_name] = (args, kwargs)
        return FakeResponse(payload)

    def list_pipelines(self, **kwargs: Any) -> FakeResponse:
        return self._record(
            "list_pipelines",
            {"items": [{"id": "pipeline-1"}], "total": 1, "page": 1, "size": 20},
            **kwargs,
        )

    def get_project(self, *args: Any, **kwargs: Any) -> FakeResponse:
        return self._record("get_project", {"id": "project-1"}, *args, **kwargs)

    def get_pipeline(self, *args: Any, **kwargs: Any) -> FakePipelineResponse:
        self.calls["get_pipeline"] = (args, kwargs)
        return FakePipelineResponse()

    def get_artifact_version(self, *args: Any, **kwargs: Any) -> FakeResponse:
        return self._record(
            "get_artifact_version", {"id": "artifact-version-1"}, *args, **kwargs
        )

    def list_artifact_versions(self, *args: Any, **kwargs: Any) -> FakeResponse:
        return self._record(
            "list_artifact_versions",
            {
                "items": [{"id": "artifact-version-1"}],
                "total": 1,
                "page": 1,
                "size": 10,
            },
            *args,
            **kwargs,
        )

    def get_model_version(self, *args: Any, **kwargs: Any) -> FakeResponse:
        return self._record(
            "get_model_version", {"id": "model-version-1"}, *args, **kwargs
        )

    def list_model_versions(self, *args: Any, **kwargs: Any) -> FakeResponse:
        return self._record(
            "list_model_versions",
            {"items": [{"id": "model-version-1"}], "total": 1, "page": 1, "size": 20},
            *args,
            **kwargs,
        )

    def get_run_template(self, *args: Any, **kwargs: Any) -> FakeResponse:
        return self._record(
            "get_run_template", {"id": "run-template-1"}, *args, **kwargs
        )

    def list_run_templates(self, *args: Any, **kwargs: Any) -> FakeResponse:
        return self._record(
            "list_run_templates",
            {"items": [{"id": "run-template-1"}], "total": 1, "page": 1, "size": 20},
            *args,
            **kwargs,
        )


async def test_legacy_inventory_and_schemas() -> None:
    """The complete legacy inventory and semantic input schemas stay stable."""
    expected = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.list_tools()

    actual_names = [tool.name for tool in result.tools]
    actual_schemas = {
        tool.name: _normalize_schema(tool.inputSchema) for tool in result.tools
    }
    assert actual_names == expected["tool_names"]
    assert actual_schemas == expected["input_schemas"]

    missing = set(actual_names)
    missing.remove("get_pipeline_details")
    errors = _profile_inventory_errors("legacy", missing)
    assert errors and "get_pipeline_details" in errors[0]


async def test_discovery_does_not_initialize_zenml_client() -> None:
    """Missing credentials still allow discovery and setup diagnostics."""
    credential_names = (
        "ZENML_STORE_URL",
        "ZENML_STORE_API_KEY",
        "ZENML_ACTIVE_PROJECT_ID",
    )
    original_credentials = {
        name: os.environ.pop(name) for name in credential_names if name in os.environ
    }
    original_get_client = server.get_zenml_client

    def fail_if_initialized() -> Any:
        raise AssertionError("ZenML client initialization was attempted")

    setattr(server, "get_zenml_client", fail_if_initialized)
    try:
        async with create_connected_server_and_client_session(server.mcp) as session:
            tools = await session.list_tools()
            assert tools.tools
            diagnostics = _structured_payload(
                await session.call_tool("diagnose_zenml_setup", {})
            )
            assert "missing_store_url" in {
                issue["code"] for issue in diagnostics["issues"]
            }
    finally:
        setattr(server, "get_zenml_client", original_get_client)
        os.environ.update(original_credentials)


async def test_fake_sdk_success_shapes() -> None:
    """Representative legacy envelopes survive a real MCP call chain."""
    fake_client = FakeZenMLClient()
    original_client = server.zenml_client
    server.zenml_client = fake_client
    try:
        async with create_connected_server_and_client_session(server.mcp) as session:
            list_payload = _structured_payload(
                await session.call_tool("list_pipelines", {})
            )
            assert list_payload == {
                "items": [{"id": "pipeline-1"}],
                "total": 1,
                "page": 1,
                "size": 20,
            }

            get_payload = _structured_payload(
                await session.call_tool("get_project", {"name_id_or_prefix": "p"})
            )
            assert get_payload == {"id": "project-1"}

            pipeline_payload = _structured_payload(
                await session.call_tool(
                    "get_pipeline_details", {"name_id_or_prefix": "training"}
                )
            )
            assert pipeline_payload == {
                "pipeline": {"id": "pipeline-1", "name": "training"},
                "latest_runs_status": ["completed"],
                "num_runs": 5,
            }

            assert _structured_payload(
                await session.call_tool(
                    "get_artifact_version", {"name_id_or_prefix": "dataset"}
                )
            ) == {"id": "artifact-version-1"}
            assert _structured_payload(
                await session.call_tool(
                    "list_artifact_versions", {"artifact_name_or_id": "dataset"}
                )
            )["items"] == [{"id": "artifact-version-1"}]
            assert _structured_payload(
                await session.call_tool(
                    "get_model_version",
                    {
                        "model_name_or_id": "classifier",
                        "model_version_name_or_number_or_id": "1",
                    },
                )
            ) == {"id": "model-version-1"}
            assert _structured_payload(
                await session.call_tool(
                    "list_model_versions", {"model_name_or_id": "classifier"}
                )
            )["items"] == [{"id": "model-version-1"}]

            template = _structured_payload(
                await session.call_tool(
                    "get_run_template", {"name_id_or_prefix": "daily"}
                )
            )
            assert set(template) == {"deprecation_notice", "run_template"}
            assert template["run_template"] == {"id": "run-template-1"}
            templates = _structured_payload(
                await session.call_tool("list_run_templates", {})
            )
            assert set(templates) == {"deprecation_notice", "run_templates"}
            assert templates["run_templates"]["items"] == [{"id": "run-template-1"}]

        assert fake_client.calls["get_artifact_version"][1] == {
            "name_id_or_prefix": "dataset",
            "version": None,
        }
        assert fake_client.calls["list_artifact_versions"][1]["artifact"] == "dataset"
        assert fake_client.calls["get_model_version"][0] == ("classifier", "1")
        assert fake_client.calls["list_model_versions"][0] == ("classifier",)
    finally:
        server.zenml_client = original_client


async def test_stdio_prompts_resources_apps_without_credentials() -> None:
    """Discovery, diagnostics, prompts, resources and Apps round-trip over stdio."""
    clean_env = dict(os.environ)
    for name in (
        "ZENML_STORE_URL",
        "ZENML_STORE_API_KEY",
        "ZENML_ACTIVE_PROJECT_ID",
    ):
        clean_env.pop(name, None)

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PATH)],
        env=clean_env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            expected = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
            assert [tool.name for tool in tools.tools] == expected["tool_names"]

            tool_meta = {tool.name: tool.meta for tool in tools.tools}
            assert tool_meta["open_pipeline_run_dashboard"] == {
                "ui": {"resourceUri": server.DASHBOARD_UI_URI}
            }
            assert tool_meta["open_run_activity_chart"] == {
                "ui": {"resourceUri": server.CHART_UI_URI}
            }

            prompts = await session.list_prompts()
            assert [prompt.name for prompt in prompts.prompts] == [
                "stack_components_analysis",
                "recent_runs_analysis",
            ]
            prompt = await session.get_prompt("recent_runs_analysis")
            assert prompt.messages and "recent runs" in str(prompt.messages[0].content)

            resources = await session.list_resources()
            resource_by_uri = {str(item.uri): item for item in resources.resources}
            assert set(resource_by_uri) == {
                server.DASHBOARD_UI_URI,
                server.CHART_UI_URI,
                "resource://zenml_server/apps",
            }
            assert resource_by_uri[server.DASHBOARD_UI_URI].mimeType == (
                "text/html;profile=mcp-app"
            )
            assert resource_by_uri[server.DASHBOARD_UI_URI].meta == {
                "ui": {"csp": {"resourceDomains": ["https://unpkg.com"]}}
            }

            templates = await session.list_resource_templates()
            assert [str(item.uriTemplate) for item in templates.resourceTemplates] == [
                "resource://zenml_server/most_recent_runs?run_count={run_count}"
            ]

            app_manifest = await session.read_resource(
                AnyUrl("resource://zenml_server/apps")
            )
            manifest = json.loads(app_manifest.contents[0].text)
            assert [app["entry"] for app in manifest["apps"]] == [
                server.DASHBOARD_UI_URI,
                server.CHART_UI_URI,
            ]
            dashboard = await session.read_resource(AnyUrl(server.DASHBOARD_UI_URI))
            assert "<!DOCTYPE html>" in dashboard.contents[0].text

            diagnostics = _structured_payload(
                await session.call_tool("diagnose_zenml_setup", {})
            )
            assert diagnostics["ok"] is False
            assert "missing_store_url" in {
                issue["code"] for issue in diagnostics["issues"]
            }


async def main() -> int:
    tests: list[tuple[str, Callable[[], Any]]] = [
        ("test_legacy_inventory_and_schemas", test_legacy_inventory_and_schemas),
        (
            "test_discovery_does_not_initialize_zenml_client",
            test_discovery_does_not_initialize_zenml_client,
        ),
        ("test_fake_sdk_success_shapes", test_fake_sdk_success_shapes),
        (
            "test_stdio_prompts_resources_apps_without_credentials",
            test_stdio_prompts_resources_apps_without_credentials,
        ),
    ]
    for name, test in tests:
        await test()
        print(f"PASS: {name}")
    print(f"All {len(tests)} tool contract tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
