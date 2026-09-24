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
# exclude-newer-package = { mcp = "2026-09-08T00:00:00Z", "mcp-types" = "2026-09-08T00:00:00Z", zenml = false }
#
# [tool.ty.rules]
# unresolved-import = "ignore"
# ///
"""Verify server adapters bind to the pinned ZenML SDK contracts."""

from __future__ import annotations

import ast
import inspect
import sys
import tomllib
from pathlib import Path
from typing import Any

import zenml
from zenml.client import Client
from zenml.zen_stores.base_zen_store import BaseZenStore
from zenml.zen_stores.zen_store_interface import ZenStoreInterface

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO_ROOT / "server" / "zenml_server.py"
sys.path.insert(0, str(REPO_ROOT / "server"))

from zenml_resource_registry import (  # noqa: E402
    ACTION_REGISTRY,
    GET_SDK_METHODS,
    LIST_SDK_METHODS,
    RESOURCE_REGISTRY,
)

# The zenml pin in pyproject.toml, which every PEP 723 header mirrors.
EXPECTED_ZENML_VERSION: str = next(
    dependency.removeprefix("zenml==")
    for dependency in tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["dependencies"]
    if dependency.startswith("zenml==")
)


def _direct_client_calls() -> list[tuple[str, int, list[str], bool, int]]:
    """Return direct Client calls as method, positional count, keywords, kwargs, line."""
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"), filename=str(SERVER_PATH))
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

    def enclosing_function(node: ast.AST) -> ast.AST | None:
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return parent
            parent = parents.get(parent)
        return None

    client_aliases = {
        (enclosing_function(node), target.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "get_zenml_client"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    calls: list[tuple[str, int, list[str], bool, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        direct_receiver = (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Name)
            and receiver.func.id == "get_zenml_client"
        )
        aliased_receiver = (
            isinstance(receiver, ast.Name)
            and (enclosing_function(node), receiver.id) in client_aliases
        )
        if not direct_receiver and not aliased_receiver:
            continue
        keyword_names = [keyword.arg for keyword in node.keywords if keyword.arg]
        has_expansion = any(keyword.arg is None for keyword in node.keywords)
        calls.append(
            (
                node.func.attr,
                len(node.args),
                keyword_names,
                has_expansion,
                node.lineno,
            )
        )
    return calls


def test_direct_calls_bind() -> None:
    """Every statically declared Client call binds to the released signature."""
    failures: list[str] = []
    for (
        method_name,
        positional_count,
        keywords,
        has_expansion,
        line,
    ) in _direct_client_calls():
        method = getattr(Client, method_name, None)
        if method is None:
            failures.append(f"line {line}: Client.{method_name} does not exist")
            continue
        if has_expansion:
            continue
        signature = inspect.signature(method)
        try:
            signature.bind(
                object(),
                *([object()] * positional_count),
                **dict.fromkeys(keywords, object()),
            )
        except TypeError as error:
            failures.append(f"line {line}: Client.{method_name}{signature}: {error}")
    assert not failures, "\n".join(failures)


def test_dynamic_calls_bind() -> None:
    """The trigger variants assembled at runtime bind to the released signature."""
    signature = inspect.signature(Client.trigger_pipeline)
    for kwargs in (
        {"pipeline_name_or_id": "pipeline", "stack_name_or_id": None},
        {
            "pipeline_name_or_id": "pipeline",
            "stack_name_or_id": "stack",
            "snapshot_name_or_id": "snapshot",
        },
        {
            "pipeline_name_or_id": "pipeline",
            "stack_name_or_id": None,
            "template_id": "template",
        },
    ):
        signature.bind(object(), **kwargs)


def test_expected_released_signatures() -> None:
    """Critical drift-sensitive parameters remain present in the pinned ZenML."""
    assert zenml.__version__ == EXPECTED_ZENML_VERSION
    expected_parameters = {
        "list_snapshots": {"tags"},
        "list_deployments": {"tags"},
        "list_artifacts": {"tags"},
        "list_artifact_versions": {"artifact", "tags"},
        "list_models": {"tags"},
        "list_model_versions": {"model", "tags"},
        "list_run_templates": set(),
        "get_stack_component": {"component_type", "name_id_or_prefix"},
    }
    for method_name, required_names in expected_parameters.items():
        parameters = inspect.signature(getattr(Client, method_name)).parameters
        assert required_names <= set(parameters), method_name
        assert "tag" not in parameters, method_name


def test_resource_list_adapter_signatures() -> None:
    """Every advertised list adapter's complete allowlist binds to the SDK."""
    for resource_type, spec in RESOURCE_REGISTRY.items():
        method_name = LIST_SDK_METHODS[resource_type]
        if resource_type == "resource_request":
            inspect.signature(BaseZenStore.list_resource_requests).bind(
                object(), filter_model=object(), hydrate=False
            )
            continue
        kwargs = {field: object() for field in spec.list_filters}
        if resource_type == "artifact_version":
            kwargs["artifact"] = kwargs.pop("artifact_id")
        elif resource_type == "model_version":
            kwargs["model"] = kwargs.pop("model_id")
        if spec.scope == "project":
            kwargs["project"] = object()
        if not spec.non_paginated:
            kwargs.update(page=1, size=20, hydrate=False)
        if resource_type == "service_connector":
            kwargs["expand_secrets"] = False
        if resource_type in {"schedule_trigger", "platform_event_trigger"}:
            kwargs["flavor"] = object()
        inspect.signature(getattr(Client, method_name)).bind(object(), **kwargs)


def test_resource_get_adapter_signatures() -> None:
    """Every advertised get adapter's exact keywords bind to the SDK."""
    identifier_keywords = {
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
        "hook_invocation": "hook_invocation_id",
    }
    for resource_type, method_name in GET_SDK_METHODS.items():
        if resource_type == "resource_request":
            inspect.signature(BaseZenStore.get_resource_request).bind(
                object(), resource_request_id=object(), hydrate=False
            )
            continue
        kwargs: dict[str, Any] = {identifier_keywords[resource_type]: object()}
        if resource_type not in {"service_connector_type"}:
            kwargs["hydrate"] = False
        if RESOURCE_REGISTRY[
            resource_type
        ].scope == "project" and resource_type not in {"run_step", "hook_invocation"}:
            kwargs["project"] = object()
        if resource_type == "stack_component":
            kwargs["component_type"] = object()
        elif resource_type == "model_version":
            kwargs["model_name_or_id"] = object()
        elif resource_type == "service_connector":
            kwargs["expand_secrets"] = False
        inspect.signature(getattr(Client, method_name)).bind(object(), **kwargs)


def test_resource_membership_response_nesting() -> None:
    """Released response models expose every relation the dispatcher verifies."""
    from zenml.models.v2.base.scoped import ProjectScopedResponseBody
    from zenml.models.v2.core.artifact_version import ArtifactVersionResponseBody
    from zenml.models.v2.core.model_version import ModelVersionResponseBody
    from zenml.models.v2.core.step_run import StepRunResponseMetadata

    assert "project_id" in ProjectScopedResponseBody.model_fields
    assert "artifact" in ArtifactVersionResponseBody.model_fields
    assert "model" in ModelVersionResponseBody.model_fields
    assert "pipeline_run_id" in StepRunResponseMetadata.model_fields


def test_resource_mutation_adapter_signatures() -> None:
    """Every advertised mutation method and critical keyword exists."""
    required_parameters = {
        "create_project": {"name", "description"},
        "update_project": {"name_id_or_prefix", "new_name", "new_description"},
        "delete_project": {"name_id_or_prefix"},
        "create_stack": {"name", "components"},
        "update_stack": {"name_id_or_prefix", "component_updates"},
        "delete_stack": {"name_id_or_prefix", "recursive"},
        "create_stack_component": {"name", "flavor", "component_type", "configuration"},
        "update_stack_component": {
            "name_id_or_prefix",
            "component_type",
            "connector_id",
        },
        "delete_stack_component": {"name_id_or_prefix", "component_type"},
        "create_flavor": {"source", "component_type"},
        "delete_flavor": {"name_id_or_prefix"},
        "create_service": {"config", "service_type", "model_version_id"},
        "update_service": {"id", "model_version_id"},
        "delete_service": {"name_id_or_prefix", "project"},
        "delete_pipeline": {"name_id_or_prefix", "project"},
        "delete_pipeline_run": {"name_id_or_prefix", "project"},
        "update_snapshot": {"name_id_or_prefix", "project", "replace"},
        "delete_snapshot": {"name_id_or_prefix", "project"},
        "delete_build": {"id_or_prefix", "project"},
        "create_run_template": {"name", "snapshot_id"},
        "update_run_template": {"name_id_or_prefix", "hidden", "project"},
        "delete_run_template": {"name_id_or_prefix", "project"},
        "delete_deployment": {"name_id_or_prefix", "project", "force", "timeout"},
        "update_artifact": {
            "name_id_or_prefix",
            "new_name",
            "has_custom_name",
            "project",
        },
        "delete_artifact": {"name_id_or_prefix", "project"},
        "update_artifact_version": {"name_id_or_prefix", "add_tags", "project"},
        "delete_artifact_version": {
            "name_id_or_prefix",
            "delete_metadata",
            "delete_from_artifact_store",
            "server_side",
        },
        "create_model": {"name", "save_models_to_registry"},
        "update_model": {"model_name_or_id", "save_models_to_registry", "project"},
        "delete_model": {"model_name_or_id", "project"},
        "create_model_version": {"model_name_or_id", "project"},
        "update_model_version": {
            "model_name_or_id",
            "version_name_or_id",
            "force",
            "project",
        },
        "delete_model_version": {"model_version_id"},
        "create_tag": {"name", "exclusive", "color"},
        "update_tag": {"tag_name_or_id", "exclusive", "color"},
        "delete_tag": {"tag_name_or_id"},
        "create_service_connector": {
            "name",
            "connector_type",
            "verify",
            "list_resources",
            "register",
        },
        "update_service_connector": {
            "name_id_or_prefix",
            "configuration",
            "verify",
            "list_resources",
            "update",
        },
        "delete_service_connector": {"name_id_or_prefix"},
        "create_code_repository": {"name", "config", "source"},
        "update_code_repository": {"name_id_or_prefix", "config", "project"},
        "delete_code_repository": {"name_id_or_prefix", "project"},
        "create_webhook": {"name", "webhook_type", "secret"},
        "update_webhook": {"name_id_or_prefix", "active", "project"},
        "delete_webhook": {"name_id_or_prefix", "project"},
        "create_schedule_trigger": {"name", "project_id", "interval"},
        "update_schedule_trigger": {"trigger_name_id_or_prefix", "interval"},
        "create_platform_event_trigger": {
            "name",
            "source_type",
            "source_id",
            "target_events",
            "project_id",
        },
        "update_platform_event_trigger": {
            "trigger_name_id_or_prefix",
            "source_type",
            "source_id",
        },
        "create_webhook_trigger": {"name", "webhook", "configuration", "project_id"},
        "update_webhook_trigger": {"trigger_name_id_or_prefix", "configuration"},
        "delete_trigger": {"trigger_id", "soft"},
        "delete_hook_invocation": {"hook_invocation_id"},
    }
    advertised = {
        mutation.sdk_method
        for spec in RESOURCE_REGISTRY.values()
        for mutation in spec.mutations.values()
    }
    assert advertised == set(required_parameters)
    for method_name, required in required_parameters.items():
        parameters = inspect.signature(getattr(Client, method_name)).parameters
        assert required <= set(parameters), method_name


def test_resource_action_adapter_signatures() -> None:
    """Every advertised action binds to the exact released public API."""
    required_parameters = {
        "replay_pipeline_run": {
            "name_id_or_prefix",
            "run_configuration",
            "project",
            "synchronous",
        },
        "attach_trigger_to_snapshot": {
            "trigger_id",
            "pipeline_snapshot_id",
            "run_configuration",
            "allow_replace",
        },
        "detach_trigger_from_snapshot": {"trigger_id", "pipeline_snapshot_id"},
        "clear_trigger_dispatch_error": {"trigger_id", "pipeline_snapshot_id"},
        "provision_deployment": {
            "name_id_or_prefix",
            "project",
            "snapshot_id",
            "timeout",
        },
        "deprovision_deployment": {"name_id_or_prefix", "project", "timeout"},
        "refresh_deployment": {"name_id_or_prefix", "project"},
        "resolve_run_wait_condition": {"run_wait_condition_id", "resolution", "result"},
        "rotate_webhook_secret": {"name_id_or_prefix", "secret", "project"},
    }
    advertised = {
        spec.sdk_method
        for spec in ACTION_REGISTRY.values()
        if spec.resource_type != "tag"
    }
    assert advertised == set(required_parameters)
    for method_name, required in required_parameters.items():
        parameters = inspect.signature(getattr(Client, method_name)).parameters
        assert required <= set(parameters), method_name
    for method_name in ("batch_create_tag_resource", "batch_delete_tag_resource"):
        parameters = inspect.signature(
            getattr(ZenStoreInterface, method_name)
        ).parameters
        assert "tag_resources" in parameters

    from zenml.config.pipeline_run_configuration import ReplayRunConfiguration

    assert {
        "skip_successful_steps",
        "steps_to_skip",
        "step_input_overrides",
        "step_default_input_overrides",
    } <= set(ReplayRunConfiguration.model_fields)


def test_representative_typed_filters_validate_in_released_models() -> None:
    """Schema-valid scalar filters survive ZenML runtime model validation."""
    from zenml.models import (
        ArtifactVersionFilter,
        PipelineFilter,
        PipelineSnapshotFilter,
        ServiceConnectorFilter,
    )

    ArtifactVersionFilter(artifact="artifact-name")
    PipelineFilter(latest_run_status="running", latest_run_user="user-name")
    PipelineSnapshotFilter(trigger_id="11111111-1111-4111-8111-111111111111")
    ServiceConnectorFilter(
        resource_type="s3",
        resource_id="bucket-name",
        labels={"team": "ml", "region": None},
    )


def main() -> int:
    tests: list[tuple[str, Any]] = [
        ("test_direct_calls_bind", test_direct_calls_bind),
        ("test_dynamic_calls_bind", test_dynamic_calls_bind),
        ("test_expected_released_signatures", test_expected_released_signatures),
        (
            "test_resource_list_adapter_signatures",
            test_resource_list_adapter_signatures,
        ),
        ("test_resource_get_adapter_signatures", test_resource_get_adapter_signatures),
        (
            "test_resource_membership_response_nesting",
            test_resource_membership_response_nesting,
        ),
        (
            "test_resource_mutation_adapter_signatures",
            test_resource_mutation_adapter_signatures,
        ),
        (
            "test_resource_action_adapter_signatures",
            test_resource_action_adapter_signatures,
        ),
        (
            "test_representative_typed_filters_validate_in_released_models",
            test_representative_typed_filters_validate_in_released_models,
        ),
    ]
    for name, test in tests:
        test()
        print(f"PASS: {name}")
    print(f"All {len(tests)} SDK contract tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
