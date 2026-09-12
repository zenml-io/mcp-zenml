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
"""Credential-free contract tests for every generic resource mutation."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import enum
import io
import json
import os
import socket
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import requests
from mcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from zenml_resource_dispatch import (  # noqa: E402
    ResourceDispatchError,
    ResourceReadOnly,
    create_resource,
    delete_resource,
    update_resource,
)
from zenml_resource_registry import (  # noqa: E402
    RESOURCE_REGISTRY,
    ResourceRegistryError,
    describe_resources,
    validate_mutation_payload,
)

os.environ.setdefault("ZENML_MCP_ANALYTICS_ENABLED", "false")
import zenml_server as server  # noqa: E402

PROJECT = "11111111-1111-4111-8111-111111111111"
TARGET = "22222222-2222-4222-8222-222222222222"
PARENT = "33333333-3333-4333-8333-333333333333"
RELATED = "44444444-4444-4444-8444-444444444444"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
EXPECTED_CALLS = json.loads(
    (FIXTURE_DIR / "resource_mutation_calls.json").read_text(encoding="utf-8")
)
EXPECTED_SCHEMAS = json.loads(
    (FIXTURE_DIR / "resource_mutation_schemas.json").read_text(encoding="utf-8")
)

EXPECTED = {
    "project": "CUD",
    "stack": "CUD",
    "stack_component": "CUD",
    "flavor": "CD",
    "service": "CUD",
    "pipeline": "D",
    "pipeline_run": "D",
    "snapshot": "UD",
    "build": "D",
    "run_template": "CUD",
    "deployment": "D",
    "artifact": "UD",
    "artifact_version": "UD",
    "model": "CUD",
    "model_version": "CUD",
    "tag": "CUD",
    "service_connector": "CUD",
    "code_repository": "CUD",
    "webhook": "CUD",
    "schedule_trigger": "CUD",
    "platform_event_trigger": "CUD",
    "webhook_trigger": "CUD",
    "hook_invocation": "D",
}

IDENTIFIER_KEY = {
    "project": "name_id_or_prefix",
    "stack": "name_id_or_prefix",
    "stack_component": "name_id_or_prefix",
    "flavor": "name_id_or_prefix",
    "service": "id",
    "pipeline": "name_id_or_prefix",
    "pipeline_run": "name_id_or_prefix",
    "snapshot": "name_id_or_prefix",
    "build": "id_or_prefix",
    "run_template": "name_id_or_prefix",
    "deployment": "name_id_or_prefix",
    "artifact": "name_id_or_prefix",
    "artifact_version": "name_id_or_prefix",
    "model": "model_name_or_id",
    "model_version": "version_name_or_id",
    "tag": "tag_name_or_id",
    "service_connector": "name_id_or_prefix",
    "code_repository": "name_id_or_prefix",
    "webhook": "name_id_or_prefix",
    "schedule_trigger": "trigger_name_id_or_prefix",
    "platform_event_trigger": "trigger_name_id_or_prefix",
    "webhook_trigger": "trigger_name_id_or_prefix",
    "hook_invocation": "hook_invocation_id",
}


class Recorder:
    def __init__(self) -> None:
        self.active_project = SimpleNamespace(id=uuid.UUID(PROJECT))
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str):
        def call(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            if name in {"create_service_connector", "update_service_connector"}:
                return self._item(name, kwargs), None
            if name.startswith("delete_"):
                return None
            if name == "create_webhook":
                return {
                    "id": TARGET,
                    "body": {
                        "project_id": PROJECT,
                        "secret": "issued-once-marker",
                    },
                }
            return self._item(name, kwargs)

        return call

    def _item(self, method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        identifier = (
            kwargs["model_version_name_or_number_or_id"]
            if "model_version_name_or_number_or_id" in kwargs
            else kwargs["version_name_or_id"]
            if "version_name_or_id" in kwargs
            else next(
                (
                    value
                    for key, value in kwargs.items()
                    if key
                    in {
                        "name_id_or_prefix",
                        "model_name_or_id",
                        "model_version_name_or_number_or_id",
                        "tag_name_or_id",
                        "trigger_name_id_or_prefix",
                        "hook_invocation_id",
                        "id_or_prefix",
                    }
                ),
                TARGET,
            )
        )
        item: dict[str, Any] = {"id": str(identifier)}
        global_reads = {
            "get_project",
            "get_stack",
            "get_stack_component",
            "get_flavor",
            "get_tag",
            "get_service_connector",
        }
        if method not in global_reads:
            item["body"] = {"project_id": PROJECT}
        if method == "get_artifact_version":
            item["resources"] = {"artifact_id": PARENT}
        if method == "get_model_version":
            item["body"] = {"project_id": PROJECT, "model_id": PARENT}
        if method == "get_platform_event_trigger":
            item["body"] = {
                "project_id": PROJECT,
                "source_type": "pipeline",
                "source_id": RELATED,
            }
        return item


def _payload(resource_type: str, operation: str) -> dict[str, Any] | None:
    payloads: dict[tuple[str, str], dict[str, Any]] = {
        ("project", "create"): {"name": "project", "description": "description"},
        ("project", "update"): {"name": "renamed"},
        ("stack", "create"): {
            "name": "stack",
            "components": {"orchestrator": RELATED, "artifact_store": TARGET},
        },
        ("stack", "update"): {"description": "updated"},
        ("stack_component", "create"): {
            "name": "component",
            "flavor": "default",
            "component_type": "orchestrator",
            "configuration": {},
        },
        ("stack_component", "update"): {"configuration": {"key": None}},
        ("flavor", "create"): {
            "source": "zenml.orchestrators.base_orchestrator:BaseOrchestratorFlavor",
            "component_type": "orchestrator",
        },
        ("service", "create"): {
            "config": {"name": "service"},
            "service_type": {"type": "model-serving", "flavor": "custom"},
        },
        ("service", "update"): {"name": "renamed"},
        ("snapshot", "update"): {"description": "", "replace": False},
        ("run_template", "create"): {"name": "template", "snapshot_id": RELATED},
        ("run_template", "update"): {"hidden": False},
        ("deployment", "delete"): {"force": True, "timeout": 30},
        ("artifact", "update"): {"has_custom_name": False},
        ("artifact_version", "update"): {"add_tags": ["reviewed"]},
        ("artifact_version", "delete"): {
            "delete_metadata": False,
            "delete_from_artifact_store": True,
        },
        ("model", "create"): {"name": "model", "save_models_to_registry": False},
        ("model", "update"): {"description": "", "save_models_to_registry": False},
        ("model_version", "create"): {"name": "v1"},
        ("model_version", "update"): {"stage": "staging", "force": False},
        ("tag", "create"): {"name": "tag", "exclusive": False},
        ("tag", "update"): {"exclusive": False},
        ("service_connector", "create"): {
            "name": "connector",
            "connector_type": "docker",
            "configuration": {"password": "credential-marker"},
        },
        ("service_connector", "update"): {
            "expiration_seconds": 0,
            "labels": {"old": None},
        },
        ("code_repository", "create"): {
            "name": "repo",
            "source": "zenml.code_repositories.base_code_repository:BaseCodeRepository",
            "config": {},
        },
        ("code_repository", "update"): {"config": {"old": None}},
        ("webhook", "create"): {"name": "webhook", "webhook_type": "generic"},
        ("webhook", "update"): {"active": False},
        ("schedule_trigger", "create"): {
            "name": "schedule",
            "interval": 60,
            "start_time": "2026-09-12T12:00:00Z",
        },
        ("schedule_trigger", "update"): {"active": False},
        ("platform_event_trigger", "create"): {
            "name": "event",
            "source_type": "pipeline_snapshot",
            "source_id": RELATED,
            "target_events": ["created"],
        },
        ("platform_event_trigger", "update"): {"target_events": ["updated"]},
        ("webhook_trigger", "create"): {
            "name": "hook",
            "webhook_id": RELATED,
            "configuration": {},
        },
        ("webhook_trigger", "update"): {"configuration": {}},
    }
    return payloads.get((resource_type, operation))


def _kwargs(resource_type: str, operation: str) -> dict[str, Any]:
    spec = RESOURCE_REGISTRY[resource_type]
    result: dict[str, Any] = {"payload": _payload(resource_type, operation)}
    if spec.scope == "project" or resource_type in {
        "service",
        "run_template",
        "model",
        "code_repository",
        "webhook",
    }:
        result["project_id"] = PROJECT
    if resource_type == "artifact_version":
        result["artifact_id"] = PARENT
    if resource_type == "model_version":
        result["model_id"] = PARENT
    if resource_type == "stack_component" and operation != "create":
        result["component_type"] = "orchestrator"
    return result


def _normalize_call_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return _normalize_call_value(value.model_dump(mode="json"))
    if all(hasattr(value, field) for field in ("module", "attribute", "type")):
        return {
            "module": value.module,
            "attribute": value.attribute,
            "type": _normalize_call_value(value.type),
        }
    if isinstance(value, dict):
        return {
            _normalize_call_value(key): _normalize_call_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_call_value(item) for item in value]
    return value


def _valid_schema_value(schema: dict[str, Any]) -> Any:
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return _valid_schema_value(schema["anyOf"][0])
    schema_type = schema.get("type")
    if schema_type == "string":
        if schema.get("format") == "uuid":
            return RELATED
        if schema.get("format") == "date-time":
            return "2026-09-12T12:00:00Z"
        return "value"
    if schema_type == "boolean":
        return False
    if schema_type in {"integer", "number"}:
        return max(schema.get("minimum", 1), 1)
    if schema_type == "array":
        return [_valid_schema_value(schema.get("items", {}))]
    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", []) or list(properties)[:1]
        if properties:
            return {field: _valid_schema_value(properties[field]) for field in required}
        if isinstance(schema.get("additionalProperties"), dict):
            return {"orchestrator": _valid_schema_value(schema["additionalProperties"])}
        return {}
    return "value"


def _payload_with_valid_field(
    resource_type: str,
    operation: str,
    field: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(_payload(resource_type, operation) or {})
    if field in payload:
        return payload
    if resource_type == "schedule_trigger" and field in {
        "cron_expression",
        "interval",
        "run_once_start_time",
    }:
        for scheduling_field in (
            "cron_expression",
            "interval",
            "run_once_start_time",
        ):
            payload.pop(scheduling_field, None)
    payload[field] = _valid_schema_value(schema)
    if resource_type == "platform_event_trigger":
        if field == "source_type":
            payload["source_id"] = RELATED
        elif field == "source_id":
            payload["source_type"] = "pipeline"
    return payload


def _invalid_schema_values(schema: dict[str, Any]) -> list[Any]:
    if "anyOf" in schema:
        branch_types = {branch.get("type") for branch in schema["anyOf"]}
        if branch_types == {"string", "null"}:
            return [{}, []]
        if branch_types == {"string", "array"}:
            return [None, {}, "not-a-uuid", [], ["not-a-uuid"]]
        return [{}]
    schema_type = schema.get("type")
    values: list[Any] = {
        "string": [{}],
        "boolean": ["false"],
        "integer": ["1"],
        "number": ["1"],
        "array": [{}],
        "object": [[]],
    }.get(schema_type, [None])
    if schema_type == "string":
        if schema.get("format") == "uuid":
            values.append("not-a-uuid")
        if schema.get("format") == "date-time":
            values.append("not-a-date")
        if schema.get("minLength", 0) > 0:
            values.append("")
        if "enum" in schema:
            values.append("not-an-enum-value")
    elif schema_type in {"integer", "number"} and "minimum" in schema:
        values.append(schema["minimum"] - 1)
        if "maximum" in schema:
            values.append(schema["maximum"] + 1)
    elif schema_type == "array":
        if schema.get("minItems", 0) > 0:
            values.append([])
        for invalid_item in _invalid_schema_values(schema.get("items", {})):
            values.append([invalid_item])
    elif schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        valid_object = {
            field: _valid_schema_value(field_schema)
            for field, field_schema in properties.items()
        }
        for required_field in required:
            missing = dict(valid_object)
            missing.pop(required_field, None)
            values.append(missing)
        for field, field_schema in properties.items():
            for invalid_item in _invalid_schema_values(field_schema):
                invalid_object = dict(valid_object)
                invalid_object[field] = invalid_item
                values.append(invalid_object)
        additional = schema.get("additionalProperties")
        if additional is False:
            values.append({**valid_object, "not_allowed": "value"})
        elif isinstance(additional, dict):
            for invalid_item in _invalid_schema_values(additional):
                values.append({"nested": invalid_item})
    return values


def test_registry_has_exact_mutation_inventory() -> None:
    abbreviation = {"create": "C", "update": "U", "delete": "D"}
    actual = {
        name: "".join(
            abbreviation[operation]
            for operation in ("create", "update", "delete")
            if operation in spec.operations
        )
        for name, spec in RESOURCE_REGISTRY.items()
        if any(operation in spec.operations for operation in abbreviation)
    }
    assert actual == EXPECTED


def test_every_payload_field_has_a_strict_runtime_contract() -> None:
    for resource_type, operations in EXPECTED.items():
        for short in operations:
            operation = {"C": "create", "U": "update", "D": "delete"}[short]
            mutation = RESOURCE_REGISTRY[resource_type].mutations[operation]
            expected_schema = EXPECTED_SCHEMAS[f"{resource_type}.{operation}"]
            assert dict(mutation.payload_properties) == expected_schema["properties"]
            assert list(mutation.required_payload) == expected_schema["required"]
            assert (
                describe_resources(resource_type, operation)["input_schema"]
                == (expected_schema["input_schema"])
            )
            valid = dict(_payload(resource_type, operation) or {})
            assert validate_mutation_payload(resource_type, operation, valid) == valid

            for required in expected_schema["required"]:
                missing = dict(valid)
                missing.pop(required)
                try:
                    validate_mutation_payload(resource_type, operation, missing)
                except ResourceRegistryError:
                    pass
                else:
                    raise AssertionError(
                        f"{resource_type}.{operation} accepted missing {required}"
                    )

            for field, schema in expected_schema["properties"].items():
                valid_field_payload = _payload_with_valid_field(
                    resource_type, operation, field, schema
                )
                assert (
                    validate_mutation_payload(
                        resource_type, operation, valid_field_payload
                    )
                    == valid_field_payload
                )
                for wrong_value in _invalid_schema_values(schema):
                    wrong = dict(valid)
                    wrong[field] = wrong_value
                    try:
                        validate_mutation_payload(resource_type, operation, wrong)
                    except ResourceRegistryError:
                        pass
                    else:
                        raise AssertionError(
                            f"{resource_type}.{operation}.{field} accepted "
                            f"invalid value {wrong_value!r}"
                        )

            unknown = {**valid, "not_a_public_field": "x"}
            try:
                validate_mutation_payload(resource_type, operation, unknown)
            except ResourceRegistryError:
                pass
            else:
                raise AssertionError(
                    f"{resource_type}.{operation} accepted an unknown field"
                )

    invalid_boundaries = (
        ("tag", "create", {"name": "tag", "color": "transparent"}),
        (
            "model_version",
            "update",
            {"stage": "latest", "force": False},
        ),
        (
            "schedule_trigger",
            "create",
            {"name": "schedule", "interval": 59},
        ),
        (
            "service_connector",
            "create",
            {"name": "connector", "connector_type": "docker", "expires_at": "soon"},
        ),
        (
            "run_template",
            "create",
            {"name": "template", "snapshot_id": "not-a-uuid"},
        ),
        (
            "service_connector",
            "create",
            {
                "name": "connector",
                "connector_type": "docker",
                "expiration_seconds": -1,
            },
        ),
        ("snapshot", "update", {"add_tags": [], "replace": False}),
        ("stack_component", "update", {"configuration": {}, "disconnect": False}),
    )
    for resource_type, operation, payload in invalid_boundaries:
        try:
            validate_mutation_payload(resource_type, operation, payload)
        except ResourceRegistryError:
            pass
        else:
            raise AssertionError(
                f"{resource_type}.{operation} accepted invalid boundary payload"
            )


def test_every_mutation_binds_one_write_with_exact_ids() -> None:
    for resource_type, operations in EXPECTED.items():
        for short in operations:
            operation = {"C": "create", "U": "update", "D": "delete"}[short]
            client = Recorder()
            kwargs = _kwargs(resource_type, operation)
            if operation == "create":
                result = create_resource(client, resource_type, **kwargs)
            elif operation == "update":
                result = update_resource(client, resource_type, TARGET, **kwargs)
            else:
                result = delete_resource(client, resource_type, TARGET, **kwargs)
            write_calls = [
                call
                for call in client.calls
                if call[0].startswith(operation)
                or (operation == "delete" and call[0] == "delete_trigger")
            ]
            assert len(write_calls) == 1, (resource_type, operation, client.calls)
            method, write_kwargs = write_calls[0]
            expected_method = (
                "delete_trigger"
                if operation == "delete"
                and resource_type
                in {
                    "schedule_trigger",
                    "platform_event_trigger",
                    "webhook_trigger",
                }
                else f"{operation}_{resource_type}"
            )
            assert method == expected_method, (resource_type, operation, method)
            assert {
                "method": method,
                "kwargs": _normalize_call_value(write_kwargs),
            } == EXPECTED_CALLS[f"{resource_type}.{operation}"], (
                resource_type,
                operation,
                write_kwargs,
            )
            assert result["outcome"] == "completed"
            assert result["operation"] == operation
            if operation != "create":
                assert result["resource_id"] == TARGET, (
                    resource_type,
                    operation,
                    result,
                )
                identifier_key = (
                    "trigger_id"
                    if expected_method == "delete_trigger"
                    else "model_version_id"
                    if resource_type == "model_version" and operation == "delete"
                    else "name_id_or_prefix"
                    if resource_type == "service" and operation == "delete"
                    else IDENTIFIER_KEY[resource_type]
                )
                assert str(write_kwargs[identifier_key]) == TARGET
            if (
                resource_type
                in {"schedule_trigger", "platform_event_trigger", "webhook_trigger"}
                and operation == "delete"
            ):
                assert write_calls[0][1] == {
                    "trigger_id": uuid.UUID(TARGET),
                    "soft": True,
                }
            if resource_type == "artifact_version" and operation == "delete":
                assert write_kwargs == {
                    "name_id_or_prefix": uuid.UUID(TARGET),
                    "delete_metadata": False,
                    "delete_from_artifact_store": True,
                    "project": PROJECT,
                    "server_side": True,
                }
            if resource_type == "stack" and operation == "delete":
                assert write_kwargs["recursive"] is False
            if resource_type == "service_connector" and operation == "create":
                assert {
                    "auto_configure": write_kwargs["auto_configure"],
                    "verify": write_kwargs["verify"],
                    "list_resources": write_kwargs["list_resources"],
                    "register": write_kwargs["register"],
                } == {
                    "auto_configure": False,
                    "verify": False,
                    "list_resources": False,
                    "register": True,
                }
            if resource_type == "service_connector" and operation == "update":
                assert write_kwargs["expiration_seconds"] == 0
                assert write_kwargs["labels"] == {"old": None}
                assert write_kwargs["verify"] is False
                assert write_kwargs["list_resources"] is False
                assert write_kwargs["update"] is True


def test_deployment_delete_timeout_accepts_the_upper_bound() -> None:
    payload = {"timeout": 300}
    assert validate_mutation_payload("deployment", "delete", payload) == payload

    client = Recorder()
    delete_resource(
        client,
        "deployment",
        TARGET,
        project_id=PROJECT,
        payload=payload,
    )
    write = next(call for call in client.calls if call[0] == "delete_deployment")
    assert write[1]["timeout"] == 300

    before = len(client.calls)
    try:
        delete_resource(
            client,
            "deployment",
            TARGET,
            project_id=PROJECT,
            payload={"timeout": 301},
        )
    except ResourceDispatchError:
        pass
    else:
        raise AssertionError("deployment delete accepted a timeout above 300 seconds")
    assert len(client.calls) == before


def test_constrained_create_schemas_match_validation_and_examples() -> None:
    service_schema = describe_resources("service", "create")["input_schema"][
        "properties"
    ]["payload"]["properties"]["config"]
    assert service_schema["anyOf"] == [
        {"type": "object", "required": ["name"]},
        {"type": "object", "required": ["model_name"]},
    ]
    for config in ({"name": "service"}, {"model_name": "model"}):
        payload = {
            "config": config,
            "service_type": {"type": "model-serving", "flavor": "custom"},
        }
        assert validate_mutation_payload("service", "create", payload) == payload
    for config in ({}, {"description": "missing identity"}, {"name": ""}):
        payload = {
            "config": config,
            "service_type": {"type": "model-serving", "flavor": "custom"},
        }
        try:
            validate_mutation_payload("service", "create", payload)
        except ResourceRegistryError:
            pass
        else:
            raise AssertionError(f"service.create accepted invalid config {config!r}")

    schedule_schema = describe_resources("schedule_trigger", "create")["input_schema"][
        "properties"
    ]["payload"]
    schedule_branches = schedule_schema["oneOf"]
    assert [branch["required"] for branch in schedule_branches] == [
        ["cron_expression"],
        ["interval", "start_time"],
        ["run_once_start_time"],
    ]
    assert [
        [excluded["required"] for excluded in branch["not"]["anyOf"]]
        for branch in schedule_branches
    ] == [
        [["interval"], ["run_once_start_time"]],
        [["cron_expression"], ["run_once_start_time"]],
        [["cron_expression"], ["interval"]],
    ]
    for schedule in (
        {"cron_expression": "0 * * * *"},
        {"interval": 60, "start_time": "2026-09-12T12:00:00Z"},
        {"run_once_start_time": "2026-09-12T12:00:00Z"},
    ):
        payload = {"name": "schedule", **schedule}
        assert (
            validate_mutation_payload("schedule_trigger", "create", payload) == payload
        )
    for schedule in (
        {},
        {"interval": 60},
        {"cron_expression": "0 * * * *", "interval": 60},
        {"run_once_start_time": "2026-09-12T12:00:00Z", "interval": 60},
        {"cron_expression": "0 * * * *", "run_once_start_time": "2026-09-12T12:00:00Z"},
    ):
        payload = {"name": "schedule", **schedule}
        try:
            validate_mutation_payload("schedule_trigger", "create", payload)
        except ResourceRegistryError:
            pass
        else:
            raise AssertionError(
                f"schedule_trigger.create accepted invalid schedule {schedule!r}"
            )

    for resource_type in ("service", "schedule_trigger"):
        example_payload = describe_resources(resource_type, "create")["example"][
            "payload"
        ]
        assert (
            validate_mutation_payload(resource_type, "create", example_payload)
            == example_payload
        )


def test_false_zero_and_clear_values_reach_the_sdk() -> None:
    cases = (
        ("snapshot", "update", "description", ""),
        ("snapshot", "update", "replace", False),
        ("run_template", "update", "hidden", False),
        ("artifact", "update", "has_custom_name", False),
        ("model", "update", "description", ""),
        ("model", "update", "save_models_to_registry", False),
        ("model_version", "update", "force", False),
        ("tag", "update", "exclusive", False),
        ("webhook", "update", "active", False),
        ("schedule_trigger", "update", "active", False),
    )
    for resource_type, operation, field, expected in cases:
        client = Recorder()
        result = update_resource(
            client,
            resource_type,
            TARGET,
            **_kwargs(resource_type, operation),
        )
        assert result["outcome"] == "completed"
        write = next(call for call in client.calls if call[0].startswith("update_"))
        assert write[1][field] == expected, (resource_type, field)

    from zenml.enums import SourceType

    client = Recorder()
    update_resource(
        client,
        "platform_event_trigger",
        TARGET,
        project_id=PROJECT,
        payload={"source_type": "pipeline_snapshot", "source_id": RELATED},
    )
    write = next(
        call for call in client.calls if call[0] == "update_platform_event_trigger"
    )
    assert write[1]["source_type"] is SourceType.PIPELINE_SNAPSHOT
    assert write[1]["source_id"] == uuid.UUID(RELATED)


def test_validation_and_read_only_precede_sdk_access() -> None:
    client = Recorder()
    invalid = [
        lambda: update_resource(
            client, "model", "not-a-uuid", payload={"name": "x"}, project_id=PROJECT
        ),
        lambda: update_resource(
            client, "model", TARGET, payload={"unknown": "x"}, project_id=PROJECT
        ),
        lambda: create_resource(client, "pipeline", payload={}, project_id=PROJECT),
        lambda: delete_resource(
            client,
            "artifact_version",
            TARGET,
            payload={"delete_metadata": False, "delete_from_artifact_store": False},
            project_id=PROJECT,
            artifact_id=PARENT,
        ),
        lambda: create_resource(
            client,
            "schedule_trigger",
            payload={
                "name": "bad",
                "interval": 59,
                "start_time": "2026-09-12T12:00:00Z",
            },
            project_id=PROJECT,
        ),
        lambda: update_resource(
            client,
            "service",
            TARGET,
            payload={"service_source": "malicious.module:Service"},
            project_id=PROJECT,
        ),
        lambda: create_resource(
            client,
            "platform_event_trigger",
            payload={
                "name": "bad",
                "source_type": "pipeline",
                "source_id": RELATED,
                "target_events": [],
            },
            project_id=PROJECT,
        ),
    ]
    for call in invalid:
        before = len(
            [
                name
                for name, _ in client.calls
                if name.startswith(("create_", "update_", "delete_"))
            ]
        )
        try:
            call()
        except ResourceDispatchError:
            pass
        else:
            raise AssertionError("invalid mutation reached the SDK")
        after = len(
            [
                name
                for name, _ in client.calls
                if name.startswith(("create_", "update_", "delete_"))
            ]
        )
        assert after == before

    for call in (
        lambda: create_resource(
            client,
            "project",
            payload={"name": "x", "description": "x"},
            read_only=True,
        ),
        lambda: update_resource(
            client,
            "model",
            TARGET,
            payload={"name": "x"},
            project_id=PROJECT,
            read_only=True,
        ),
        lambda: delete_resource(
            client,
            "model",
            TARGET,
            project_id=PROJECT,
            read_only=True,
        ),
    ):
        before = len(client.calls)
        try:
            call()
        except ResourceReadOnly:
            pass
        else:
            raise AssertionError("read-only mutation was allowed")
        assert len(client.calls) == before


def test_import_allowlist_requires_a_package_boundary() -> None:
    payload = {
        "name": "repo",
        "source": "acme.plugins_evil:Repository",
        "config": {},
    }
    with patch.dict(os.environ, {"ZENML_MCP_ALLOWED_IMPORT_PREFIXES": "acme.plugins"}):
        client = Recorder()
        try:
            create_resource(
                client, "code_repository", payload=payload, project_id=PROJECT
            )
        except ResourceDispatchError:
            pass
        else:
            raise AssertionError("sibling package bypassed the import allowlist")
        assert not client.calls


def test_project_parent_and_exact_target_mismatches_block_writes() -> None:
    cases: list[tuple[str, Any]] = []

    wrong_project = Recorder()

    def get_model(**kwargs: Any) -> dict[str, Any]:
        wrong_project.calls.append(("get_model", kwargs))
        return {"id": TARGET, "body": {"project_id": RELATED}}

    setattr(wrong_project, "get_model", get_model)
    cases.append(
        (
            "project",
            lambda: update_resource(
                wrong_project,
                "model",
                TARGET,
                project_id=PROJECT,
                payload={"description": "blocked"},
            ),
        )
    )

    wrong_parent = Recorder()

    def get_version(**kwargs: Any) -> dict[str, Any]:
        wrong_parent.calls.append(("get_artifact_version", kwargs))
        return {
            "id": TARGET,
            "body": {"project_id": PROJECT},
            "resources": {"artifact_id": RELATED},
        }

    setattr(wrong_parent, "get_artifact_version", get_version)
    cases.append(
        (
            "parent",
            lambda: update_resource(
                wrong_parent,
                "artifact_version",
                TARGET,
                project_id=PROJECT,
                artifact_id=PARENT,
                payload={"add_tags": ["blocked"]},
            ),
        )
    )

    wrong_target = Recorder()

    def get_tag(**kwargs: Any) -> dict[str, Any]:
        wrong_target.calls.append(("get_tag", kwargs))
        return {"id": RELATED}

    setattr(wrong_target, "get_tag", get_tag)
    cases.append(
        (
            "target",
            lambda: update_resource(
                wrong_target, "tag", TARGET, payload={"name": "blocked"}
            ),
        )
    )

    for label, call in cases:
        try:
            call()
        except ResourceDispatchError:
            pass
        else:
            raise AssertionError(f"{label} mismatch was accepted")
    assert not any(name.startswith("update_") for name, _ in wrong_project.calls)
    assert not any(name.startswith("update_") for name, _ in wrong_parent.calls)
    assert not any(name.startswith("update_") for name, _ in wrong_target.calls)


def test_every_related_create_and_update_branch_blocks_mismatches() -> None:
    class MismatchRecorder(Recorder):
        def __init__(self, mismatch_method: str) -> None:
            super().__init__()
            self.mismatch_method = mismatch_method

        def _item(self, method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
            item = super()._item(method, kwargs)
            if method == self.mismatch_method:
                item["id"] = TARGET
            return item

    cases: tuple[tuple[str, str, Any], ...] = (
        (
            "run-template snapshot",
            "get_snapshot",
            lambda client: create_resource(
                client,
                "run_template",
                payload=_payload("run_template", "create"),
                project_id=PROJECT,
            ),
        ),
        (
            "model-version model",
            "get_model",
            lambda client: create_resource(
                client,
                "model_version",
                payload=_payload("model_version", "create"),
                project_id=PROJECT,
                model_id=PARENT,
            ),
        ),
        (
            "webhook-trigger webhook",
            "get_webhook",
            lambda client: create_resource(
                client,
                "webhook_trigger",
                payload=_payload("webhook_trigger", "create"),
                project_id=PROJECT,
            ),
        ),
        (
            "platform-event source",
            "get_snapshot",
            lambda client: create_resource(
                client,
                "platform_event_trigger",
                payload=_payload("platform_event_trigger", "create"),
                project_id=PROJECT,
            ),
        ),
        (
            "service model version",
            "get_model_version",
            lambda client: create_resource(
                client,
                "service",
                payload={
                    **(_payload("service", "create") or {}),
                    "model_version_id": RELATED,
                },
                project_id=PROJECT,
            ),
        ),
        (
            "service update model version",
            "get_model_version",
            lambda client: update_resource(
                client,
                "service",
                TARGET,
                payload={"model_version_id": RELATED},
                project_id=PROJECT,
            ),
        ),
        (
            "platform-event supplied update source",
            "get_pipeline",
            lambda client: update_resource(
                client,
                "platform_event_trigger",
                TARGET,
                payload={"source_type": "pipeline", "source_id": RELATED},
                project_id=PROJECT,
            ),
        ),
        (
            "platform-event inherited update source",
            "get_pipeline",
            lambda client: update_resource(
                client,
                "platform_event_trigger",
                TARGET,
                payload={"target_events": ["updated"]},
                project_id=PROJECT,
            ),
        ),
        (
            "stack component bindings",
            "get_stack_component",
            lambda client: create_resource(
                client, "stack", payload=_payload("stack", "create")
            ),
        ),
        (
            "stack component update",
            "get_stack_component",
            lambda client: update_resource(
                client,
                "stack",
                TARGET,
                payload={"component_updates": {"orchestrator": RELATED}},
            ),
        ),
        (
            "component connector",
            "get_service_connector",
            lambda client: update_resource(
                client,
                "stack_component",
                TARGET,
                payload={"connector_id": RELATED},
                component_type="orchestrator",
            ),
        ),
    )
    for label, mismatch_method, call in cases:
        client = MismatchRecorder(mismatch_method)
        try:
            call(client)
        except ResourceDispatchError:
            pass
        else:
            raise AssertionError(f"{label} mismatch was accepted")
        assert any(name == mismatch_method for name, _ in client.calls), label
        assert not any(
            name.startswith(("create_", "update_")) for name, _ in client.calls
        ), label

    client = Recorder()
    try:
        create_resource(
            client,
            "model",
            payload={"name": "wrong-project"},
            project_id=RELATED,
        )
    except ResourceDispatchError:
        pass
    else:
        raise AssertionError("project-active create accepted a different project")
    assert client.calls == []


def test_webhook_secret_is_returned_once_and_connector_config_is_redacted() -> None:
    client = Recorder()
    webhook = create_resource(client, "webhook", **_kwargs("webhook", "create"))
    assert webhook["issued_secret"] == "issued-once-marker"
    assert "secret" not in webhook["item"]
    connector = create_resource(
        client, "service_connector", **_kwargs("service_connector", "create")
    )
    serialized = repr(connector)
    assert "credential-marker" not in serialized
    assert "configuration" not in serialized


def test_post_dispatch_connection_loss_is_unknown_and_not_retried() -> None:
    client = Recorder()
    attempts = 0

    def lose_response(**kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise requests.ConnectionError("remote peer closed after request")

    setattr(client, "create_webhook", lose_response)
    result = create_resource(client, "webhook", **_kwargs("webhook", "create"))
    assert attempts == 1
    assert result["outcome"] == "unknown"
    assert result["error"]["type"] == "UnknownOutcome"
    assert "cannot be recovered" in result["reconciliation"]["note"]

    async def invoke() -> None:
        fake = Recorder()
        setattr(fake, "create_webhook", lose_response)
        analytics_events: list[dict[str, Any]] = []
        with (
            patch.object(server, "zenml_client", fake),
            patch.object(
                server.analytics,
                "track_tool_call",
                side_effect=lambda **event: analytics_events.append(event),
            ),
        ):
            async with Client(server.mcp, mode="auto") as mcp_client:
                response = await mcp_client.call_tool(
                    "zenml_create_resource",
                    {
                        "resource_type": "webhook",
                        **_kwargs("webhook", "create"),
                    },
                )
                assert response.is_error is True
                assert response.structured_content is not None
                assert response.structured_content["outcome"] == "unknown"
        mutation_event = next(
            event for event in analytics_events if event.get("operation") == "create"
        )
        assert mutation_event["outcome"] == "unknown"
        assert mutation_event["success"] is False

    asyncio.run(invoke())
    assert attempts == 2

    preconnect = Recorder()

    def fail_to_connect(**kwargs: Any) -> None:
        raise requests.ConnectTimeout("connection was never established")

    setattr(preconnect, "create_webhook", fail_to_connect)
    try:
        create_resource(preconnect, "webhook", **_kwargs("webhook", "create"))
    except requests.ConnectTimeout:
        pass
    else:
        raise AssertionError("pre-connect failure was reported as unknown outcome")


def test_unknown_nested_mutations_return_complete_reconciliation_inputs() -> None:
    client = Recorder()

    def lose_response(**kwargs: Any) -> None:
        del kwargs
        raise requests.ConnectionError("remote peer closed after request")

    setattr(client, "update_artifact_version", lose_response)
    result = update_resource(
        client,
        "artifact_version",
        TARGET,
        payload={"add_tags": ["reviewed"]},
        project_id=PROJECT,
        artifact_id=PARENT,
    )
    assert result["outcome"] == "unknown"
    assert result["reconciliation"] == {
        "operation": "get",
        "resource_type": "artifact_version",
        "resource_id": TARGET,
        "project_id": PROJECT,
        "artifact_id": PARENT,
    }

    setattr(client, "create_model_version", lose_response)
    result = create_resource(
        client,
        "model_version",
        payload={"name": "v1"},
        project_id=PROJECT,
        model_id=PARENT,
    )
    assert result["outcome"] == "unknown"
    assert result["reconciliation"] == {
        "operation": "list",
        "resource_type": "model_version",
        "filters": {"name": "v1", "model_id": PARENT},
        "project_id": PROJECT,
        "note": "Confirm the mutation by reading the resource; do not repeat it automatically.",
    }


def test_real_zero_retry_session_sends_one_request_after_dropped_response() -> None:
    committed: list[bytes] = []

    class DropResponseHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            committed.append(self.rfile.read(length))
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), DropResponseHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    class HTTPClient(Recorder):
        def __init__(self) -> None:
            super().__init__()
            self.zen_store = SimpleNamespace(session=requests.Session())

        def create_webhook(self, **kwargs: Any) -> Any:
            return self.zen_store.session.post(
                f"http://127.0.0.1:{httpd.server_port}/webhooks",
                json=kwargs,
                timeout=2,
            ).json()

    try:
        client = HTTPClient()
        server._configure_zero_retry_rest_session(client)
        result = create_resource(client, "webhook", **_kwargs("webhook", "create"))
        assert result["outcome"] == "unknown"
        assert result["error"]["type"] == "UnknownOutcome"
        assert len(committed) == 1
        assert b"webhook" in committed[0]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_provider_errors_do_not_expose_payloads_or_credentials() -> None:
    async def invoke() -> None:
        fake = Recorder()

        def reject(**kwargs: Any) -> None:
            raise ValueError("provider rejected credential-marker")

        setattr(fake, "create_webhook", reject)
        stderr = io.StringIO()
        with (
            patch.object(server, "zenml_client", fake),
            contextlib.redirect_stderr(stderr),
        ):
            async with Client(server.mcp, mode="auto") as mcp_client:
                response = await mcp_client.call_tool(
                    "zenml_create_resource",
                    {
                        "resource_type": "webhook",
                        "project_id": PROJECT,
                        "payload": {
                            "name": "webhook",
                            "webhook_type": "generic",
                            "secret": "credential-marker",
                        },
                    },
                )
        assert response.is_error is True
        assert "credential-marker" not in repr(response)
        assert "credential-marker" not in stderr.getvalue()

    asyncio.run(invoke())


def test_retained_trigger_obeys_read_only_policy_before_client_lookup() -> None:
    async def invoke() -> None:
        with (
            patch.dict(os.environ, {"ZENML_MCP_WRITE_POLICY": "read_only"}),
            patch.object(
                server,
                "get_zenml_client",
                side_effect=AssertionError("client accessed"),
            ),
        ):
            async with Client(server.mcp, mode="auto") as mcp_client:
                for tool, arguments in (
                    ("trigger_pipeline", {"pipeline_name_or_id": "pipeline"}),
                    (
                        "zenml_create_resource",
                        {
                            "resource_type": "project",
                            "payload": {"name": "x", "description": "x"},
                        },
                    ),
                    (
                        "zenml_update_resource",
                        {
                            "resource_type": "model",
                            "resource_id": TARGET,
                            "project_id": PROJECT,
                            "payload": {"name": "x"},
                        },
                    ),
                    (
                        "zenml_delete_resource",
                        {
                            "resource_type": "model",
                            "resource_id": TARGET,
                            "project_id": PROJECT,
                        },
                    ),
                ):
                    result = await mcp_client.call_tool(tool, arguments)
                    assert result.is_error is True
                    assert result.structured_content is not None
                    assert (
                        result.structured_content["error"]["type"] == "PermissionDenied"
                    )

    asyncio.run(invoke())

    with patch.dict(
        os.environ,
        {"ZENML_MCP_WRITE_POLICY": "invalid", "ZENML_MCP_READ_ONLY": "false"},
    ):
        try:
            create_resource(
                Recorder(),
                "project",
                payload={"name": "x", "description": "x"},
            )
        except ResourceReadOnly:
            pass
        else:
            raise AssertionError("invalid write policy did not fail closed")

    with patch.dict(
        os.environ,
        {"ZENML_MCP_READ_ONLY": "invalid", "ZENML_MCP_WRITE_POLICY": "read_write"},
    ):
        try:
            create_resource(
                Recorder(),
                "project",
                payload={"name": "x", "description": "x"},
            )
        except ResourceReadOnly:
            pass
        else:
            raise AssertionError("invalid legacy read-only flag did not fail closed")


def test_every_mutation_pair_through_mcp() -> None:
    async def invoke() -> None:
        fake = Recorder()
        with patch.object(server, "zenml_client", fake):
            async with Client(server.mcp, mode="auto") as mcp_client:
                for resource_type, operations in EXPECTED.items():
                    for short in operations:
                        operation = {"C": "create", "U": "update", "D": "delete"}[short]
                        arguments = {
                            "resource_type": resource_type,
                            **_kwargs(resource_type, operation),
                        }
                        if operation != "create":
                            arguments["resource_id"] = TARGET
                        result = await mcp_client.call_tool(
                            f"zenml_{operation}_resource", arguments
                        )
                        assert result.is_error is False, (
                            resource_type,
                            operation,
                            result,
                        )
                        assert result.structured_content is not None
                        assert result.structured_content["outcome"] == "completed"

    asyncio.run(invoke())


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"All {len(tests)} resource mutation tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
