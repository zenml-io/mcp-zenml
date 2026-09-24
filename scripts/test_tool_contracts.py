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
from unittest.mock import patch

from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SERVER_PATH = REPO_ROOT / "server" / "zenml_server.py"
CONTRACT_PATH = SCRIPT_DIR / "fixtures" / "legacy_tool_schemas.json"
GENERIC_READ_TOOLS = frozenset(
    {
        "zenml_describe_resources",
        "zenml_get_resource",
        "zenml_list_resources",
    }
)
GENERIC_MUTATION_TOOLS = frozenset(
    {
        "zenml_create_resource",
        "zenml_update_resource",
        "zenml_delete_resource",
        "zenml_action_resource",
    }
)
GENERIC_TOOLS = GENERIC_READ_TOOLS | GENERIC_MUTATION_TOOLS

sys.path.insert(0, str(REPO_ROOT / "server"))
sys.path.insert(0, str(SCRIPT_DIR))

os.environ.setdefault("ZENML_MCP_ANALYTICS_ENABLED", "false")
os.environ["ZENML_MCP_PROFILE"] = "legacy"
os.environ["ZENML_MCP_WRITE_POLICY"] = "read_write"
os.environ.pop("ZENML_MCP_READ_ONLY", None)

import zenml_server as server  # noqa: E402
from test_mcp_server import (  # noqa: E402  # noqa: E402
    _detect_tool_error,
    _extract_call_tool_output,
    _generic_project_id,
    _profile_inventory_errors,
    _validate_generic_project_get,
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


async def test_app_openers_have_truthful_text_fallbacks() -> None:
    dashboard = server.open_pipeline_run_dashboard()
    chart = server.open_run_activity_chart()
    assert "MCP Apps-capable host" in dashboard
    assert "zenml_list_resources" in dashboard
    assert "MCP Apps-capable" in chart
    assert "zenml_list_resources" in chart


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

    def list_users(self, **kwargs: Any) -> FakeResponse:
        return self._record(
            "list_users",
            {
                "items": [
                    {
                        "id": "user-1",
                        "name": "alex",
                        "body": {"activation_token": "invite-secret", "active": True},
                        "metadata": {"nested_token": "nested-secret"},
                    }
                ],
                "total": 1,
                "page": 1,
                "size": 50,
            },
            **kwargs,
        )

    def get_user(self, *args: Any, **kwargs: Any) -> FakeResponse:
        return self._record(
            "get_user",
            {
                "id": "user-1",
                "name": "alex",
                "body": {"activation_token": "invite-secret", "active": True},
            },
            *args,
            **kwargs,
        )

    @property
    def active_user(self) -> FakeResponse:
        return FakeResponse(
            {
                "id": "user-1",
                "name": "alex",
                "body": {"activation_token": "invite-secret", "active": True},
            }
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
    assert all(
        schema.get("additionalProperties") is False
        for schema in actual_schemas.values()
    )
    legacy_names = [name for name in actual_names if name not in GENERIC_TOOLS]
    assert legacy_names == expected["tool_names"]
    assert set(actual_names) == set(expected["tool_names"]) | GENERIC_TOOLS
    assert {name: actual_schemas[name] for name in expected["tool_names"]} == expected[
        "input_schemas"
    ]

    missing = set(actual_names)
    missing.remove("get_pipeline_details")
    errors = _profile_inventory_errors("legacy", "read_write", missing)
    assert errors and "get_pipeline_details" in errors[0]
    unexpected = set(actual_names) | {"unregistered_tool"}
    errors = _profile_inventory_errors("legacy", "read_write", unexpected)
    assert errors and "unregistered_tool" in errors[0]


async def test_unknown_arguments_are_rejected_before_mutations() -> None:
    """Undeclared options fail before generic and retained tools execute."""
    unexpected_client_calls = 0

    def record_client_call() -> Any:
        nonlocal unexpected_client_calls
        unexpected_client_calls += 1
        raise AssertionError("tool function executed after argument rejection")

    calls = (
        (
            "zenml_create_resource",
            {"resource_type": "tag", "dry_run": "FAKE-UNKNOWN-SECRET-789"},
        ),
        (
            "zenml_update_resource",
            {
                "resource_type": "tag",
                "resource_id": "11111111-1111-4111-8111-111111111111",
                "dry_run": "FAKE-UNKNOWN-SECRET-789",
            },
        ),
        (
            "zenml_delete_resource",
            {
                "resource_type": "tag",
                "resource_id": "11111111-1111-4111-8111-111111111111",
                "dry_run": "FAKE-UNKNOWN-SECRET-789",
            },
        ),
        (
            "zenml_action_resource",
            {
                "resource_type": "deployment",
                "action": "stop",
                "resource_id": "11111111-1111-4111-8111-111111111111",
                "dry_run": "FAKE-UNKNOWN-SECRET-789",
            },
        ),
        (
            "trigger_pipeline",
            {
                "snapshot_name_or_id": "snapshot-1",
                "dry_run": "FAKE-UNKNOWN-SECRET-789",
            },
        ),
    )

    with patch.object(server, "get_zenml_client", record_client_call):
        async with Client(server.mcp, mode="legacy") as session:
            for tool_name, arguments in calls:
                result = await session.call_tool(tool_name, arguments)
                assert result.is_error is True
                assert "dry_run" in repr(result)
                assert "FAKE-UNKNOWN-SECRET-789" not in repr(result)

    assert unexpected_client_calls == 0


async def test_server_instructions_prefer_generic_resource_tools() -> None:
    """Legacy discovery guides capable hosts toward the replacement tools."""
    assert "Prefer zenml_describe_resources" in server.INSTRUCTIONS
    assert "legacy compatibility profile" in server.INSTRUCTIONS
    assert "advertised only when the write\npolicy is read_write" in (
        server.INSTRUCTIONS
    )
    assert "Use the generic" in server.INSTRUCTIONS
    assert "resource tools for new calls and migrations" in server.INSTRUCTIONS


def _assert_no_token_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert "token" not in key.lower(), value
            _assert_no_token_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_token_keys(child)


async def test_legacy_user_tools_redact_token_fields() -> None:
    """Legacy user projections never expose activation or nested token fields."""
    fake_client = FakeZenMLClient()
    original_client = server.zenml_client
    server.zenml_client = fake_client
    try:
        async with Client(server.mcp, mode="legacy") as session:
            payloads = [
                _structured_payload(await session.call_tool("list_users", {})),
                _structured_payload(
                    await session.call_tool("get_user", {"name_id_or_prefix": "alex"})
                ),
                _structured_payload(await session.call_tool("get_active_user", {})),
            ]
        for payload in payloads:
            _assert_no_token_keys(payload)
            assert "invite-secret" not in repr(payload)
            assert "nested-secret" not in repr(payload)
    finally:
        server.zenml_client = original_client


async def test_legacy_sensitive_resources_redact_configuration() -> None:
    """Legacy service and component reads omit credential-bearing configuration."""

    class SensitiveClient:
        def get_service(self, name_id_or_prefix: str) -> FakeResponse:
            return FakeResponse(
                {
                    "id": name_id_or_prefix,
                    "config": {"password": "FAKE-SERVICE-SECRET"},
                    "endpoint": "https://internal.example",
                    "name": "safe-service-name",
                }
            )

        def list_stack_components(self, **kwargs: Any) -> FakeResponse:
            component = FakeStackComponent(
                id="11111111-1111-4111-8111-111111111111",
                name="alerter",
                type="alerter",
            )
            return FakeResponse({"items": [component], "total": 1})

        def get_stack_component(self, **kwargs: Any) -> FakeResponse:
            return FakeResponse(
                {
                    "id": kwargs["name_id_or_prefix"],
                    "name": "alerter",
                    "type": "alerter",
                    "configuration": {"slack_token": "FAKE-COMPONENT-SECRET"},
                }
            )

    original_client = server.zenml_client
    server.zenml_client = SensitiveClient()
    try:
        async with Client(server.mcp, mode="legacy") as session:
            service = _structured_payload(
                await session.call_tool(
                    "get_service", {"name_id_or_prefix": "service-1"}
                )
            )
            component = _structured_payload(
                await session.call_tool(
                    "get_stack_component",
                    {"name_id_or_prefix": "11111111-1111-4111-8111-111111111111"},
                )
            )
    finally:
        server.zenml_client = original_client

    serialized = repr((service, component))
    assert "FAKE-SERVICE-SECRET" not in serialized
    assert "FAKE-COMPONENT-SECRET" not in serialized
    assert "configuration" not in serialized and "config" not in serialized
    assert service["name"] == "safe-service-name"
    assert component["name"] == "alerter"


async def test_step_logs_send_source_or_logs_id() -> None:
    """Step log calls always satisfy ZenML's exactly-one selector contract."""
    calls: list[dict[str, Any]] = []

    def record_logs(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"logs": []}

    env = {
        "ZENML_STORE_URL": "https://zenml.example",
        "ZENML_STORE_API_KEY": "test-api-key",
    }
    with (
        patch.dict(os.environ, env),
        patch.object(server, "_access_tokens", {}),
        patch.object(
            server, "_request_access_token", return_value=("access-token", 3600)
        ),
        patch.object(server, "make_step_logs_request", side_effect=record_logs),
    ):
        async with Client(server.mcp, mode="legacy") as session:
            _structured_payload(
                await session.call_tool("get_step_logs", {"step_run_id": "step-1"})
            )
            _structured_payload(
                await session.call_tool(
                    "get_step_logs",
                    {"step_run_id": "step-1", "logs_id": "logs-1"},
                )
            )
            conflict = _structured_error(
                await session.call_tool(
                    "get_step_logs",
                    {
                        "step_run_id": "step-1",
                        "source": "step",
                        "logs_id": "logs-1",
                    },
                )
            )
            blank_source = _structured_error(
                await session.call_tool(
                    "get_step_logs", {"step_run_id": "step-1", "source": "  "}
                )
            )
            blank_logs_id = _structured_error(
                await session.call_tool(
                    "get_step_logs", {"step_run_id": "step-1", "logs_id": ""}
                )
            )
            _structured_payload(
                await session.call_tool(
                    "get_step_logs", {"step_run_id": "step-1", "tail": 200}
                )
            )
            bad_tails = [
                _structured_error(
                    await session.call_tool(
                        "get_step_logs", {"step_run_id": "step-1", "tail": tail}
                    )
                )
                for tail in (0, server.STEP_LOGS_MAX_ENTRIES + 1)
            ]

    assert calls == [
        {"source": "step", "logs_id": None, "tail": None},
        {"source": None, "logs_id": "logs-1", "tail": None},
        {"source": "step", "logs_id": None, "tail": 200},
    ]
    assert conflict["type"] == "ValidationError"
    assert "Only one" in conflict["message"]
    assert blank_source["type"] == "ValidationError"
    assert blank_logs_id["type"] == "ValidationError"
    assert all(error["type"] == "ValidationError" for error in bad_tails)

    # scripts/test_step_logs.py covers the HTTP requests themselves.
    for source, logs_id in ((" ", None), (None, "")):
        try:
            server.make_step_logs_request(
                "https://zenml.example",
                "step-1",
                "access-token",
                source=source,
                logs_id=logs_id,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("blank log selector reached the HTTP client")


async def test_diagnostics_fail_when_authentication_fails() -> None:
    """A healthy public endpoint cannot hide invalid ZenML credentials."""
    healthy = type(
        "HealthyResponse",
        (),
        {"status_code": 200, "json": lambda self: {"version": "0.97.0"}},
    )()
    unauthorized = __import__("requests").Response()
    unauthorized.status_code = 401
    unauthorized.url = "https://zenml.example/api/v1/login"

    with (
        patch.dict(
            os.environ,
            {
                "ZENML_STORE_URL": "https://zenml.example",
                "ZENML_STORE_API_KEY": "invalid-secret-value",
            },
        ),
        patch.object(server.requests, "get", return_value=healthy),
        patch.object(server.requests, "post", return_value=unauthorized),
    ):
        diagnostics = server.collect_zenml_setup_diagnostics()

    assert diagnostics["ok"] is False
    assert diagnostics["checks"]["connectivity"]["ok"] is True
    assert diagnostics["checks"]["authentication"] == {
        "attempted": True,
        "ok": False,
        "error_type": "HTTPError",
        "status_code": 401,
        "failure_kind": "rejected",
    }
    assert "authentication_failed" in {issue["code"] for issue in diagnostics["issues"]}
    assert "invalid-secret-value" not in repr(diagnostics)

    with (
        patch.dict(
            os.environ,
            {
                "ZENML_STORE_URL": "https://zenml.example",
                "ZENML_STORE_API_KEY": "valid-secret-value",
            },
        ),
        patch.object(server.requests, "get", return_value=healthy),
        patch.object(server, "get_access_token", return_value="access-token"),
    ):
        healthy_diagnostics = server.collect_zenml_setup_diagnostics()
    assert healthy_diagnostics["checks"]["authentication"] == {
        "attempted": True,
        "ok": True,
    }
    assert healthy_diagnostics["ok"] is True

    with (
        patch.dict(
            os.environ,
            {
                "ZENML_STORE_URL": "https://zenml.example",
                "ZENML_STORE_API_KEY": "unknown-secret-value",
            },
        ),
        patch.object(server.requests, "get", side_effect=server.requests.Timeout()),
        patch.object(server, "get_access_token", side_effect=server.requests.Timeout()),
    ):
        unreachable = server.collect_zenml_setup_diagnostics()
    issue_codes = {issue["code"] for issue in unreachable["issues"]}
    assert "authentication_unreachable" in issue_codes
    assert "authentication_failed" not in issue_codes
    assert "unknown-secret-value" not in repr(unreachable)

    unavailable_response = server.requests.Response()
    unavailable_response.status_code = 503
    unavailable_error = server.requests.HTTPError(response=unavailable_response)
    with (
        patch.dict(
            os.environ,
            {
                "ZENML_STORE_URL": "https://zenml.example",
                "ZENML_STORE_API_KEY": "unknown-secret-value",
            },
        ),
        patch.object(server.requests, "get", return_value=healthy),
        patch.object(server, "get_access_token", side_effect=unavailable_error),
    ):
        server_error = server.collect_zenml_setup_diagnostics()
    issue_codes = {issue["code"] for issue in server_error["issues"]}
    assert "authentication_server_error" in issue_codes
    assert "authentication_unreachable" not in issue_codes

    with (
        patch.dict(
            os.environ,
            {
                "ZENML_STORE_URL": "https://zenml.example",
                "ZENML_STORE_API_KEY": "unknown-secret-value",
            },
        ),
        patch.object(server.requests, "get", return_value=healthy),
        patch.object(server, "get_access_token", side_effect=KeyError("access_token")),
    ):
        invalid_response = server.collect_zenml_setup_diagnostics()
    issue_codes = {issue["code"] for issue in invalid_response["issues"]}
    assert "authentication_invalid_response" in issue_codes
    assert "authentication_unreachable" not in issue_codes

    class InvalidTokenResponse:
        def raise_for_status(self) -> None:
            return None

        def __init__(self, access_token: Any) -> None:
            self.access_token = access_token

        def json(self) -> dict[str, Any]:
            return {"access_token": self.access_token}

    for invalid_token in (None, "", 7):
        with patch.object(
            server.requests,
            "post",
            return_value=InvalidTokenResponse(invalid_token),
        ):
            try:
                server.get_access_token("https://zenml.example", "api-key")
            except RuntimeError as error:
                assert str(error) == "Invalid ZenML authentication response"
            else:
                raise AssertionError(
                    f"accepted invalid access token: {invalid_token!r}"
                )

    with (
        patch.dict(
            os.environ,
            {
                "ZENML_STORE_URL": "https://zenml.example",
                "ZENML_STORE_API_KEY": "unknown-secret-value",
            },
        ),
        patch.object(server, "_access_tokens", {}),
        patch.object(
            server,
            "_request_access_token",
            side_effect=json.JSONDecodeError("FAKE-LOGIN-SECRET", "not-json", 0),
        ),
    ):
        async with Client(server.mcp, mode="legacy") as session:
            malformed_login = _structured_error(
                await session.call_tool(
                    "get_step_logs",
                    {"step_run_id": "step-1", "source": "step"},
                )
            )
    assert malformed_login["type"] == "UpstreamError"
    assert "invalid JSON response" in malformed_login["message"]
    assert "FAKE-LOGIN-SECRET" not in repr(malformed_login)


async def test_trigger_pipeline_selector_contract() -> None:
    """Snapshot-only triggering works and conflicting selectors fail closed."""

    class TriggerClient:
        active_project = type("Project", (), {"id": "project-1"})()

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def trigger_pipeline(self, **kwargs: Any) -> FakeResponse:
            self.calls.append(kwargs)
            return FakeResponse({"id": "run-1"})

    fake_client = TriggerClient()
    original_client = server.zenml_client
    server.zenml_client = fake_client
    try:
        async with Client(server.mcp, mode="legacy") as session:
            snapshot = _structured_payload(
                await session.call_tool(
                    "trigger_pipeline", {"snapshot_name_or_id": "snapshot-1"}
                )
            )
            template = _structured_payload(
                await session.call_tool(
                    "trigger_pipeline",
                    {"template_id": "11111111-1111-4111-8111-111111111111"},
                )
            )
            conflict = _structured_error(
                await session.call_tool(
                    "trigger_pipeline",
                    {
                        "snapshot_name_or_id": "snapshot-1",
                        "template_id": "11111111-1111-4111-8111-111111111111",
                    },
                )
            )
            missing = _structured_error(await session.call_tool("trigger_pipeline", {}))
    finally:
        server.zenml_client = original_client

    assert snapshot["pipeline_run"] == {"id": "run-1"}
    assert fake_client.calls == [
        {"snapshot_name_or_id": "snapshot-1"},
        {"template_id": "11111111-1111-4111-8111-111111111111"},
    ]
    assert "deprecated" in template["deprecation_warning"].lower()
    assert conflict["type"] == "ValidationError"
    assert "mutually exclusive" in conflict["message"]
    assert missing["type"] == "ValidationError"
    assert "at least one" in missing["message"]


async def test_missing_step_source_code_is_an_error() -> None:
    """Missing source code is never serialized as a successful 'None' string."""

    class StepClient:
        def get_run_step(self, step_run_id: str) -> Any:
            return type("Step", (), {"source_code": None})()

    original_client = server.zenml_client
    server.zenml_client = StepClient()
    try:
        async with Client(server.mcp, mode="legacy") as session:
            error = _structured_error(
                await session.call_tool("get_step_code", {"step_run_id": "step-1"})
            )
    finally:
        server.zenml_client = original_client

    assert error["type"] == "FeatureUnavailable"
    assert error["message"] == "Source code is unavailable for this step run."

    class MissingStepClient:
        def get_run_step(self, step_run_id: str) -> Any:
            raise KeyError(step_run_id)

    server.zenml_client = MissingStepClient()
    try:
        async with Client(server.mcp, mode="legacy") as session:
            not_found = _structured_error(
                await session.call_tool("get_step_code", {"step_run_id": "missing"})
            )
    finally:
        server.zenml_client = original_client
    assert not_found["type"] == "NotFound"

    from zenml.exceptions import DoesNotExistException

    class SdkMissingStepClient:
        def get_run_step(self, step_run_id: str) -> Any:
            raise DoesNotExistException("missing step")

    server.zenml_client = SdkMissingStepClient()
    try:
        async with Client(server.mcp, mode="legacy") as session:
            sdk_not_found = _structured_error(
                await session.call_tool("get_step_code", {"step_run_id": "missing"})
            )
    finally:
        server.zenml_client = original_client
    assert sdk_not_found["type"] == "NotFound"


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
            description = _structured_payload(
                await session.call_tool("zenml_describe_resources", {})
            )
            assert len(description["resources"]) == 30
            catalog_result = await session.read_resource(
                "resource://zenml_server/resources"
            )
            catalog = json.loads(catalog_result.contents[0].text)
            assert len(catalog["resources"]) == 30
            schema_result = await session.read_resource(
                "resource://zenml_server/resource-schemas/pipeline/list"
            )
            schema = json.loads(schema_result.contents[0].text)
            assert schema["resource_type"] == "pipeline"
            assert schema["operation"] == "list"
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
            actual_names = [tool.name for tool in tools.tools]
            assert [
                name for name in actual_names if name not in GENERIC_TOOLS
            ] == expected["tool_names"]
            assert set(actual_names) == set(expected["tool_names"]) | GENERIC_TOOLS

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
                "resource://zenml_server/resources",
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
            ] == [
                "resource://zenml_server/resource-schemas/{resource_type}/{operation}",
                "resource://zenml_server/most_recent_runs?run_count={run_count}",
            ]

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


async def test_generic_smoke_payload_guards() -> None:
    """Smoke payload guards reject empty, malformed, and mismatched results."""
    valid_list = {
        "resource_type": "project",
        "items": [{"id": "project-1"}],
        "total": 1,
        "page": 1,
        "size": 1,
        "effective_scope": {"kind": "global"},
    }
    assert _generic_project_id(valid_list) == "project-1"
    for invalid in (
        {"resource_type": "project", "items": []},
        {**valid_list, "resource_type": "pipeline"},
        {**valid_list, "items": [{}]},
    ):
        try:
            _generic_project_id(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid generic list passed: {invalid!r}")

    _validate_generic_project_get(
        "structured",
        {"resource_type": "project", "item": {"id": "project-1"}},
        "project-1",
    )
    for kind, payload in (
        ("text", {"resource_type": "project", "item": {"id": "project-1"}}),
        ("structured", {"resource_type": "pipeline", "item": {"id": "project-1"}}),
        ("structured", {"resource_type": "project", "item": {"id": "wrong"}}),
    ):
        try:
            _validate_generic_project_get(kind, payload, "project-1")
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid generic get passed: {kind}, {payload!r}")


async def main() -> int:
    tests: list[tuple[str, Callable[[], Any]]] = [
        (
            "test_app_openers_have_truthful_text_fallbacks",
            test_app_openers_have_truthful_text_fallbacks,
        ),
        ("test_legacy_inventory_and_schemas", test_legacy_inventory_and_schemas),
        (
            "test_unknown_arguments_are_rejected_before_mutations",
            test_unknown_arguments_are_rejected_before_mutations,
        ),
        (
            "test_server_instructions_prefer_generic_resource_tools",
            test_server_instructions_prefer_generic_resource_tools,
        ),
        (
            "test_legacy_user_tools_redact_token_fields",
            test_legacy_user_tools_redact_token_fields,
        ),
        (
            "test_legacy_sensitive_resources_redact_configuration",
            test_legacy_sensitive_resources_redact_configuration,
        ),
        (
            "test_step_logs_send_source_or_logs_id",
            test_step_logs_send_source_or_logs_id,
        ),
        (
            "test_diagnostics_fail_when_authentication_fails",
            test_diagnostics_fail_when_authentication_fails,
        ),
        (
            "test_trigger_pipeline_selector_contract",
            test_trigger_pipeline_selector_contract,
        ),
        (
            "test_missing_step_source_code_is_an_error",
            test_missing_step_source_code_is_an_error,
        ),
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
        ("test_generic_smoke_payload_guards", test_generic_smoke_payload_guards),
    ]
    for name, test in tests:
        await test()
        print(f"PASS: {name}")
    print(f"All {len(tests)} tool contract tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
