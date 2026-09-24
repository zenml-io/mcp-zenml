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
# exclude-newer-package = { mcp = "2026-09-08T00:00:00Z", "mcp-types" = "2026-09-08T00:00:00Z", zenml = false }
#
# [tool.ty.rules]
# unresolved-import = "ignore"
#
# [tool.ty.environment]
# extra-paths = ["../server"]
# ///
"""Credential-free behavior tests for every finite resource action."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import enum
import io
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import requests
from mcp import Client
from zenml.deployers.exceptions import DeploymentTimeoutError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from zenml_resource_dispatch import (  # noqa: E402
    ResourceDispatchError,
    ResourceReadOnly,
    action_resource,
    get_resource,
)
from zenml_resource_registry import (  # noqa: E402
    ACTION_REGISTRY,
    ResourceRegistryError,
    describe_resources,
    validate_action_payload,
)

os.environ.setdefault("ZENML_MCP_ANALYTICS_ENABLED", "false")
import zenml_server as server  # noqa: E402

PROJECT = "11111111-1111-4111-8111-111111111111"
TARGET = "22222222-2222-4222-8222-222222222222"
RELATED = "33333333-3333-4333-8333-333333333333"
NEW_RUN = "44444444-4444-4444-8444-444444444444"
ACTION_CONTRACTS = json.loads(
    (
        Path(__file__).resolve().parent / "fixtures" / "resource_action_contracts.json"
    ).read_text(encoding="utf-8")
)


class Page:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.total = len(items)
        self.page = 1
        self.size = 2

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {
            "items": self.items,
            "total": self.total,
            "page": self.page,
            "size": self.size,
        }


class Model:
    def __init__(self, **values: Any) -> None:
        self.values = values
        for key, value in values.items():
            setattr(self, key, value)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return dict(self.values)


class Store:
    def __init__(self, owner: "Recorder") -> None:
        self.owner = owner

    def batch_create_tag_resource(self, **kwargs: Any) -> list[Model]:
        self.owner.calls.append(("batch_create_tag_resource", kwargs))
        return [Model(id=NEW_RUN, project_id=PROJECT)]

    def batch_delete_tag_resource(self, **kwargs: Any) -> None:
        self.owner.calls.append(("batch_delete_tag_resource", kwargs))


class Recorder:
    def __init__(self, *, exclusive_tag: bool = False) -> None:
        self.active_project = SimpleNamespace(id=uuid.UUID(PROJECT))
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.zen_store = Store(self)
        self.exclusive_tag = exclusive_tag

    def __getattr__(self, name: str):
        if name.startswith("get_"):

            def get(**kwargs: Any) -> Model:
                self.calls.append((name, kwargs))
                resource_id = next(
                    (value for key, value in kwargs.items() if "id" in key), TARGET
                )
                values: dict[str, Any] = {
                    "id": str(resource_id),
                    "project_id": PROJECT,
                }
                if name == "get_tag":
                    values["exclusive"] = self.exclusive_tag
                if name == "get_artifact_version":
                    values["artifact_id"] = NEW_RUN
                if name == "get_model_version":
                    values["model_id"] = str(kwargs.get("model_name_or_id", NEW_RUN))
                return Model(**values)

            return get
        if name.startswith("list_"):

            def list_items(**kwargs: Any) -> Page:
                self.calls.append((name, kwargs))
                filter_id = kwargs.get("id")
                if filter_id is None:
                    for value in kwargs.values():
                        if hasattr(value, "id"):
                            filter_id = value.id
                            break
                return Page([{"id": str(filter_id or RELATED), "project_id": PROJECT}])

            return list_items

        def action(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            if name == "replay_pipeline_run":
                return Model(id=NEW_RUN, status="running", project_id=PROJECT)
            if name in {"provision_deployment", "refresh_deployment"}:
                return Model(id=TARGET, status="running", project_id=PROJECT)
            if name == "resolve_run_wait_condition":
                return Model(id=TARGET, status="resolved", project_id=PROJECT)
            if name == "rotate_webhook_secret":
                return Model(
                    id=TARGET,
                    body=Model(secret="issued-once-marker"),
                    project_id=PROJECT,
                )
            return None

        return action


def _payload(resource_type: str, action: str) -> dict[str, Any]:
    if resource_type == "pipeline_run":
        return {"run_configuration": {"skip_successful_steps": True}}
    if resource_type.endswith("_trigger"):
        if action == "attach":
            return {"snapshot_id": RELATED, "allow_replace": True}
        if action == "detach":
            return {"snapshot_id": RELATED}
        return {}
    if resource_type == "deployment":
        return {"snapshot_id": RELATED, "timeout": 30} if action == "provision" else {}
    if resource_type == "run_wait_condition":
        return {"resolution": "CONTINUE", "result": {"approved": True}}
    if resource_type == "tag":
        return {"target_id": RELATED, "target_type": "pipeline_run"}
    if resource_type == "webhook":
        return {"secret": "replacement-marker"}
    raise AssertionError(resource_type)


def _normalize_call_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return _normalize_call_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {
            _normalize_call_value(key): _normalize_call_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_normalize_call_value(item) for item in value]
    return value


def test_catalog_is_exact_and_payloads_are_bounded() -> None:
    expected = {
        ("pipeline_run", "replay"),
        ("deployment", "provision"),
        ("deployment", "deprovision"),
        ("deployment", "refresh"),
        ("run_wait_condition", "resolve"),
        ("tag", "attach"),
        ("tag", "detach"),
        ("webhook", "rotate_secret"),
        *(
            (trigger, action)
            for trigger in (
                "schedule_trigger",
                "platform_event_trigger",
                "webhook_trigger",
            )
            for action in ("attach", "detach", "clear_dispatch_error")
        ),
    }
    assert set(ACTION_REGISTRY) == expected
    assert set(ACTION_CONTRACTS) == {
        f"{resource_type}.{action}" for resource_type, action in expected
    }
    assert "action" in describe_resources("pipeline_run")["operations"]
    assert (
        describe_resources("pipeline_run", "action")["actions"][0]["action"] == "replay"
    )
    for spec in ACTION_REGISTRY.values():
        schema = spec.schema()
        assert (
            schema == ACTION_CONTRACTS[f"{spec.resource_type}.{spec.action}"]["schema"]
        )
        assert "project_id" in schema["required"]
        assert ("payload" in schema["required"]) is bool(spec.required_payload)
        assert schema["additionalProperties"] is False
        if spec.required_payload:
            try:
                validate_action_payload(spec.resource_type, spec.action, {})
            except ResourceRegistryError:
                pass
            else:
                raise AssertionError((spec.resource_type, spec.action, "required"))
        for field, field_schema in spec.payload_properties.items():
            if not field_schema:
                continue
            invalid: Any = 7 if field_schema.get("type") != "integer" else "seven"
            try:
                validate_action_payload(
                    spec.resource_type,
                    spec.action,
                    {**_payload(spec.resource_type, spec.action), field: invalid},
                )
            except ResourceRegistryError:
                pass
            else:
                raise AssertionError((spec.resource_type, spec.action, field))
    try:
        validate_action_payload("pipeline_run", "replay", {"config_path": "/tmp/x"})
    except ResourceRegistryError:
        pass
    else:
        raise AssertionError("unallowlisted replay field was accepted")

    nested_invalid = (
        (
            "pipeline_run",
            "replay",
            {"run_configuration": {"steps_to_skip": [7]}},
        ),
        (
            "pipeline_run",
            "replay",
            {"run_configuration": {"unknown": True}},
        ),
        (
            "pipeline_run",
            "replay",
            {"run_configuration": {"step_input_overrides": {"step": 7}}},
        ),
        (
            "pipeline_run",
            "replay",
            {"run_configuration": {"step_default_input_overrides": {"step": "wrong"}}},
        ),
        (
            "schedule_trigger",
            "attach",
            {
                "snapshot_id": RELATED,
                "run_configuration": {"substitutions": {"key": 7}},
            },
        ),
        (
            "schedule_trigger",
            "attach",
            {
                "snapshot_id": RELATED,
                "run_configuration": {"execution_mode": "unknown"},
            },
        ),
        (
            "tag",
            "attach",
            {"target_id": RELATED, "target_type": "secret"},
        ),
        (
            "run_wait_condition",
            "resolve",
            {"resolution": "SKIP"},
        ),
    )
    for resource_type, action, payload in nested_invalid:
        try:
            validate_action_payload(resource_type, action, payload)
        except ResourceRegistryError:
            pass
        else:
            raise AssertionError((resource_type, action, payload))


def test_every_action_dispatches_once_with_exact_ids() -> None:
    for resource_type, action in ACTION_REGISTRY:
        client = Recorder()
        result = action_resource(
            client,
            resource_type,
            action,
            TARGET,
            project_id=PROJECT,
            payload=_payload(resource_type, action),
        )
        assert result["outcome"] in {"accepted", "completed"}
        expected_call = ACTION_CONTRACTS[f"{resource_type}.{action}"]["call"]
        action_calls = [
            {"method": name, "kwargs": _normalize_call_value(kwargs)}
            for name, kwargs in client.calls
            if name == expected_call["method"]
        ]
        assert action_calls == [expected_call], (
            resource_type,
            action,
            client.calls,
        )
    replay = action_resource(
        Recorder(), "pipeline_run", "replay", TARGET, project_id=PROJECT, payload={}
    )
    assert replay["new_run_id"] == NEW_RUN
    assert replay["status"] == "running"
    assert replay["reconciliation"] == {
        "operation": "get",
        "resource_type": "pipeline_run",
        "resource_id": NEW_RUN,
        "project_id": PROJECT,
        "note": "Inspect the replayed run status; do not replay automatically.",
    }


def test_trigger_wait_tag_and_webhook_semantics() -> None:
    client = Recorder()
    action_resource(
        client,
        "schedule_trigger",
        "attach",
        TARGET,
        project_id=PROJECT,
        payload={"snapshot_id": RELATED, "allow_replace": False},
    )
    attach = next(
        kwargs for name, kwargs in client.calls if name == "attach_trigger_to_snapshot"
    )
    assert attach["allow_replace"] is False
    assert isinstance(attach["trigger_id"], uuid.UUID)
    assert isinstance(attach["pipeline_snapshot_id"], uuid.UUID)

    configured = Recorder()
    action_resource(
        configured,
        "schedule_trigger",
        "attach",
        TARGET,
        project_id=PROJECT,
        payload={
            "snapshot_id": RELATED,
            "run_configuration": {
                "run_name": "contract-run",
                "enable_cache": False,
                "substitutions": {"region": "eu"},
            },
        },
    )
    configured_attach = next(
        kwargs
        for name, kwargs in configured.calls
        if name == "attach_trigger_to_snapshot"
    )
    assert configured_attach["run_configuration"].run_name == "contract-run"
    assert configured_attach["run_configuration"].enable_cache is False
    assert configured_attach["run_configuration"].substitutions == {"region": "eu"}

    conflicting = Recorder()
    attach_calls = 0

    def reject_conflict(**kwargs: Any) -> None:
        nonlocal attach_calls
        attach_calls += 1
        if not kwargs["allow_replace"]:
            raise ValueError("trigger already attached")

    setattr(conflicting, "attach_trigger_to_snapshot", reject_conflict)
    try:
        action_resource(
            conflicting,
            "schedule_trigger",
            "attach",
            TARGET,
            project_id=PROJECT,
            payload={"snapshot_id": RELATED, "allow_replace": False},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("trigger conflict was hidden")
    action_resource(
        conflicting,
        "schedule_trigger",
        "attach",
        TARGET,
        project_id=PROJECT,
        payload={"snapshot_id": RELATED, "allow_replace": True},
    )
    assert attach_calls == 2
    detached = action_resource(
        conflicting,
        "schedule_trigger",
        "detach",
        TARGET,
        project_id=PROJECT,
        payload={"snapshot_id": RELATED},
    )
    assert detached["reconciliation"] == {
        "operation": "list",
        "resource_type": "schedule_trigger",
        "project_id": PROJECT,
        "filters": {"id": TARGET, "snapshot_id": RELATED},
        "note": "Confirm that the trigger attachment is absent.",
    }

    state: dict[str, str | None] = {"snapshot_id": RELATED}
    detached_state = Recorder()

    def get_trigger(**kwargs: Any) -> Model:
        del kwargs
        return Model(id=TARGET, project_id=PROJECT, snapshot_id=state["snapshot_id"])

    def detach(**kwargs: Any) -> None:
        assert str(kwargs["pipeline_snapshot_id"]) == RELATED
        state["snapshot_id"] = None

    setattr(detached_state, "get_schedule_trigger", get_trigger)
    setattr(detached_state, "detach_trigger_from_snapshot", detach)
    action_resource(
        detached_state,
        "schedule_trigger",
        "detach",
        TARGET,
        project_id=PROJECT,
        payload={"snapshot_id": RELATED},
    )
    persisted = get_resource(
        detached_state, "schedule_trigger", TARGET, project_id=PROJECT
    )
    assert persisted["item"]["snapshot_id"] is None

    cleared = Recorder()
    cleared_result = action_resource(
        cleared,
        "schedule_trigger",
        "clear_dispatch_error",
        TARGET,
        project_id=PROJECT,
        payload={},
    )
    clear_kwargs = next(
        kwargs
        for name, kwargs in cleared.calls
        if name == "clear_trigger_dispatch_error"
    )
    assert clear_kwargs["pipeline_snapshot_id"] is None
    assert cleared_result["reconciliation"] == {
        "operation": "get",
        "resource_type": "schedule_trigger",
        "resource_id": TARGET,
        "project_id": PROJECT,
        "hydrate": True,
        "note": "Inspect snapshot_dispatch_states and confirm the dispatch error fields are empty; do not repeat automatically.",
    }

    cleared_snapshot = Recorder()
    cleared_snapshot_result = action_resource(
        cleared_snapshot,
        "schedule_trigger",
        "clear_dispatch_error",
        TARGET,
        project_id=PROJECT,
        payload={"snapshot_id": RELATED},
    )
    clear_snapshot_kwargs = next(
        kwargs
        for name, kwargs in cleared_snapshot.calls
        if name == "clear_trigger_dispatch_error"
    )
    assert str(clear_snapshot_kwargs["pipeline_snapshot_id"]) == RELATED
    assert cleared_snapshot_result["reconciliation"]["expected_snapshot_id"] == RELATED
    assert cleared_snapshot_result["reconciliation"]["hydrate"] is True

    wait = action_resource(
        Recorder(),
        "run_wait_condition",
        "resolve",
        TARGET,
        project_id=PROJECT,
        payload={"resolution": "ABORT", "result": ["reason"]},
    )
    assert wait["outcome"] == "completed"
    assert wait["status"] == "resolved"

    aborted_client = Recorder()
    abort_result = ["reason"]
    action_resource(
        aborted_client,
        "run_wait_condition",
        "resolve",
        TARGET,
        project_id=PROJECT,
        payload={"resolution": "ABORT", "result": abort_result},
    )
    abort_kwargs = next(
        kwargs
        for name, kwargs in aborted_client.calls
        if name == "resolve_run_wait_condition"
    )
    assert abort_kwargs["resolution"].value == "abort"
    assert abort_kwargs["result"] is abort_result

    for action in ("provision", "refresh"):
        deployment = action_resource(
            Recorder(),
            "deployment",
            action,
            TARGET,
            project_id=PROJECT,
            payload={"timeout": 30} if action == "provision" else {},
        )
        assert deployment["status"] == "running"

    default_provision = Recorder()
    action_resource(
        default_provision,
        "deployment",
        "provision",
        TARGET,
        project_id=PROJECT,
        payload={},
    )
    default_provision_kwargs = next(
        kwargs
        for name, kwargs in default_provision.calls
        if name == "provision_deployment"
    )
    assert default_provision_kwargs["snapshot_id"] is None
    assert default_provision_kwargs["timeout"] == 300

    wait_client = Recorder()
    resolution_calls = 0
    pipeline_actions = 0

    class ConflictError(Exception):
        pass

    def resolve_once(**kwargs: Any) -> Model:
        nonlocal resolution_calls, pipeline_actions
        del kwargs
        resolution_calls += 1
        if resolution_calls > 1:
            raise ConflictError("already resolved")
        pipeline_actions += 1
        return Model(id=TARGET, status="resolved", project_id=PROJECT)

    setattr(wait_client, "resolve_run_wait_condition", resolve_once)
    wait_payload = {"resolution": "CONTINUE", "result": {"ok": True}}
    action_resource(
        wait_client,
        "run_wait_condition",
        "resolve",
        TARGET,
        project_id=PROJECT,
        payload=wait_payload,
    )
    try:
        action_resource(
            wait_client,
            "run_wait_condition",
            "resolve",
            TARGET,
            project_id=PROJECT,
            payload=wait_payload,
        )
    except ConflictError:
        pass
    else:
        raise AssertionError("second wait resolution conflict was hidden")
    assert resolution_calls == 2
    assert pipeline_actions == 1

    try:
        action_resource(
            Recorder(exclusive_tag=True),
            "tag",
            "attach",
            TARGET,
            project_id=PROJECT,
            payload={"target_id": RELATED, "target_type": "pipeline_run"},
        )
    except ResourceDispatchError as error:
        assert "allow_exclusive_replace" in str(error)
    else:
        raise AssertionError("exclusive relation replacement was implicit")

    tag_client = Recorder(exclusive_tag=True)
    tag = action_resource(
        tag_client,
        "tag",
        "attach",
        TARGET,
        project_id=PROJECT,
        payload={
            "target_id": RELATED,
            "target_type": "pipeline_run",
            "allow_exclusive_replace": True,
        },
    )
    assert tag["exclusive_replacement_authorized"] is True
    assert tag["reconciliation"] == {
        "operation": "get",
        "resource_type": "pipeline_run",
        "resource_id": RELATED,
        "project_id": PROJECT,
        "hydrate": True,
        "expected_tag_id": TARGET,
        "note": "Inspect the hydrated target tags for this exact tag ID; do not repeat automatically.",
    }
    request = next(
        kwargs
        for name, kwargs in tag_client.calls
        if name == "batch_create_tag_resource"
    )["tag_resources"][0]
    assert str(request.tag_id) == TARGET
    assert str(request.resource_id) == RELATED

    for target_type in (
        "artifact",
        "artifact_version",
        "model",
        "model_version",
        "pipeline",
        "pipeline_run",
        "run_template",
        "pipeline_snapshot",
        "deployment",
    ):
        target_client = Recorder()
        payload = {"target_id": RELATED, "target_type": target_type}
        if target_type == "artifact_version":
            payload["artifact_id"] = NEW_RUN
        elif target_type == "model_version":
            payload["model_id"] = NEW_RUN
        target_result = action_resource(
            target_client,
            "tag",
            "attach",
            TARGET,
            project_id=PROJECT,
            payload=payload,
        )
        assert (
            sum(name == "batch_create_tag_resource" for name, _ in target_client.calls)
            == 1
        ), target_type
        reconciliation = target_result["reconciliation"]
        assert reconciliation["hydrate"] is True
        assert reconciliation["expected_tag_id"] == TARGET
        if target_type == "artifact_version":
            assert reconciliation["artifact_id"] == NEW_RUN
        elif target_type == "model_version":
            assert reconciliation["model_id"] == NEW_RUN
        read_client = Recorder()
        reconciled = get_resource(
            read_client,
            reconciliation["resource_type"],
            reconciliation["resource_id"],
            project_id=reconciliation["project_id"],
            artifact_id=reconciliation.get("artifact_id"),
            model_id=reconciliation.get("model_id"),
            hydrate=reconciliation["hydrate"],
        )
        assert reconciled["item"]["id"] == RELATED
        assert read_client.calls[-1][1]["hydrate"] is True

    webhook = action_resource(
        Recorder(),
        "webhook",
        "rotate_secret",
        TARGET,
        project_id=PROJECT,
        payload={},
    )
    assert webhook["issued_secret"] == "issued-once-marker"
    assert "secret" not in webhook["item"]


def test_invalid_pairs_ids_and_read_only_never_call_sdk() -> None:
    for resource_type, action in (
        ("resource_request", "release"),
        ("resource_request", "renew"),
        ("pipeline_run", "rotate_secret"),
        ("unknown", "replay"),
    ):
        client = Recorder()
        try:
            action_resource(
                client,
                resource_type,
                action,
                TARGET,
                project_id=PROJECT,
                payload={},
            )
        except ResourceDispatchError:
            pass
        else:
            raise AssertionError((resource_type, action))
        assert client.calls == []

    for field, value in (
        ("resource_id", "deployment-name"),
        ("project_id", "project-name"),
    ):
        resource_id = value if field == "resource_id" else TARGET
        project_id = value if field == "project_id" else PROJECT
        client = Recorder()
        try:
            action_resource(
                client,
                "deployment",
                "refresh",
                resource_id,
                project_id=project_id,
                payload={},
            )
        except ResourceDispatchError:
            pass
        else:
            raise AssertionError(field)
        assert client.calls == []

    for action, payload in (
        ("provision", {"timeout": 0}),
        ("provision", {"timeout": 301}),
        ("deprovision", {"timeout": 0}),
        ("deprovision", {"timeout": 301}),
    ):
        client = Recorder()
        try:
            action_resource(
                client,
                "deployment",
                action,
                TARGET,
                project_id=PROJECT,
                payload=payload,
            )
        except ResourceDispatchError:
            pass
        else:
            raise AssertionError((action, payload))
        assert client.calls == []

    for payload in (
        {"target_id": RELATED, "target_type": "artifact_version"},
        {"target_id": RELATED, "target_type": "model_version"},
        {
            "target_id": RELATED,
            "target_type": "pipeline_run",
            "artifact_id": NEW_RUN,
        },
    ):
        client = Recorder()
        try:
            action_resource(
                client,
                "tag",
                "attach",
                TARGET,
                project_id=PROJECT,
                payload=payload,
            )
        except ResourceDispatchError:
            pass
        else:
            raise AssertionError(payload)
        assert client.calls == []

    mismatched = Recorder()
    try:
        action_resource(
            mismatched,
            "pipeline_run",
            "replay",
            TARGET,
            project_id=RELATED,
            payload={},
        )
    except ResourceDispatchError as error:
        assert "effective project" in str(error)
    else:
        raise AssertionError("mismatched project was accepted")
    assert "replay_pipeline_run" not in [name for name, _ in mismatched.calls]

    for resource_type in ("schedule_trigger", "deployment"):
        relation_mismatch = Recorder()

        def wrong_project_snapshot(**kwargs: Any) -> Model:
            relation_mismatch.calls.append(("get_snapshot", kwargs))
            return Model(id=RELATED, project_id=TARGET)

        setattr(relation_mismatch, "get_snapshot", wrong_project_snapshot)
        try:
            action_resource(
                relation_mismatch,
                resource_type,
                "attach" if resource_type == "schedule_trigger" else "provision",
                TARGET,
                project_id=PROJECT,
                payload={"snapshot_id": RELATED},
            )
        except ResourceDispatchError:
            pass
        else:
            raise AssertionError("mismatched snapshot project was accepted")
        action_method = (
            "attach_trigger_to_snapshot"
            if resource_type == "schedule_trigger"
            else "provision_deployment"
        )
        assert action_method not in [name for name, _ in relation_mismatch.calls]

    tag_mismatch = Recorder()

    def wrong_tag_target(**kwargs: Any) -> Page:
        tag_mismatch.calls.append(("list_pipeline_runs", kwargs))
        return Page([{"id": TARGET, "project_id": PROJECT}])

    setattr(tag_mismatch, "list_pipeline_runs", wrong_tag_target)
    try:
        action_resource(
            tag_mismatch,
            "tag",
            "attach",
            TARGET,
            project_id=PROJECT,
            payload={"target_id": RELATED, "target_type": "pipeline_run"},
        )
    except ResourceDispatchError:
        pass
    else:
        raise AssertionError("mismatched tag target was accepted")
    assert "batch_create_tag_resource" not in [name for name, _ in tag_mismatch.calls]

    client = Recorder()
    try:
        action_resource(
            client,
            "pipeline_run",
            "replay",
            TARGET,
            project_id=PROJECT,
            read_only=True,
        )
    except ResourceReadOnly:
        pass
    else:
        raise AssertionError("read-only action was accepted")
    assert client.calls == []


def test_deployment_timeout_and_connection_loss_are_not_retried() -> None:
    timed = Recorder()
    attempts = 0

    def timeout(**kwargs: Any) -> None:
        nonlocal attempts
        del kwargs
        attempts += 1
        raise DeploymentTimeoutError("provisioning timed out")

    setattr(timed, "provision_deployment", timeout)
    result = action_resource(
        timed,
        "deployment",
        "provision",
        TARGET,
        project_id=PROJECT,
        payload={"snapshot_id": RELATED, "timeout": 30},
    )
    assert attempts == 1
    assert result["outcome"] == "accepted"
    assert result["status"] == "timed_out"
    assert "do not repeat" in result["reconciliation"]["note"]

    deprovision_timeout = Recorder()
    deprovision_attempts = 0

    def time_out_deprovision(**kwargs: Any) -> None:
        nonlocal deprovision_attempts
        del kwargs
        deprovision_attempts += 1
        raise DeploymentTimeoutError("deprovisioning timed out")

    setattr(deprovision_timeout, "deprovision_deployment", time_out_deprovision)
    result = action_resource(
        deprovision_timeout,
        "deployment",
        "deprovision",
        TARGET,
        project_id=PROJECT,
        payload={"timeout": 30},
    )
    assert deprovision_attempts == 1
    assert result["outcome"] == "accepted"
    assert result["status"] == "timed_out"
    assert "do not repeat" in result["reconciliation"]["note"]

    absent = Recorder()

    def missing(**kwargs: Any) -> None:
        del kwargs
        raise KeyError("missing")

    setattr(absent, "get_deployment", missing)
    try:
        action_resource(
            absent,
            "deployment",
            "provision",
            TARGET,
            project_id=PROJECT,
            payload={"snapshot_id": RELATED},
        )
    except Exception as error:
        assert type(error).__name__ == "ResourceNotFound"
    else:
        raise AssertionError("absent deployment was provisioned")
    assert "provision_deployment" not in [name for name, _ in absent.calls]

    unavailable = Recorder()
    action_attempts = 0

    def missing_integration(**kwargs: Any) -> None:
        nonlocal action_attempts
        del kwargs
        action_attempts += 1
        raise NotImplementedError("missing deployer integration")

    setattr(unavailable, "provision_deployment", missing_integration)
    try:
        action_resource(
            unavailable,
            "deployment",
            "provision",
            TARGET,
            project_id=PROJECT,
            payload={"snapshot_id": RELATED},
        )
    except Exception as error:
        assert type(error).__name__ == "ResourceFeatureUnavailable"
    else:
        raise AssertionError("missing deployer integration was hidden")
    assert action_attempts == 1

    deprovision = Recorder()
    deprovision_attempts = 0

    def fail_deprovision(**kwargs: Any) -> None:
        nonlocal deprovision_attempts
        del kwargs
        deprovision_attempts += 1
        raise ValueError("provider refused")

    setattr(deprovision, "deprovision_deployment", fail_deprovision)
    try:
        action_resource(
            deprovision,
            "deployment",
            "deprovision",
            TARGET,
            project_id=PROJECT,
            payload={},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("deprovision failure was hidden")
    assert deprovision_attempts == 1

    refreshed = action_resource(
        Recorder(),
        "deployment",
        "refresh",
        TARGET,
        project_id=PROJECT,
        payload={},
    )
    assert refreshed["outcome"] == "completed"
    assert refreshed["item"]["status"] == "running"

    lost = Recorder()
    attempts = 0

    def lose_response(**kwargs: Any) -> None:
        nonlocal attempts
        del kwargs
        attempts += 1
        raise requests.ConnectionError("response lost")

    setattr(lost, "rotate_webhook_secret", lose_response)
    unknown = action_resource(
        lost,
        "webhook",
        "rotate_secret",
        TARGET,
        project_id=PROJECT,
        payload={"secret": "one-time-marker"},
    )
    assert attempts == 1
    assert unknown["outcome"] == "unknown"
    assert "one-time-marker" not in repr(unknown)
    assert "cannot be recovered" in unknown["reconciliation"]["note"]


def test_replay_connection_loss_does_not_offer_a_source_run_read() -> None:
    client = Recorder()
    attempts = 0

    def lose_response(**kwargs: Any) -> None:
        nonlocal attempts
        del kwargs
        attempts += 1
        raise requests.ConnectionError("response lost")

    setattr(client, "replay_pipeline_run", lose_response)
    result = action_resource(
        client,
        "pipeline_run",
        "replay",
        TARGET,
        project_id=PROJECT,
        payload={},
    )

    assert attempts == 1
    assert result["outcome"] == "unknown"
    assert result["resource_id"] == TARGET
    reconciliation = result["reconciliation"]
    assert reconciliation == {
        "operation": None,
        "resource_type": "pipeline_run",
        "source_run_id": TARGET,
        "project_id": PROJECT,
        "new_run_id": None,
        "reconcilable": False,
        "note": (
            "The replay may have created a new run, but its ID was lost with the "
            "response. Reading the source run cannot confirm the replay outcome; "
            "do not replay automatically."
        ),
    }
    assert reconciliation["operation"] is None
    assert (
        "reading the source run cannot reconcile" in result["error"]["message"].lower()
    )
    assert "lost while executing the action" in result["error"]["message"]
    assert "may have been dispatched" in result["error"]["message"]


def test_action_malformed_json_response_reports_unknown_outcome() -> None:
    client = Recorder()
    attempts = 0

    def parse_truncated_response(**kwargs: Any) -> None:
        nonlocal attempts
        del kwargs
        attempts += 1
        raise json.JSONDecodeError("Unterminated object", '{"secret":', 10)

    setattr(client, "rotate_webhook_secret", parse_truncated_response)
    result = action_resource(
        client,
        "webhook",
        "rotate_secret",
        TARGET,
        project_id=PROJECT,
        payload={},
    )

    assert attempts == 1
    assert result["outcome"] == "unknown"
    assert result["error"]["type"] == "UnknownOutcome"


def test_nested_pre_dispatch_action_failure_is_not_reported_as_unknown() -> None:
    class NewConnectionError(Exception):
        pass

    class MaxRetryError(Exception):
        def __init__(self, reason: BaseException) -> None:
            super().__init__(reason)
            self.reason = reason

    client = Recorder()
    for definite_failure in (
        requests.ConnectionError(
            MaxRetryError(NewConnectionError("connection refused"))
        ),
        requests.exceptions.ProxyError(
            MaxRetryError(NewConnectionError("proxy connection failed"))
        ),
    ):

        def fail_before_dispatch(**kwargs: Any) -> None:
            del kwargs
            raise definite_failure

        setattr(client, "rotate_webhook_secret", fail_before_dispatch)
        try:
            action_resource(
                client,
                "webhook",
                "rotate_secret",
                TARGET,
                project_id=PROJECT,
                payload={},
            )
        except requests.ConnectionError as error:
            assert error is definite_failure
        else:
            raise AssertionError(
                "definite pre-dispatch failure was reported as unknown outcome"
            )

    def lose_proxy_response(**kwargs: Any) -> None:
        del kwargs
        raise requests.exceptions.ProxyError("proxy response failed")

    setattr(client, "rotate_webhook_secret", lose_proxy_response)
    result = action_resource(
        client,
        "webhook",
        "rotate_secret",
        TARGET,
        project_id=PROJECT,
        payload={},
    )
    assert result["outcome"] == "unknown"


def test_real_mcp_action_and_read_only_precheck() -> None:
    async def invoke() -> None:
        fake = Recorder()
        events: list[dict[str, Any]] = []
        stderr = io.StringIO()
        with (
            contextlib.redirect_stderr(stderr),
            patch.object(server, "zenml_client", fake),
            patch.object(
                server.analytics,
                "track_tool_call",
                side_effect=lambda **event: events.append(event),
            ),
        ):
            async with Client(server.mcp, mode="auto") as mcp_client:
                response = await mcp_client.call_tool(
                    "zenml_action_resource",
                    {
                        "resource_type": "pipeline_run",
                        "action": "replay",
                        "resource_id": TARGET,
                        "project_id": PROJECT,
                        "payload": {},
                    },
                )
                for resource_type, action in ACTION_REGISTRY:
                    if (resource_type, action) == ("pipeline_run", "replay"):
                        continue
                    action_response = await mcp_client.call_tool(
                        "zenml_action_resource",
                        {
                            "resource_type": resource_type,
                            "action": action,
                            "resource_id": TARGET,
                            "project_id": PROJECT,
                            "payload": _payload(resource_type, action),
                        },
                    )
                    assert action_response.is_error is False, (
                        resource_type,
                        action,
                        action_response,
                    )
                    assert action_response.structured_content is not None
                    structured = action_response.structured_content
                    assert structured["resource_type"] == resource_type
                    assert structured["action"] == action
                    assert structured["resource_id"] == TARGET
                    assert structured["outcome"] in {"accepted", "completed"}
                    assert structured["reconciliation"]["resource_type"] in {
                        resource_type,
                        _payload(resource_type, action).get("target_type"),
                    }
                    if (resource_type, action) == ("webhook", "rotate_secret"):
                        assert structured["issued_secret"] == "issued-once-marker"
                        assert "secret" not in structured.get("item", {})
                    if resource_type == "tag" and action == "attach":
                        assert structured["exclusive_replacement_authorized"] is False
                rejected = await mcp_client.call_tool(
                    "zenml_action_resource",
                    {
                        "resource_type": "pipeline_run",
                        "action": "credential-marker",
                        "resource_id": TARGET,
                        "project_id": PROJECT,
                    },
                )
        assert response.is_error is False
        assert response.structured_content is not None
        assert response.structured_content["new_run_id"] == NEW_RUN
        event = next(item for item in events if item.get("operation") == "action")
        assert event["resource_type"] == "pipeline_run"
        assert event["action"] == "replay"
        assert event["outcome"] == "accepted"
        assert rejected.is_error is True
        rejected_event = next(
            item for item in events if item.get("action") == "unknown"
        )
        assert "credential-marker" not in repr(rejected_event)
        assert "replacement-marker" not in repr(events)
        assert "replacement-marker" not in stderr.getvalue()
        assert "issued-once-marker" not in stderr.getvalue()

        with (
            patch.dict(os.environ, {"ZENML_MCP_WRITE_POLICY": "read_only"}),
            patch.object(
                server,
                "get_zenml_client",
                side_effect=AssertionError("client accessed"),
            ),
        ):
            async with Client(server.mcp, mode="auto") as mcp_client:
                blocked = await mcp_client.call_tool(
                    "zenml_action_resource",
                    {
                        "resource_type": "pipeline_run",
                        "action": "replay",
                        "resource_id": TARGET,
                        "project_id": PROJECT,
                    },
                )
        assert blocked.is_error is True
        assert blocked.structured_content is not None
        assert blocked.structured_content["error"]["type"] == "PermissionDenied"

    asyncio.run(invoke())


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"All {len(tests)} resource action tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
