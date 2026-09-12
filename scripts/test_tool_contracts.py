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

from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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


def _structured_error(result: Any) -> dict[str, Any]:
    """Extract a structured MCP tool error."""
    is_error = getattr(result, "isError", None)
    if is_error is None:
        is_error = getattr(result, "is_error", False)
    assert is_error, result
    kind, payload = _extract_call_tool_output(result)
    assert kind == "structured", result
    assert isinstance(payload, dict) and "error" in payload, result
    return payload["error"]


class FakeResponse:
    """Minimal ZenML response object used behind the real MCP protocol."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.__dict__.update(payload)

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return self.payload


class FakePipelineResponse(FakeResponse):
    def __init__(self) -> None:
        super().__init__({"id": "pipeline-1", "name": "training"})
        self.runs = [type("Run", (), {"status": "completed"})()]


class FakeStackComponent(FakeResponse):
    """Typed fake for component identifier resolution."""

    def __init__(self, *, id: str, name: str, type: str) -> None:
        super().__init__({"id": id, "name": name, "type": type})
        self.id = id
        self.name = name
        self.type = type


class FakeZenMLClient:
    """Small fake covering the legacy shapes that differ from plain get/list."""

    def __init__(self) -> None:
        self.calls: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self.call_history: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(
        self, call_name: str, payload: dict[str, Any], *args: Any, **kwargs: Any
    ) -> FakeResponse:
        self.calls[call_name] = (args, kwargs)
        self.call_history.append((call_name, args, kwargs))
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

    def list_snapshots(self, **kwargs: Any) -> FakeResponse:
        return self._record("list_snapshots", {"items": [], "total": 0}, **kwargs)

    def list_deployments(self, **kwargs: Any) -> FakeResponse:
        return self._record("list_deployments", {"items": [], "total": 0}, **kwargs)

    def list_artifacts(self, **kwargs: Any) -> FakeResponse:
        return self._record("list_artifacts", {"items": [], "total": 0}, **kwargs)

    def list_artifact_versions(self, **kwargs: Any) -> FakeResponse:
        return self._record(
            "list_artifact_versions",
            {
                "items": [{"id": "artifact-version-1"}],
                "total": 1,
                "page": 1,
                "size": 10,
            },
            **kwargs,
        )

    def get_model_version(self, *args: Any, **kwargs: Any) -> FakeResponse:
        return self._record(
            "get_model_version", {"id": "model-version-1"}, *args, **kwargs
        )

    def get_model(self, model_name_or_id: str) -> FakeResponse:
        return self._record(
            "get_model",
            {"id": "11111111-1111-4111-8111-111111111111"},
            model_name_or_id,
        )

    def list_models(self, **kwargs: Any) -> FakeResponse:
        return self._record("list_models", {"items": [], "total": 0}, **kwargs)

    def list_model_versions(self, **kwargs: Any) -> FakeResponse:
        return self._record(
            "list_model_versions",
            {"items": [{"id": "model-version-1"}], "total": 1, "page": 1, "size": 20},
            **kwargs,
        )

    def get_run_template(self, *args: Any, **kwargs: Any) -> FakeResponse:
        return self._record(
            "get_run_template", {"id": "run-template-1"}, *args, **kwargs
        )

    def list_run_templates(self, **kwargs: Any) -> FakeResponse:
        return self._record(
            "list_run_templates",
            {"items": [{"id": "run-template-1"}], "total": 1, "page": 1, "size": 20},
            **kwargs,
        )


class FakeStackComponentClient:
    """Fake that models cross-type component lookup and exact typed retrieval."""

    def __init__(self, components: list[FakeStackComponent]) -> None:
        self.components = components
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def list_stack_components(
        self,
        *,
        sort_by: str = "created",
        page: int = 1,
        size: int = 20,
        logical_operator: str = "and",
        id: str | None = None,
        created: str | None = None,
        updated: str | None = None,
        name: str | None = None,
        flavor: str | None = None,
        stack_id: str | None = None,
        hydrate: bool = False,
    ) -> FakeResponse:
        call = {
            "sort_by": sort_by,
            "page": page,
            "size": size,
            "logical_operator": logical_operator,
            "id": id,
            "created": created,
            "updated": updated,
            "name": name,
            "flavor": flavor,
            "stack_id": stack_id,
            "hydrate": hydrate,
        }
        self.list_calls.append(call)

        def matches_filter(actual: str, expression: str | None) -> bool:
            if expression is None:
                return True
            operator, value = (
                expression.split(":", 1)
                if ":" in expression
                else ("equals", expression)
            )
            return (
                actual.startswith(value)
                if operator == "startswith"
                else actual == value
            )

        predicates = [
            lambda component: matches_filter(str(component.id), id),
            lambda component: matches_filter(str(component.name), name),
        ]
        if logical_operator == "or" and id is not None and name is not None:
            matches = [
                component
                for component in self.components
                if any(predicate(component) for predicate in predicates)
            ]
        else:
            matches = [
                component
                for component in self.components
                if all(predicate(component) for predicate in predicates)
            ]
        return FakeResponse(
            {
                "items": matches[:size],
                "total": len(matches),
                "page": page,
                "size": size,
            }
        )

    def get_stack_component(
        self,
        *,
        component_type: str,
        name_id_or_prefix: str,
        allow_name_prefix_match: bool,
    ) -> FakeResponse:
        call = {
            "component_type": component_type,
            "name_id_or_prefix": name_id_or_prefix,
            "allow_name_prefix_match": allow_name_prefix_match,
        }
        self.get_calls.append(call)
        return next(
            component
            for component in self.components
            if str(component.id) == name_id_or_prefix
            and component.type == component_type
        )


async def test_legacy_inventory_and_schemas() -> None:
    """The complete legacy inventory and semantic input schemas stay stable."""
    expected = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    async with Client(server.mcp, mode="legacy") as session:
        result = await session.list_tools()

    actual_names = [tool.name for tool in result.tools]
    actual_schemas = {
        tool.name: _normalize_schema(tool.input_schema) for tool in result.tools
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
        async with Client(server.mcp, mode="legacy") as session:
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
        async with Client(server.mcp, mode="legacy") as session:
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
        assert fake_client.calls["list_model_versions"][0] == ()
        assert fake_client.calls["list_model_versions"][1]["model"] == (
            "11111111-1111-4111-8111-111111111111"
        )
    finally:
        server.zenml_client = original_client


async def test_current_sdk_filter_adapters() -> None:
    """Legacy filters translate without changing tool inputs or paging."""
    fake_client = FakeZenMLClient()
    original_client = server.zenml_client
    server.zenml_client = fake_client
    default_calls = {
        "list_snapshots": {},
        "list_deployments": {},
        "list_artifacts": {},
        "list_artifact_versions": {"artifact_name_or_id": "artifact"},
        "list_models": {},
        "list_model_versions": {"model_name_or_id": "model"},
        "list_run_templates": {},
    }
    tagged_calls = {
        name: ({**arguments, "tag": 'oneof:["nightly","release"]'})
        for name, arguments in default_calls.items()
        if name != "list_run_templates"
    }
    try:
        async with Client(server.mcp, mode="legacy") as session:
            for tool_name, arguments in default_calls.items():
                _structured_payload(await session.call_tool(tool_name, arguments))

            for tool_name, arguments in tagged_calls.items():
                _structured_payload(await session.call_tool(tool_name, arguments))
                assert fake_client.calls[tool_name][1]["tags"] == arguments["tag"]
                assert "tag" not in fake_client.calls[tool_name][1]

            calls_before = len(fake_client.call_history)
            error = _structured_error(
                await session.call_tool("list_run_templates", {"tag": "nightly"})
            )
            assert error["type"] == "UnsupportedFilter"
            assert "does not support tag filtering" in error["message"]
            assert len(fake_client.call_history) == calls_before

            calls_before = len(fake_client.call_history)
            error = _structured_error(
                await session.call_tool(
                    "list_deployments", {"status": "oneof:running,error"}
                )
            )
            assert error["type"] == "ValidationError"
            assert 'oneof:["running","error"]' in error["message"]
            assert len(fake_client.call_history) == calls_before

        assert fake_client.calls["list_model_versions"][1]["model"] == (
            "11111111-1111-4111-8111-111111111111"
        )
        assert fake_client.calls["get_model"][0] == ("model",)
    finally:
        server.zenml_client = original_client


async def test_stack_component_identifier_resolution() -> None:
    """Legacy component identifiers resolve across types before an exact get."""
    first = FakeStackComponent(
        id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        name="artifact-store",
        type="artifact_store",
    )
    second = FakeStackComponent(
        id="aaaabbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        name="orchestrator",
        type="orchestrator",
    )
    fake_client = FakeStackComponentClient([first, second])
    original_client = server.zenml_client
    server.zenml_client = fake_client
    try:
        async with Client(server.mcp, mode="legacy") as session:
            for identifier in (first.name, first.id, "aaaaaaaa"):
                payload = _structured_payload(
                    await session.call_tool(
                        "get_stack_component", {"name_id_or_prefix": identifier}
                    )
                )
                assert payload["id"] == first.id

            gets_before = len(fake_client.get_calls)
            error = _structured_error(
                await session.call_tool(
                    "get_stack_component", {"name_id_or_prefix": "aaaa"}
                )
            )
            assert error["type"] == "AmbiguousIdentifier"
            assert "matches multiple stack components" in error["message"]
            assert len(fake_client.get_calls) == gets_before

        assert fake_client.get_calls == [
            {
                "component_type": first.type,
                "name_id_or_prefix": first.id,
                "allow_name_prefix_match": False,
            },
            {
                "component_type": first.type,
                "name_id_or_prefix": first.id,
                "allow_name_prefix_match": False,
            },
            {
                "component_type": first.type,
                "name_id_or_prefix": first.id,
                "allow_name_prefix_match": False,
            },
        ]
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
            assert resource_by_uri[server.DASHBOARD_UI_URI].mime_type == (
                "text/html;profile=mcp-app"
            )
            assert resource_by_uri[server.DASHBOARD_UI_URI].meta == {
                "ui": {"csp": {"resourceDomains": ["https://unpkg.com"]}}
            }

            templates = await session.list_resource_templates()
            assert [
                str(item.uri_template) for item in templates.resource_templates
            ] == ["resource://zenml_server/most_recent_runs?run_count={run_count}"]

            app_manifest = await session.read_resource("resource://zenml_server/apps")
            manifest = json.loads(app_manifest.contents[0].text)
            assert [app["entry"] for app in manifest["apps"]] == [
                server.DASHBOARD_UI_URI,
                server.CHART_UI_URI,
            ]
            dashboard = await session.read_resource(server.DASHBOARD_UI_URI)
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
        ("test_current_sdk_filter_adapters", test_current_sdk_filter_adapters),
        (
            "test_stack_component_identifier_resolution",
            test_stack_component_identifier_resolution,
        ),
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
