#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "mcp[cli]==2.2.0",
#     "zenml==0.97.0",
#     "setuptools",
#     "requests>=2.32.0",
# ]
#
# [tool.uv]
# exclude-newer-package = { mcp = "2026-09-08T00:00:00Z", "mcp-types" = "2026-09-08T00:00:00Z" }
#
# [tool.ty.rules]
# unresolved-import = "ignore"
# ///
"""Fresh-process contracts for compact/legacy and read-only registration."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server" / "zenml_server.py"
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))

from generate_manifest_fields import _updated_manifest  # noqa: E402

LEGACY_CONTRACT = ROOT / "scripts" / "fixtures" / "legacy_tool_schemas.json"
GENERIC_TOOLS = (
    "zenml_describe_resources",
    "zenml_list_resources",
    "zenml_get_resource",
    "zenml_create_resource",
    "zenml_update_resource",
    "zenml_delete_resource",
    "zenml_action_resource",
)
COMPACT_READ_WRITE = (
    "diagnose_zenml_setup",
    "get_step_logs",
    *GENERIC_TOOLS,
    "get_active_user",
    "get_active_project",
    "trigger_pipeline",
    "get_deployment_logs",
    "get_step_code",
    "open_pipeline_run_dashboard",
    "open_run_activity_chart",
)
MUTATING_TOOLS = {
    "zenml_create_resource",
    "zenml_update_resource",
    "zenml_delete_resource",
    "zenml_action_resource",
    "trigger_pipeline",
}
LEGACY_SPECIALIZED = tuple(json.loads(LEGACY_CONTRACT.read_text())["tool_names"])
ALL_TOOLS = tuple(
    dict.fromkeys((*LEGACY_SPECIALIZED[:2], *GENERIC_TOOLS, *LEGACY_SPECIALIZED[2:]))
)
EXPECTED_TOOLS = {
    ("compact", "read_write"): COMPACT_READ_WRITE,
    ("compact", "read_only"): tuple(
        name for name in COMPACT_READ_WRITE if name not in MUTATING_TOOLS
    ),
    ("legacy", "read_write"): ALL_TOOLS,
    ("legacy", "read_only"): tuple(
        name for name in ALL_TOOLS if name not in MUTATING_TOOLS
    ),
}


def _server_env(
    profile: str | None,
    policy: str | None,
    *,
    legacy_read_only: str | None = None,
) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "ZENML_MCP_PROFILE",
            "ZENML_MCP_WRITE_POLICY",
            "ZENML_MCP_READ_ONLY",
            "ZENML_STORE_URL",
            "ZENML_STORE_API_KEY",
        }
    }
    env.update({"ZENML_MCP_ANALYTICS_ENABLED": "false", "NO_COLOR": "1"})
    if profile is not None:
        env["ZENML_MCP_PROFILE"] = profile
    if policy is not None:
        env["ZENML_MCP_WRITE_POLICY"] = policy
    if legacy_read_only is not None:
        env["ZENML_MCP_READ_ONLY"] = legacy_read_only
    return env


def _structured(result: Any) -> dict[str, Any]:
    payload = result.structured_content
    assert isinstance(payload, dict)
    return payload


async def _unknown_tool(session: ClientSession, name: str) -> None:
    try:
        result = await session.call_tool(name, {})
    except Exception as error:
        assert "unknown tool" in str(error).lower(), (name, error)
        return
    assert result.is_error is True, (name, result)
    assert "unknown tool" in repr(result).lower(), (name, result)


async def _inspect_profile(
    profile: str | None,
    policy: str | None,
    *,
    expected_profile: str | None = None,
    expected_policy: str | None = None,
    legacy_read_only: str | None = None,
) -> tuple[int, int]:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        env=_server_env(profile, policy, legacy_read_only=legacy_read_only),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            actual = [tool.name for tool in listed.tools]
            resolved_profile = expected_profile or profile or "compact"
            resolved_policy = expected_policy or policy or "read_write"
            expected = list(EXPECTED_TOOLS[(resolved_profile, resolved_policy)])
            assert actual == expected, (profile, policy, actual)
            assert len(actual) == len(set(actual))

            excluded = set(ALL_TOOLS) - set(actual)
            for name in sorted(excluded):
                await _unknown_tool(session, name)

            if resolved_policy == "read_only":
                described = _structured(
                    await session.call_tool("zenml_describe_resources", {})
                )
                for resource in described["resources"]:
                    assert resource["policy"] == "read_only"
                    assert not set(resource["operations"]) & {
                        "create",
                        "update",
                        "delete",
                        "action",
                    }
                catalog = await session.read_resource(
                    "resource://zenml_server/resources"
                )
                catalog_payload = json.loads(catalog.contents[0].text)
                assert catalog_payload == described
                disabled = await session.call_tool(
                    "zenml_describe_resources",
                    {"resource_type": "project", "operation": "create"},
                )
                assert disabled.is_error is True
                assert "read-only policy" in repr(disabled)
                schema = await session.read_resource(
                    "resource://zenml_server/resource-schemas/project/create"
                )
                assert schema.contents[0].text.startswith(
                    "Error in zenml_resource_operation_schema:"
                )

            schema_bytes = sum(
                len(json.dumps(tool.input_schema, sort_keys=True))
                for tool in listed.tools
            )
            return len(actual), schema_bytes


def test_invalid_profile_fails_startup() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'server'); import zenml_server",
        ],
        cwd=ROOT,
        env=_server_env("invalid", "read_write"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "ZENML_MCP_PROFILE must be" in result.stderr


async def main() -> int:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert tuple(tool["name"] for tool in manifest["tools"]) == COMPACT_READ_WRITE
    for profile in ("compact", "legacy"):
        for policy in ("read_write", "read_only"):
            expected = list(EXPECTED_TOOLS[(profile, policy)])
            rendered = _updated_manifest(
                manifest,
                [{"name": name, "description": "test"} for name in expected],
                manifest["prompts"],
                profile,
                policy,
            )
            assert [tool["name"] for tool in rendered["tools"]] == expected
            rendered_env = rendered["server"]["mcp_config"]["env"]
            assert rendered_env["ZENML_MCP_PROFILE"] == profile
            assert rendered_env["ZENML_MCP_WRITE_POLICY"] == policy
    configurable = _updated_manifest(
        manifest,
        manifest["tools"],
        manifest["prompts"],
        "compact",
        "read_write",
        update_runtime_env=False,
    )
    assert (
        configurable["server"]["mcp_config"]["env"]
        == manifest["server"]["mcp_config"]["env"]
    )
    await _inspect_profile(None, None)
    print("PASS: absent profile and policy use compact/read_write defaults")
    receipts: dict[tuple[str, str], tuple[int, int]] = {}
    for profile in ("compact", "legacy"):
        for policy in ("read_write", "read_only"):
            receipts[(profile, policy)] = await _inspect_profile(profile, policy)
            print(f"PASS: {profile}/{policy} profile contract")
    await _inspect_profile("compact", "invalid", expected_policy="read_only")
    print("PASS: invalid write policy fails closed")
    await _inspect_profile(
        "compact",
        "read_write",
        expected_policy="read_only",
        legacy_read_only="invalid",
    )
    print("PASS: invalid legacy read-only flag fails closed")
    await _inspect_profile(
        "compact",
        "read_write",
        expected_policy="read_only",
        legacy_read_only="true",
    )
    print("PASS: legacy read-only flag overrides read/write policy")
    assert (
        receipts[("compact", "read_write")][1] < receipts[("legacy", "read_write")][1]
    )
    test_invalid_profile_fails_startup()
    print("PASS: invalid profile fails startup")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
