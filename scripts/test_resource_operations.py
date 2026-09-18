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
"""Credential-free invocation tests for every generic resource read adapter."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from mcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from zenml.enums import StackComponentType, TriggerFlavor
from zenml_resource_dispatch import (
    GET_ADAPTERS,
    LIST_ADAPTERS,
    ResourceDispatchError,
    ResourceFeatureUnavailable,
    ResourceNotFound,
    ResourcePermissionDenied,
    get_resource,
    list_resources,
    safe_project,
)
from zenml_resource_registry import RESOURCE_REGISTRY

os.environ.setdefault("ZENML_MCP_ANALYTICS_ENABLED", "false")
import zenml_mcp_analytics as analytics  # noqa: E402
import zenml_server as server  # noqa: E402

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
PARENT_ID = "22222222-2222-4222-8222-222222222222"

EXPECTED_LIST_METHODS = {
    "project": "list_projects",
    "user": "list_users",
    "stack": "list_stacks",
    "stack_component": "list_stack_components",
    "flavor": "list_flavors",
    "service": "list_services",
    "pipeline": "list_pipelines",
    "pipeline_run": "list_pipeline_runs",
    "run_step": "list_run_steps",
    "snapshot": "list_snapshots",
    "build": "list_builds",
    "run_template": "list_run_templates",
    "deployment": "list_deployments",
    "schedule": "list_schedules",
    "artifact": "list_artifacts",
    "artifact_version": "list_artifact_versions",
    "model": "list_models",
    "model_version": "list_model_versions",
    "tag": "list_tags",
    "secret": "list_secrets",
    "service_connector": "list_service_connectors",
    "service_connector_type": "list_service_connector_types",
    "code_repository": "list_code_repositories",
    "webhook": "list_webhooks",
    "schedule_trigger": "list_schedule_triggers",
    "platform_event_trigger": "list_platform_event_triggers",
    "webhook_trigger": "list_webhook_triggers",
    "resource_request": "list_resource_requests",
    "run_wait_condition": "list_run_wait_conditions",
    "hook_invocation": "list_hook_invocations",
}

EXPECTED_GET_METHODS = {
    "project": "get_project",
    "user": "get_user",
    "stack": "get_stack",
    "stack_component": "get_stack_component",
    "flavor": "get_flavor",
    "service": "get_service",
    "pipeline": "get_pipeline",
    "pipeline_run": "get_pipeline_run",
    "run_step": "get_run_step",
    "snapshot": "get_snapshot",
    "build": "get_build",
    "run_template": "get_run_template",
    "deployment": "get_deployment",
    "schedule": "get_schedule",
    "artifact": "get_artifact",
    "artifact_version": "get_artifact_version",
    "model": "get_model",
    "model_version": "get_model_version",
    "tag": "get_tag",
    "service_connector": "get_service_connector",
    "service_connector_type": "get_service_connector_type",
    "code_repository": "get_code_repository",
    "webhook": "get_webhook",
    "schedule_trigger": "get_schedule_trigger",
    "platform_event_trigger": "get_platform_event_trigger",
    "webhook_trigger": "get_webhook_trigger",
    "resource_request": "get_resource_request",
    "hook_invocation": "get_hook_invocation",
}

EXPECTED_GET_IDENTIFIER_KEYWORDS = {
    "project": "name_id_or_prefix",
    "user": "name_id_or_prefix",
    "stack": "name_id_or_prefix",
    "stack_component": "name_id_or_prefix",
    "flavor": "name_id_or_prefix",
    "service": "name_id_or_prefix",
    "pipeline": "name_id_or_prefix",
    "pipeline_run": "name_id_or_prefix",
    "run_step": "step_run_id",
    "snapshot": "name_id_or_prefix",
    "build": "id_or_prefix",
    "run_template": "name_id_or_prefix",
    "deployment": "name_id_or_prefix",
    "schedule": "name_id_or_prefix",
    "artifact": "name_id_or_prefix",
    "artifact_version": "name_id_or_prefix",
    "model": "model_name_or_id",
    "model_version": "model_version_name_or_number_or_id",
    "tag": "tag_name_or_id",
    "service_connector": "name_id_or_prefix",
    "service_connector_type": "connector_type",
    "code_repository": "name_id_or_prefix",
    "webhook": "name_id_or_prefix",
    "schedule_trigger": "trigger_name_id_or_prefix",
    "platform_event_trigger": "trigger_name_id_or_prefix",
    "webhook_trigger": "trigger_name_id_or_prefix",
    "resource_request": "resource_request_id",
    "hook_invocation": "hook_invocation_id",
}


class FakePage:
    def __init__(
        self, kwargs: dict[str, Any], items: list[dict[str, Any]] | None = None
    ) -> None:
        self.kwargs = kwargs
        self.items = items or [{"id": "item"}]

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {
            "items": self.items,
            "total": 41,
            "index": 7,
            "max_size": 13,
        }


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.active_project = SimpleNamespace(id=uuid.UUID(PROJECT_ID))
        self.zen_store = StoreRecorder(self)
        self.item_factory: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None
        self.page_items: dict[str, list[dict[str, Any]]] = {}

    def __getattr__(self, name: str):
        def call(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            if name == "list_service_connector_types":
                return [{"connector_type": f"type-{index}"} for index in range(9)]
            if name.startswith("list_"):
                return FakePage(kwargs, self.page_items.get(name))
            return self._get_item(name, kwargs)

        return call

    def _get_item(self, name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        if self.item_factory is not None:
            return self.item_factory(name, kwargs)
        item: dict[str, Any] = {"id": "target"}
        if name not in {
            "get_project",
            "get_user",
            "get_stack",
            "get_stack_component",
            "get_flavor",
            "get_tag",
            "get_service_connector",
            "get_service_connector_type",
        }:
            item["body"] = {"project_id": PROJECT_ID}
        if name == "get_artifact_version":
            item["resources"] = {"artifact_id": PARENT_ID}
        elif name == "get_model_version":
            item["body"] = {"project_id": PROJECT_ID, "model_id": PARENT_ID}
        elif name == "get_run_step":
            item["body"] = {"project_id": PROJECT_ID}
            item["metadata"] = {"pipeline_run_id": PARENT_ID}
        elif name == "get_stack_component":
            item["type"] = kwargs["component_type"]
        return item


class StoreRecorder:
    def __init__(self, owner: Recorder) -> None:
        self.owner = owner

    def list_resource_requests(self, **kwargs: Any) -> FakePage:
        self.owner.calls.append(("list_resource_requests", kwargs))
        filter_model = kwargs["filter_model"]
        return FakePage({"page": filter_model.page, "size": filter_model.size})

    def get_resource_request(self, **kwargs: Any) -> dict[str, Any]:
        self.owner.calls.append(("get_resource_request", kwargs))
        return {"id": kwargs["resource_request_id"]}


def _list_filters(resource_type: str) -> dict[str, Any]:
    required = RESOURCE_REGISTRY[resource_type].list_required_filters
    values = {field: PARENT_ID for field in required}
    return values


def _get_kwargs(resource_type: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    spec = RESOURCE_REGISTRY[resource_type]
    if spec.scope == "project":
        kwargs["project_id"] = PROJECT_ID
    if resource_type == "artifact_version":
        kwargs["artifact_id"] = PARENT_ID
    elif resource_type == "model_version":
        kwargs["model_id"] = PARENT_ID
    elif resource_type == "run_step":
        kwargs["pipeline_run_id"] = PARENT_ID
    elif resource_type == "stack_component":
        kwargs["component_type"] = "orchestrator"
    return kwargs


def test_every_list_adapter_invokes_exact_method() -> None:
    assert set(LIST_ADAPTERS) == set(EXPECTED_LIST_METHODS)
    for resource_type, method in EXPECTED_LIST_METHODS.items():
        client = Recorder()
        spec = RESOURCE_REGISTRY[resource_type]
        project_id = PROJECT_ID if spec.scope == "project" else None
        result = list_resources(
            client,
            resource_type,
            filters=_list_filters(resource_type),
            project_id=project_id,
            page=2,
            size=3,
        )
        assert client.calls[-1][0] == method, resource_type
        assert result["resource_type"] == resource_type
        call_kwargs = client.calls[-1][1]
        if spec.non_paginated:
            assert result["page"] == 2 and result["size"] == 3
            assert result["total"] == 9 and len(result["items"]) == 3
            assert "page" not in call_kwargs and "size" not in call_kwargs
        else:
            assert result["page"] == 7 and result["size"] == 13
            assert result["total"] == 41
            if resource_type == "resource_request":
                filter_model = call_kwargs["filter_model"]
                assert filter_model.page == 2 and filter_model.size == 3
                assert call_kwargs["hydrate"] is False
            else:
                assert call_kwargs["page"] == 2 and call_kwargs["size"] == 3
                assert call_kwargs["hydrate"] is False
        if spec.scope == "project":
            assert call_kwargs["project"] == PROJECT_ID
        else:
            assert "project" not in call_kwargs
        if resource_type == "artifact_version":
            assert call_kwargs["artifact"] == PARENT_ID
            assert "artifact_id" not in call_kwargs
        elif resource_type == "model_version":
            assert call_kwargs["model"] == PARENT_ID
            assert "model_id" not in call_kwargs
        elif resource_type == "run_step":
            assert call_kwargs["pipeline_run_id"] == PARENT_ID
        if resource_type == "service_connector":
            assert call_kwargs["expand_secrets"] is False


def test_every_get_adapter_invokes_exact_method() -> None:
    assert set(GET_ADAPTERS) == set(EXPECTED_GET_METHODS)
    for resource_type, method in EXPECTED_GET_METHODS.items():
        client = Recorder()
        result = get_resource(
            client, resource_type, "target", **_get_kwargs(resource_type)
        )
        assert client.calls[-1][0] == method, resource_type
        assert result["resource_type"] == resource_type
        assert result["item"]["id"] == "target"
        call_kwargs = client.calls[-1][1]
        assert call_kwargs[EXPECTED_GET_IDENTIFIER_KEYWORDS[resource_type]] == "target"
        if RESOURCE_REGISTRY[
            resource_type
        ].scope == "project" and resource_type not in {
            "run_step",
            "hook_invocation",
        }:
            assert call_kwargs["project"] == PROJECT_ID
        else:
            assert "project" not in call_kwargs
        if resource_type == "model_version":
            assert call_kwargs["model_name_or_id"] == PARENT_ID
            assert call_kwargs["model_version_name_or_number_or_id"] == "target"
        if resource_type == "artifact_version":
            assert call_kwargs["name_id_or_prefix"] == "target"
        if resource_type == "run_step":
            assert call_kwargs["hydrate"] is True
        if resource_type == "stack_component":
            assert call_kwargs["component_type"] is StackComponentType.ORCHESTRATOR


def test_every_read_pair_through_mcp() -> None:
    """Every advertised read pair crosses the public MCP schema and decorator."""

    async def invoke() -> None:
        fake = Recorder()
        with patch.object(server, "zenml_client", fake):
            async with Client(server.mcp, mode="auto") as mcp_client:
                for resource_type, expected_method in EXPECTED_LIST_METHODS.items():
                    spec = RESOURCE_REGISTRY[resource_type]
                    arguments: dict[str, Any] = {
                        "resource_type": resource_type,
                        "filters": _list_filters(resource_type),
                        "page": 2,
                        "size": 3,
                    }
                    if spec.scope == "project":
                        arguments["project_id"] = PROJECT_ID
                    result = await mcp_client.call_tool(
                        "zenml_list_resources", arguments
                    )
                    assert result.is_error is False, resource_type
                    assert fake.calls[-1][0] == expected_method, resource_type
                    assert result.structured_content is not None
                    assert result.structured_content["resource_type"] == resource_type

                for resource_type, expected_method in EXPECTED_GET_METHODS.items():
                    arguments = {
                        "resource_type": resource_type,
                        "resource_id": "target",
                        **_get_kwargs(resource_type),
                    }
                    result = await mcp_client.call_tool("zenml_get_resource", arguments)
                    assert result.is_error is False, resource_type
                    assert fake.calls[-1][0] == expected_method, resource_type
                    assert result.structured_content is not None
                    assert result.structured_content["resource_type"] == resource_type

    asyncio.run(invoke())


def test_validation_happens_before_sdk_calls() -> None:
    client = Recorder()
    invalid_calls = [
        lambda: list_resources(client, "pipeline", size=201),
        lambda: list_resources(client, "pipeline", filters={"unknown": "x"}),
        lambda: list_resources(
            client, "schedule_trigger", filters={"flavor": "webhook"}
        ),
        lambda: list_resources(client, "artifact_version", filters={}),
        lambda: get_resource(client, "model_version", "target", project_id=PROJECT_ID),
        lambda: list_resources(client, "pipeline", project_id=" "),
        lambda: list_resources(client, "user", project_id=PROJECT_ID),
        lambda: list_resources(client, "user", filters={"active": "true"}),
        lambda: list_resources(client, "pipeline_run", filters={"index": True}),
        lambda: list_resources(
            client, "schedule_trigger", filters={"concurrency": "queue"}
        ),
        lambda: list_resources(
            client, "service_connector", filters={"labels": ["team=ml"]}
        ),
        lambda: list_resources(
            client, "service_connector", filters={"resource_type": ["s3"]}
        ),
        lambda: list_resources(
            client, "snapshot", filters={"trigger_id": "not-a-uuid"}
        ),
        lambda: get_resource(
            client,
            "stack_component",
            "target",
            component_type="not-a-component-type",
        ),
    ]
    for call in invalid_calls:
        before = len(client.calls)
        try:
            call()
        except (ResourceDispatchError, ValueError):
            pass
        else:
            raise AssertionError("invalid generic read reached the SDK")
        assert len(client.calls) == before


def test_stack_component_get_rejects_uuid_from_another_type() -> None:
    client = Recorder()
    client.item_factory = lambda name, kwargs: {
        "id": "target",
        "type": "StackComponentType.ALERTER",
    }
    try:
        get_resource(
            client,
            "stack_component",
            "target",
            component_type="orchestrator",
        )
    except ResourceNotFound:
        pass
    else:
        raise AssertionError("stack component UUID bypassed component_type")
    assert client.calls == [
        (
            "get_stack_component",
            {
                "name_id_or_prefix": "target",
                "component_type": StackComponentType.ORCHESTRATOR,
                "hydrate": False,
            },
        )
    ]


def test_typed_filters_are_forwarded_after_local_validation() -> None:
    client = Recorder()
    list_resources(
        client,
        "schedule_trigger",
        project_id=PROJECT_ID,
        filters={
            "active": True,
            "concurrency": ["skip", "submit"],
            "next_occurrence": ["gte:2026-09-12", "lt:2026-09-14"],
        },
    )
    kwargs = client.calls[-1][1]
    assert kwargs["active"] is True
    assert kwargs["concurrency"] == 'oneof:["skip","submit"]'
    assert kwargs["next_occurrence"] == [
        "gte:2026-09-12 00:00:00",
        "lt:2026-09-14 23:59:59",
    ]

    client = Recorder()
    list_resources(
        client,
        "service_connector",
        filters={"labels": {"team": "ml", "region": None}},
    )
    assert client.calls[-1][1]["labels"] == {"team": "ml", "region": None}


def test_list_arrays_use_single_field_or_semantics() -> None:
    client = Recorder()
    list_resources(
        client,
        "pipeline_run",
        project_id=PROJECT_ID,
        filters={
            "status": ["failed", "completed"],
            "pipeline_name": "important",
            "logical_operator": "and",
        },
    )
    kwargs = client.calls[-1][1]
    assert kwargs["status"] == 'oneof:["failed","completed"]'
    assert kwargs["pipeline_name"] == "important"
    assert kwargs["logical_operator"] == "and"

    client = Recorder()
    list_resources(
        client,
        "pipeline_run",
        project_id=PROJECT_ID,
        filters={
            "status": 'oneof:["failed","completed"]',
            "pipeline_name": "important",
            "logical_operator": "or",
        },
    )
    kwargs = client.calls[-1][1]
    assert kwargs["status"] == 'oneof:["failed","completed"]'
    assert kwargs["logical_operator"] == "or"


def test_execution_status_filters_reject_misspellings_before_sdk_calls() -> None:
    for value in (
        "complete",
        ["failed", "complete"],
        'oneof:["failed","complete"]',
    ):
        client = Recorder()
        try:
            list_resources(
                client,
                "pipeline_run",
                project_id=PROJECT_ID,
                filters={"status": value},
            )
        except ResourceDispatchError:
            pass
        else:
            raise AssertionError(f"invalid execution status {value!r} was accepted")
        assert client.calls == []


def test_trigger_discriminators_and_datetime_bounds() -> None:
    client = Recorder()
    list_resources(
        client,
        "schedule_trigger",
        project_id=PROJECT_ID,
        filters={"next_occurrence": "lte:2026-09-12"},
    )
    kwargs = client.calls[-1][1]
    assert kwargs["flavor"] is TriggerFlavor.NATIVE_SCHEDULE
    assert kwargs["next_occurrence"] == "lte:2026-09-12 23:59:59"
    client = Recorder()
    list_resources(client, "platform_event_trigger", project_id=PROJECT_ID)
    assert client.calls[-1][1]["flavor"] is TriggerFlavor.PLATFORM_EVENT


def test_safe_projection_and_connector_flags() -> None:
    projected = safe_project(
        {
            "connector_type": "aws",
            "auth_method": "secret-key",
            "metadata": {
                "display_name": "metadata-kept",
                "configuration": {"region": "eu", "api_key": "bad"},
                "pipeline_configuration": {
                    "environment": {"AWS_SECRET_ACCESS_KEY": "bad-access"}
                },
                "labels": {
                    "team": "ml",
                    "DATABASE_URL": "bad-database-url",
                    "signing-key": "bad-signing-key",
                },
                "run_metadata": {
                    "apiKey": "bad-api-camel",
                    "accessKey": "bad-access-camel",
                    "privateKey": "bad-private-camel",
                    "signingKey": "bad-signing-camel",
                    "databaseUrl": "bad-database-camel",
                    "metric": 0.9,
                },
            },
            "nested": {"signing_secret": "bad", "token": "bad"},
        },
        resource_type="pipeline",
    )
    text = repr(projected)
    assert "bad" not in text and "configuration" not in text
    assert projected["metadata"]["display_name"] == "metadata-kept"
    assert projected["metadata"]["labels"] == {"team": "ml"}
    assert projected["metadata"]["run_metadata"] == {"metric": 0.9}
    secret = safe_project(
        {
            "id": "secret-1",
            "name": "metadata-kept",
            "body": {"private": True, "values": {"safe-looking-key": "bad-secret"}},
        },
        resource_type="secret",
    )
    assert secret["name"] == "metadata-kept"
    assert secret["body"] == {"private": True}
    assert "bad-secret" not in repr(secret)
    repository = safe_project(
        {
            "id": "repository-1",
            "name": "metadata-kept",
            "metadata": {
                "source": "github",
                "config": {"token": "bad-token", "custom_credential": "bad-custom"},
            },
        },
        resource_type="code_repository",
    )
    assert repository["metadata"] == {"source": "github"}
    assert "bad-" not in repr(repository)
    deployment_client = Recorder()
    deployment_client.item_factory = lambda name, kwargs: {
        "id": "target",
        "body": {"project_id": PROJECT_ID},
        "metadata": {
            "auth_key": "bad-deployment-auth-key",
            "endpoint": "https://deployment.example.test",
        },
        "resources": {"tags": [{"id": "tag-id"}]},
    }
    deployment = get_resource(
        deployment_client,
        "deployment",
        "target",
        project_id=PROJECT_ID,
        hydrate=True,
    )
    assert "bad-deployment-auth-key" not in repr(deployment)
    assert deployment["item"]["metadata"]["endpoint"].startswith("https://")
    assert deployment["item"]["resources"]["tags"] == [{"id": "tag-id"}]
    assert deployment_client.calls[-1][1]["hydrate"] is True
    client = Recorder()
    list_resources(client, "service_connector")
    assert client.calls[-1][1]["expand_secrets"] is False

    repeated_documentation = "connector documentation " * 1_000
    client.page_items["list_service_connectors"] = [
        {
            "id": f"connector-{index}",
            "name": f"connector-{index}",
            "body": {
                "connector_type": {
                    "connector_type": "aws",
                    "name": "AWS",
                    "description": repeated_documentation,
                    "auth_methods": [
                        {
                            "auth_method": "secret-key",
                            "config_schema": {"description": repeated_documentation},
                        }
                    ],
                },
                "auth_method": "secret-key",
                "resource_types": ["s3-bucket"],
            },
        }
        for index in range(20)
    ]
    connector_page = list_resources(client, "service_connector", size=20)
    assert connector_page["items"][0]["body"]["connector_type"] == "aws"
    assert "auth_methods" not in repr(connector_page)
    assert repeated_documentation not in repr(connector_page)
    assert len(repr(connector_page)) < 20_000

    client = Recorder()
    get_resource(client, "service_connector", "target")
    assert client.calls[-1][1]["expand_secrets"] is False

    client = Recorder()
    client.page_items["list_secrets"] = [
        {
            "id": "secret-1",
            "name": "metadata-kept",
            "body": {"private": True, "values": {"key": "bad-secret"}},
        }
    ]
    secret_result = list_resources(client, "secret")
    assert secret_result["items"] == [
        {
            "id": "secret-1",
            "name": "metadata-kept",
            "body": {"private": True},
        }
    ]

    client = Recorder()
    client.item_factory = lambda name, kwargs: {
        "id": "repository-1",
        "name": "metadata-kept",
        "body": {"project_id": PROJECT_ID},
        "metadata": {
            "source": "github",
            "config": {"custom_credential": "bad-custom"},
        },
    }
    repository_result = get_resource(
        client, "code_repository", "repository-1", project_id=PROJECT_ID
    )
    assert repository_result["item"]["metadata"] == {"source": "github"}
    assert "bad-custom" not in repr(repository_result)


def test_active_project_scope_is_forwarded_and_reported() -> None:
    client = Recorder()
    listed = list_resources(client, "pipeline")
    assert client.calls[-1][1]["project"] == PROJECT_ID
    assert listed["effective_scope"] == {
        "kind": "project",
        "project_id": PROJECT_ID,
        "source": "active_project",
    }

    client = Recorder()
    fetched = get_resource(client, "pipeline", "target")
    assert client.calls[-1][1]["project"] == PROJECT_ID
    assert fetched["effective_scope"] == {
        "kind": "project",
        "project_id": PROJECT_ID,
        "source": "active_project",
    }


def test_project_and_parent_mismatches_are_rejected() -> None:
    client = Recorder()
    client.item_factory = lambda name, kwargs: {
        "id": "target",
        "body": {"project_id": PARENT_ID},
    }
    try:
        get_resource(client, "pipeline", "target", project_id=PROJECT_ID)
    except ResourceNotFound:
        pass
    else:
        raise AssertionError("cross-project UUID lookup was accepted")

    client = Recorder()
    client.item_factory = lambda name, kwargs: {"id": "target"}
    try:
        get_resource(client, "pipeline", "target", project_id=PROJECT_ID)
    except ResourceNotFound:
        pass
    else:
        raise AssertionError(
            "project-scoped response without project metadata was accepted"
        )

    client = Recorder()
    client.item_factory = lambda name, kwargs: {
        "id": "target",
        "body": {"project_id": PROJECT_ID},
    }
    try:
        get_resource(
            client,
            "artifact_version",
            "target",
            project_id=PROJECT_ID,
            artifact_id=PARENT_ID,
        )
    except ResourceNotFound:
        pass
    else:
        raise AssertionError("nested response without parent metadata was accepted")


def test_upstream_categories_remain_distinct() -> None:
    from zenml.exceptions import (
        DoesNotExistException,
        IllegalOperationError,
        SubscriptionUpgradeRequiredError,
    )

    for upstream_error, expected in (
        (SubscriptionUpgradeRequiredError("upgrade"), ResourceFeatureUnavailable),
        (IllegalOperationError("forbidden"), ResourcePermissionDenied),
        (DoesNotExistException("missing"), ResourceNotFound),
    ):
        client = Recorder()
        client.__dict__["list_schedule_triggers"] = lambda **kwargs: (
            _ for _ in ()
        ).throw(upstream_error)
        try:
            list_resources(client, "schedule_trigger", project_id=PROJECT_ID)
        except expected:
            pass
        else:
            raise AssertionError(f"{type(upstream_error).__name__} was not translated")


def test_analytics_resource_metadata_is_allowlisted() -> None:
    assert {
        "resource_type",
        "operation",
        "action",
        "profile",
        "outcome",
        "size",
    } <= analytics.ALLOWED_ANALYTICS_PROPERTIES
    captured: list[tuple[str, dict[str, Any]]] = []
    with patch.object(
        analytics,
        "track_event",
        side_effect=lambda name, properties: captured.append((name, properties)),
    ):
        analytics.track_tool_call(
            tool_name="zenml_list_resources",
            success=True,
            duration_ms=2,
            size=20,
            resource_type="pipeline",
            operation="list",
            profile="compact",
            outcome="success",
        )
    _, properties = captured[-1]
    assert properties["resource_type"] == "pipeline"
    assert set(properties) <= {
        "tool_name",
        "success",
        "duration_ms",
        "size",
        "resource_type",
        "operation",
        "profile",
        "outcome",
    }


def test_generic_action_analytics_only_for_action_calls() -> None:
    recorded: list[dict[str, Any]] = []

    def generic_stub(resource_type: str) -> dict[str, Any]:
        del resource_type
        return {}

    def action_stub(resource_type: str, action: str) -> dict[str, Any]:
        del resource_type, action
        return {}

    with patch.object(
        server.analytics,
        "track_tool_call",
        side_effect=lambda **properties: recorded.append(properties),
    ):
        for tool_name in (
            "zenml_describe_resources",
            "zenml_list_resources",
            "zenml_get_resource",
            "zenml_create_resource",
            "zenml_update_resource",
            "zenml_delete_resource",
        ):
            generic_stub.__name__ = tool_name
            server.handle_tool_exceptions(generic_stub)("pipeline")

        action_stub.__name__ = "zenml_action_resource"
        action_call = server.handle_tool_exceptions(action_stub)
        action_call("schedule_trigger", "attach")
        action_call("schedule_trigger", "unsupported")

    assert [(event["operation"], event["action"]) for event in recorded] == [
        ("describe", None),
        ("list", None),
        ("get", None),
        ("create", None),
        ("update", None),
        ("delete", None),
        ("action", "attach"),
        ("action", "unknown"),
    ]


def test_upstream_categories_cross_the_mcp_boundary() -> None:
    from zenml.exceptions import (
        DoesNotExistException,
        IllegalOperationError,
        SubscriptionUpgradeRequiredError,
    )

    async def invoke() -> None:
        cases = (
            (SubscriptionUpgradeRequiredError("upgrade"), "FeatureUnavailable"),
            (IllegalOperationError("forbidden"), "PermissionDenied"),
            (DoesNotExistException("missing"), "NotFound"),
        )
        async with Client(server.mcp, mode="auto") as mcp_client:
            for upstream_error, expected_type in cases:
                fake = Recorder()

                def fail(**kwargs: Any) -> Any:
                    del kwargs
                    raise upstream_error

                fake.__dict__["list_schedule_triggers"] = fail
                with patch.object(server, "zenml_client", fake):
                    result = await mcp_client.call_tool(
                        "zenml_list_resources",
                        {
                            "resource_type": "schedule_trigger",
                            "project_id": PROJECT_ID,
                        },
                    )
                assert result.is_error is True
                assert result.structured_content is not None
                assert result.structured_content["error"]["type"] == expected_type

    asyncio.run(invoke())


def test_public_mcp_invocations() -> None:
    async def invoke() -> None:
        fake = Recorder()
        recorded_analytics: list[dict[str, Any]] = []
        with (
            patch.object(server, "zenml_client", fake),
            patch.object(
                server.analytics,
                "track_tool_call",
                side_effect=lambda **properties: recorded_analytics.append(properties),
            ),
        ):
            async with Client(server.mcp, mode="auto") as client:
                describe_result = await client.call_tool("zenml_describe_resources", {})
                assert describe_result.is_error is False
                assert describe_result.structured_content is not None
                assert len(describe_result.structured_content["resources"]) == 30

                list_result = await client.call_tool(
                    "zenml_list_resources",
                    {
                        "resource_type": "user",
                        "page": 1,
                        "size": 3,
                        "filters": {
                            "name": "contains:caller-private-name",
                            "email": "caller-private-config",
                        },
                    },
                )
                assert list_result.is_error is False
                assert list_result.structured_content == {
                    "resource_type": "user",
                    "items": [{"id": "item"}],
                    "total": 41,
                    "page": 7,
                    "size": 13,
                    "effective_scope": {"kind": "global"},
                }

                get_result = await client.call_tool(
                    "zenml_get_resource",
                    {
                        "resource_type": "user",
                        "resource_id": "target",
                        "hydrate": True,
                    },
                )
                assert get_result.is_error is False
                assert get_result.structured_content == {
                    "resource_type": "user",
                    "item": {"id": "target"},
                    "effective_scope": {"kind": "global"},
                }
                assert fake.calls[-1][1]["hydrate"] is True

                error_result = await client.call_tool(
                    "zenml_list_resources",
                    {"resource_type": "raw-secret-token-value"},
                )
                assert error_result.is_error is True
                assert error_result.structured_content is not None
                assert (
                    error_result.structured_content["error"]["type"]
                    == "ValidationError"
                )

        list_event = next(
            event for event in recorded_analytics if event.get("operation") == "list"
        )
        assert list_event["resource_type"] == "user"
        assert list_event["outcome"] == "success"
        assert "caller-private" not in repr(recorded_analytics)
        assert "raw-secret-token-value" not in repr(recorded_analytics)
        assert any(
            event.get("resource_type") == "unknown"
            for event in recorded_analytics
            if event.get("outcome") == "error"
        )

    asyncio.run(invoke())


def main() -> int:
    tests = [
        value for name, value in sorted(globals().items()) if name.startswith("test_")
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"All {len(tests)} resource operation tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
