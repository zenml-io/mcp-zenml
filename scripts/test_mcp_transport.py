#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "mcp[cli]==2.2.0",
#     "zenml==0.96.4",
#     "setuptools",
#     "requests>=2.32.0",
# ]
#
# [tool.uv]
# exclude-newer-package = { mcp = "2026-09-08T00:00:00Z", "mcp-types" = "2026-09-08T00:00:00Z" }
#
# [tool.ty.rules]
# unresolved-import = "ignore"
#
# [tool.ty.environment]
# extra-paths = ["../server"]
# ///
"""Credential-free checks for the MCP 2.2 runtime and HTTP transport."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from mcp import Client
from mcp.types import Implementation

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("ZENML_MCP_ANALYTICS_ENABLED", "false")

import zenml_mcp_analytics as analytics  # noqa: E402
import zenml_server as server  # noqa: E402


async def test_legacy_and_current_client_negotiation() -> None:
    """Handshake and modern auto-negotiation both discover and invoke tools."""
    async with Client(server.mcp, mode="legacy") as legacy:
        tools = await legacy.list_tools()
        assert any(tool.name == "diagnose_zenml_setup" for tool in tools.tools)
        result = await legacy.call_tool("diagnose_zenml_setup", {})
        assert result.is_error is False

    async with Client(
        server.mcp,
        mode="auto",
        client_info=Implementation(name="transport-test", version="1"),
    ) as current:
        tools = await current.list_tools()
        assert any(tool.name == "diagnose_zenml_setup" for tool in tools.tools)
        result = await current.call_tool("diagnose_zenml_setup", {})
        assert result.is_error is False


async def test_context_schema_and_request_attribution() -> None:
    """Injected Context stays private and analytics uses each request's client."""
    tools = await server.mcp.list_tools()
    assert all("ctx" not in tool.input_schema.get("properties", {}) for tool in tools)

    recorded: list[tuple[str | None, str | None]] = []
    recorded_lock = threading.Lock()

    def record_tool_call(**properties: Any) -> None:
        with recorded_lock:
            recorded.append(
                (
                    properties.get("mcp_client_name"),
                    properties.get("mcp_client_version"),
                )
            )

    async def invoke(name: str, version: str) -> None:
        async with Client(
            server.mcp,
            mode="auto",
            client_info=Implementation(name=name, version=version),
        ) as client:
            result = await client.call_tool("diagnose_zenml_setup", {})
            assert result.is_error is False

    with patch.object(server.analytics, "track_tool_call", record_tool_call):
        await asyncio.gather(invoke("client-a", "1"), invoke("client-b", "2"))

    assert sorted(recorded) == [("client-a", "1"), ("client-b", "2")]


def test_singleton_initialization_and_zero_retry_session() -> None:
    """Concurrent first access initializes once and disables REST retries."""

    class FakeStore:
        def __init__(self) -> None:
            self.config = type("Config", (), {"connection_pool_size": 7})()
            self.session = __import__("requests").Session()

    class FakeClient:
        init_count = 0

        def __init__(self) -> None:
            type(self).init_count += 1
            time.sleep(0.05)
            self.zen_store = FakeStore()

    original_client = server.zenml_client
    original_failure = server._client_init_failure_reported
    server.zenml_client = None
    server._client_init_failure_reported = False
    try:
        with patch("zenml.client.Client", FakeClient):
            results: list[Any] = []

            def initialize() -> None:
                results.append(server.get_zenml_client())

            threads = [threading.Thread(target=initialize) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert FakeClient.init_count == 1
        assert len({id(item) for item in results}) == 1
        rest_session = results[0].zen_store.session
        for scheme in ("http://", "https://"):
            retries = rest_session.adapters[scheme].max_retries
            assert retries.total == 0
            assert retries.connect == 0
            assert retries.read == 0
    finally:
        server.zenml_client = original_client
        server._client_init_failure_reported = original_failure


async def test_http_security_and_lifespan() -> None:
    """Host/Origin checks and session-manager shutdown survive the v2 runner."""
    config = server.HTTPTransportConfig(
        host="127.0.0.1",
        port=8000,
        forwarded_allow_ips="10.0.0.0/8",
    )
    security = server.create_transport_security_settings(config)
    assert security.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in security.allowed_hosts
    assert config.forwarded_allow_ips == "10.0.0.0/8"

    disabled = server.HTTPTransportConfig(
        host=config.host,
        port=config.port,
        disable_dns_rebinding_protection=True,
        forwarded_allow_ips=config.forwarded_allow_ips,
    )
    assert (
        server.create_transport_security_settings(
            disabled
        ).enable_dns_rebinding_protection
        is False
    )
    assert disabled.forwarded_allow_ips == config.forwarded_allow_ips

    app = server.create_streamable_http_app(config)
    manager = server.mcp.session_manager
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            valid = await client.post(
                "/mcp",
                headers={
                    "host": "127.0.0.1:8000",
                    "origin": "http://127.0.0.1:8000",
                },
                json={},
            )
            assert valid.status_code not in {403, 421}

            bad_host = await client.post(
                "/mcp", headers={"host": "attacker.invalid"}, json={}
            )
            assert bad_host.status_code == 421

            bad_origin = await client.post(
                "/mcp",
                headers={
                    "host": "127.0.0.1:8000",
                    "origin": "https://attacker.invalid",
                },
                json={},
            )
            assert bad_origin.status_code == 403

        assert manager._task_group is not None

    assert manager._task_group is None
    assert manager._lifespan_state is None
    assert manager._server_instances == {}


async def test_sanitized_tool_error_and_worker_thread() -> None:
    """Failures are MCP execution errors and sync work does not block the loop."""
    from mcp.server.mcpserver import MCPServer

    isolated = MCPServer("runtime-test")
    worker_started = threading.Event()
    worker_finished = threading.Event()

    @isolated.tool()
    @server.handle_tool_exceptions
    def fail_after_work() -> dict[str, Any]:
        worker_started.set()
        time.sleep(0.08)
        worker_finished.set()
        raise RuntimeError("credential=super-secret")

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while not worker_finished.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    async with Client(isolated, mode="auto") as client:
        call = asyncio.create_task(client.call_tool("fail_after_work", {}))
        beat = asyncio.create_task(heartbeat())
        await asyncio.to_thread(worker_started.wait, 1)
        result = await call
        await beat

    assert ticks >= 2
    assert result.is_error is True
    assert result.structured_content is not None
    error = result.structured_content["error"]
    assert error["type"] == "UnexpectedError"
    assert "super-secret" not in str(result.model_dump(mode="json"))


def test_analytics_metadata_allowlist() -> None:
    """Telemetry drops arbitrary and structured values before transmission."""
    sent: list[dict[str, Any]] = []
    with (
        patch.object(analytics, "_ensure_initialized", return_value=True),
        patch.object(
            analytics, "_send_events", side_effect=lambda events: sent.extend(events)
        ),
        patch.object(analytics, "DEV_MODE", False),
        patch.object(analytics, "_session_id", "test-session"),
        patch.object(
            analytics,
            "_session_properties",
            {"mcp_client_name": "session-client", "transport": "stdio"},
        ),
    ):
        analytics.track_event(
            "Tool Called",
            {
                "mcp_client_name": "request-client",
                "resource_type": "pipeline_run",
                "credential": "super-secret",
                "payload": {"token": "super-secret"},
                "action": {"name": "unsafe-structured-value"},
            },
        )

    assert len(sent) == 1
    properties = sent[0]["properties"]
    assert properties["mcp_client_name"] == "request-client"
    assert properties["resource_type"] == "pipeline_run"
    assert properties["session_id"] == "test-session"
    assert "credential" not in properties
    assert "payload" not in properties
    assert "action" not in properties
    assert "super-secret" not in str(sent)


async def main() -> int:
    tests = [
        test_legacy_and_current_client_negotiation,
        test_context_schema_and_request_attribution,
        test_http_security_and_lifespan,
        test_sanitized_tool_error_and_worker_thread,
    ]
    for test in tests:
        await test()
        print(f"PASS: {test.__name__}")
    test_singleton_initialization_and_zero_retry_session()
    print("PASS: test_singleton_initialization_and_zero_retry_session")
    test_analytics_metadata_allowlist()
    print("PASS: test_analytics_metadata_allowlist")
    print(f"All {len(tests) + 2} MCP runtime tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
