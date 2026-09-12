#!/usr/bin/env python3
"""Credential-free contract tests for the static generic-resource registry."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from zenml_resource_registry import RESOURCE_REGISTRY, describe_resources


def test_exact_read_coverage() -> None:
    assert len(RESOURCE_REGISTRY) == 30
    assert sum("list" in spec.operations for spec in RESOURCE_REGISTRY.values()) == 30
    assert sum("get" in spec.operations for spec in RESOURCE_REGISTRY.values()) == 28
    assert RESOURCE_REGISTRY["secret"].operations == ("list",)
    assert RESOURCE_REGISTRY["run_wait_condition"].operations == ("list",)
    assert [name for name, spec in RESOURCE_REGISTRY.items() if spec.non_paginated] == [
        "service_connector_type"
    ]


def test_catalog_is_bounded() -> None:
    catalog = describe_resources()
    assert len(catalog["resources"]) == 30
    assert all("input_schema" not in item for item in catalog["resources"])
    assert all(
        set(item) == {"resource_type", "operations", "scope", "policy", "description"}
        for item in catalog["resources"]
    )


def test_operation_schemas_match_invocation_policy() -> None:
    project_schema = describe_resources("pipeline_run", "list")["input_schema"]
    global_schema = describe_resources("user", "list")["input_schema"]
    assert "project_id" in project_schema["properties"]
    assert "project_id" not in global_schema["properties"]
    assert project_schema["properties"]["size"]["maximum"] == 200
    assert (
        "flavor"
        not in describe_resources("schedule_trigger", "list")["input_schema"][
            "properties"
        ]["filters"]["properties"]
    )
    assert (
        "flavor"
        not in describe_resources("platform_event_trigger", "list")["input_schema"][
            "properties"
        ]["filters"]["properties"]
    )
    for resource_type, parent in (
        ("artifact_version", "artifact_id"),
        ("model_version", "model_id"),
        ("run_step", "pipeline_run_id"),
    ):
        list_schema = describe_resources(resource_type, "list")["input_schema"]
        assert "filters" in list_schema["required"]
        assert parent in list_schema["properties"]["filters"]["required"]
        assert (
            parent
            in describe_resources(resource_type, "get")["input_schema"]["required"]
        )
        assert (
            describe_resources(resource_type, "list")["example"]["filters"][parent]
            == f"<{parent}>"
        )
    component_schema = describe_resources("stack_component", "get")["input_schema"]
    assert "component_type" in component_schema["required"]


def test_filter_schemas_are_typed() -> None:
    user_filters = describe_resources("user", "list")["input_schema"]["properties"][
        "filters"
    ]["properties"]
    assert user_filters["active"] == {"type": "boolean"}
    assert user_filters["logical_operator"]["enum"] == ["and", "or"]
    assert user_filters["name"]["anyOf"][0]["type"] == "string"

    run_filters = describe_resources("pipeline_run", "list")["input_schema"][
        "properties"
    ]["filters"]["properties"]
    assert run_filters["index"]["anyOf"][0]["anyOf"][0]["type"] == "integer"

    trigger_filters = describe_resources("schedule_trigger", "list")["input_schema"][
        "properties"
    ]["filters"]["properties"]
    assert trigger_filters["concurrency"]["anyOf"][0]["enum"] == ["skip", "submit"]

    connector_filters = describe_resources("service_connector", "list")["input_schema"][
        "properties"
    ]["filters"]["properties"]
    assert connector_filters["labels"]["type"] == "object"
    assert connector_filters["resource_type"] == {
        "type": "string",
        "minLength": 1,
    }

    snapshot_filters = describe_resources("snapshot", "list")["input_schema"][
        "properties"
    ]["filters"]["properties"]
    assert snapshot_filters["trigger_id"] == {"type": "string", "format": "uuid"}


def test_aliases_and_unsupported_operations_are_rejected() -> None:
    for resource_type in ("users", "Pipeline", "pipeline-runs"):
        try:
            describe_resources(resource_type)
        except ValueError as error:
            assert "allowed resource types" in str(error)
        else:
            raise AssertionError(f"alias {resource_type!r} was accepted")
    try:
        describe_resources("secret", "get")
    except ValueError as error:
        assert "supported operations: list" in str(error)
    else:
        raise AssertionError("secret/get was advertised")


def main() -> int:
    tests = [
        test_exact_read_coverage,
        test_catalog_is_bounded,
        test_operation_schemas_match_invocation_policy,
        test_filter_schemas_are_typed,
        test_aliases_and_unsupported_operations_are_rejected,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"All {len(tests)} resource registry tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
