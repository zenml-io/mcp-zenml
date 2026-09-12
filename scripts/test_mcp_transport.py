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
import socket
import subprocess
import sys
import tempfile
import textwrap
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
            self.adapter_ids = {
                scheme: id(adapter) for scheme, adapter in self.session.adapters.items()
            }

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
        original_configure = server._configure_zero_retry_rest_session

        def configure_before_publish(client: Any) -> None:
            assert server.zenml_client is None
            original_configure(client)

        with (
            patch("zenml.client.Client", FakeClient),
            patch.object(
                server,
                "_configure_zero_retry_rest_session",
                side_effect=configure_before_publish,
            ),
        ):
            results: list[Any] = []
            failures: list[BaseException] = []

            def initialize() -> None:
                try:
                    results.append(server.get_zenml_client())
                except BaseException as error:
                    failures.append(error)

            threads = [threading.Thread(target=initialize) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert FakeClient.init_count == 1
        assert failures == []
        assert len(results) == len(threads) == 4
        assert len({id(item) for item in results}) == 1
        rest_session = results[0].zen_store.session
        for scheme in ("http://", "https://"):
            assert (
                id(rest_session.adapters[scheme])
                == results[0].zen_store.adapter_ids[scheme]
            )
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

    for wildcard_host in ("0.0.0.0", "::"):
        wildcard = server.HTTPTransportConfig(host=wildcard_host)
        try:
            server.create_transport_security_settings(wildcard)
        except ValueError as error:
            message = str(error)
            assert wildcard_host in message
            assert "concrete host" in message
            assert "--disable-dns-rebinding-protection" in message
        else:
            raise AssertionError(
                f"protected wildcard bind {wildcard_host!r} was accepted"
            )

        wildcard_disabled = server.HTTPTransportConfig(
            host=wildcard_host,
            disable_dns_rebinding_protection=True,
        )
        assert (
            server.create_transport_security_settings(
                wildcard_disabled
            ).enable_dns_rebinding_protection
            is False
        )

    app = server.create_streamable_http_app(config)
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


async def _wait_for_http_mcp_server(
    process: subprocess.Popen[str], port: int, expected_tool: str, label: str
) -> None:
    """Wait until the spawned process answers as the expected MCP server."""
    deadline = time.monotonic() + 15
    url = f"http://127.0.0.1:{port}/mcp"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"{label} exited early ({process.returncode}): {stdout}{stderr}"
            )
        try:
            async with Client(url) as client:
                tools = await client.list_tools()
            if any(tool.name == expected_tool for tool in tools.tools):
                return
        except Exception:
            await asyncio.sleep(0.05)
    raise AssertionError(f"{label} did not answer as the expected MCP server")


async def test_real_localhost_http_session() -> None:
    """A subprocess HTTP server completes a real MCP handshake and discovery."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"ZENML_STORE_URL", "ZENML_STORE_API_KEY"}
    }
    env.update(
        {
            "ZENML_MCP_ANALYTICS_ENABLED": "false",
            "ZENML_MCP_PROFILE": "compact",
            "ZENML_MCP_WRITE_POLICY": "read_write",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "server" / "zenml_server.py"),
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        await _wait_for_http_mcp_server(
            process, port, "diagnose_zenml_setup", "HTTP server"
        )

        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            tools = await client.list_tools()
            names = [tool.name for tool in tools.tools]
            assert names[0] == "diagnose_zenml_setup"
            assert len(names) == 16
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


async def test_timeout_and_cancellation_outcomes() -> None:
    """HTTP timeout/cancellation do not retry or stop dispatched sync work."""
    with tempfile.TemporaryDirectory(prefix="mcp-transport-") as temp_dir:
        root = Path(temp_dir)
        events_path = root / "events.txt"
        release_path = root / "release"
        script_path = root / "blocking_server.py"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        script_path.write_text(
            textwrap.dedent(
                f"""
                import asyncio
                import threading
                import time
                from pathlib import Path

                import uvicorn
                from mcp.server.mcpserver import MCPServer

                events = Path({str(events_path)!r})
                release = Path({str(release_path)!r})
                server = MCPServer("blocking-http-test")

                @server.tool()
                def slow_call(call_id: str, wait_for_release: bool = False):
                    with events.open("a", encoding="utf-8") as stream:
                        stream.write(f"start:{{call_id}}:{{threading.get_ident()}}\\n")
                    if wait_for_release:
                        deadline = time.monotonic() + 5
                        while not release.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        if not release.exists():
                            raise RuntimeError("test did not release blocking worker")
                    else:
                        time.sleep(0.1)
                    with events.open("a", encoding="utf-8") as stream:
                        stream.write(f"complete:{{call_id}}\\n")
                    return {{"completed": True}}

                app = server.streamable_http_app(host="127.0.0.1")
                asyncio.run(uvicorn.Server(uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port={port},
                    log_level="error",
                    lifespan="on",
                )).serve())
                """
            ),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        async def wait_for_event(expected: str, *, prefix: bool = False) -> list[str]:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                lines = (
                    events_path.read_text(encoding="utf-8").splitlines()
                    if events_path.exists()
                    else []
                )
                if (
                    any(line.startswith(expected) for line in lines)
                    if prefix
                    else expected in lines
                ):
                    return lines
                await asyncio.sleep(0.01)
            raise AssertionError(f"HTTP worker did not record {expected!r}")

        try:
            await _wait_for_http_mcp_server(
                process, port, "slow_call", "blocking HTTP server"
            )

            async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                try:
                    await asyncio.wait_for(
                        client.call_tool("slow_call", {"call_id": "timeout"}),
                        timeout=0.02,
                    )
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("HTTP call did not time out")
                await wait_for_event("complete:timeout")
                await asyncio.sleep(0.5)
                timeout_lines = events_path.read_text(encoding="utf-8").splitlines()
            assert sum(line.startswith("start:timeout:") for line in timeout_lines) == 1
            timeout_thread = next(
                line.rsplit(":", 1)[1]
                for line in timeout_lines
                if line.startswith("start:timeout:")
            )
            assert timeout_thread != str(threading.get_ident())

            async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                call = asyncio.create_task(
                    client.call_tool(
                        "slow_call",
                        {"call_id": "cancel", "wait_for_release": True},
                    )
                )
                await wait_for_event("start:cancel:", prefix=True)
                call.cancel()
                try:
                    await call
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("cancelled HTTP call returned normally")
                release_path.touch()
                await wait_for_event("complete:cancel")
                await asyncio.sleep(0.5)
                cancel_lines = events_path.read_text(encoding="utf-8").splitlines()
            assert sum(line.startswith("start:cancel:") for line in cancel_lines) == 1
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


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


def test_dev_analytics_shutdown_returns_cleanly() -> None:
    """The dev-mode early return must not reference uninitialized cleanup state."""
    with (
        patch.object(analytics, "ANALYTICS_ENABLED", True),
        patch.object(analytics, "DEV_MODE", True),
        patch.object(analytics, "_session_start_time", time.time()),
        patch.object(analytics, "_shutdown_once", threading.Event()),
    ):
        analytics._on_shutdown()


async def main() -> int:
    tests = [
        test_legacy_and_current_client_negotiation,
        test_context_schema_and_request_attribution,
        test_http_security_and_lifespan,
        test_sanitized_tool_error_and_worker_thread,
        test_real_localhost_http_session,
        test_timeout_and_cancellation_outcomes,
    ]
    for test in tests:
        await test()
        print(f"PASS: {test.__name__}")
    test_singleton_initialization_and_zero_retry_session()
    print("PASS: test_singleton_initialization_and_zero_retry_session")
    test_analytics_metadata_allowlist()
    print("PASS: test_analytics_metadata_allowlist")
    test_dev_analytics_shutdown_returns_cleanly()
    print("PASS: test_dev_analytics_shutdown_returns_cleanly")
    print(f"All {len(tests) + 3} MCP runtime tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
