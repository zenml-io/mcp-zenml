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
"""Explicitly gated CRUD and action receipts against a disposable ZenML server.

The default invocation is credential-free and performs no network or mutation.
An operator must supply a dedicated loopback server and its disposable API key.
Shared or remote ZenML targets are rejected even when the gate is enabled.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))


def _enabled_target() -> str | None:
    if os.getenv("ZENML_MCP_DISPOSABLE_INTEGRATION") != "1":
        if os.getenv("ZENML_MCP_ACTION_INTEGRATION") == "1":
            raise RuntimeError(
                "Action integration requires ZENML_MCP_DISPOSABLE_INTEGRATION=1"
            )
        return None
    target = os.getenv("ZENML_STORE_URL", "")
    api_key = os.getenv("ZENML_STORE_API_KEY", "")
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise RuntimeError(
            "Disposable mutation integration requires a loopback ZENML_STORE_URL"
        )
    if not api_key:
        raise RuntimeError(
            "Disposable mutation integration requires ZENML_STORE_API_KEY"
        )
    return target


def _action_fixture() -> dict[str, str]:
    """Load explicit disposable IDs for feature-enabled action receipts."""
    raw = os.getenv("ZENML_MCP_ACTION_FIXTURE", "")
    if not raw:
        raise RuntimeError(
            "Action integration is incomplete: ZENML_MCP_ACTION_FIXTURE is required"
        )
    try:
        fixture = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Action integration is incomplete: ZENML_MCP_ACTION_FIXTURE must be JSON"
        ) from error
    required = {
        "snapshot_id",
        "pipeline_run_id",
        "wait_condition_id",
        "deployment_id",
    }
    if not isinstance(fixture, dict) or not required <= set(fixture):
        missing = sorted(
            required - set(fixture) if isinstance(fixture, dict) else required
        )
        raise RuntimeError(
            "Action integration is incomplete: missing fixture IDs "
            + ", ".join(missing)
        )
    result: dict[str, str] = {}
    for field in required:
        value = fixture[field]
        try:
            result[field] = str(uuid.UUID(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise RuntimeError(
                f"Action integration is incomplete: {field} must be an exact UUID"
            ) from error
    return result


class _OneShotCleanup:
    """Run a non-idempotent cleanup callback at most once after it is armed."""

    def __init__(self) -> None:
        self.pending = False

    def arm(self) -> None:
        self.pending = True

    def run(self, callback: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        if not self.pending:
            return None
        self.pending = False
        return callback()


def run_disposable_crud() -> None:
    from zenml.client import Client
    from zenml_resource_dispatch import (
        ResourceNotFound,
        create_resource,
        delete_resource,
        get_resource,
        update_resource,
    )

    def nested_value(value: Any, key: str) -> Any:
        if isinstance(value, dict):
            if key in value:
                return value[key]
            for child in value.values():
                found = nested_value(child, key)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = nested_value(child, key)
                if found is not None:
                    return found
        return None

    client = Client()
    suffix = uuid.uuid4().hex[:12]
    name = f"mcp-disposable-u5-{suffix}"
    created_id: str | None = None
    delete_attempted = False
    try:
        created = create_resource(
            client,
            "project",
            payload={"name": name, "description": "Disposable MCP U5 fixture"},
        )
        created_id = created["resource_id"]
        assert created_id
        persisted = get_resource(client, "project", created_id)
        assert persisted["item"]["id"] == created_id
        updated = update_resource(
            client,
            "project",
            created_id,
            payload={"name": f"{name}-updated"},
        )
        assert updated["outcome"] == "completed"
        persisted_update = get_resource(client, "project", created_id)
        assert nested_value(persisted_update["item"], "name") == f"{name}-updated"
        delete_attempted = True
        deleted_result = delete_resource(client, "project", created_id)
        assert deleted_result["outcome"] == "completed"
        try:
            get_resource(client, "project", created_id)
        except ResourceNotFound:
            pass
        else:
            raise AssertionError("deleted disposable project still exists")
    finally:
        if created_id and not delete_attempted:
            client.delete_project(name_id_or_prefix=created_id)


def run_disposable_actions() -> None:
    """Exercise the feature-enabled U6 action matrix on disposable resources."""
    from zenml.client import Client
    from zenml.exceptions import IllegalOperationError
    from zenml_resource_dispatch import (
        action_resource,
        create_resource,
        delete_resource,
        get_resource,
        list_resources,
    )

    fixture = _action_fixture()
    client = Client()
    project_id = str(client.active_project.id)
    suffix = uuid.uuid4().hex[:12]
    webhook_id: str | None = None
    trigger_id: str | None = None
    tag_id: str | None = None
    replayed_run_id: str | None = None
    trigger_attached = False
    tag_attached = False
    deployment_cleanup = _OneShotCleanup()
    try:
        created = create_resource(
            client,
            "webhook",
            project_id=project_id,
            payload={
                "name": f"mcp-disposable-u6-{suffix}",
                "webhook_type": "generic",
            },
        )
        webhook_id = created["resource_id"]
        rotated = action_resource(
            client,
            "webhook",
            "rotate_secret",
            webhook_id,
            project_id=project_id,
            payload={},
        )
        assert rotated["outcome"] == "completed"
        assert rotated["issued_secret"]
        assert "secret" not in rotated.get("item", {})
        persisted = get_resource(client, "webhook", webhook_id, project_id=project_id)
        assert "secret" not in persisted["item"]

        trigger = create_resource(
            client,
            "schedule_trigger",
            project_id=project_id,
            payload={
                "name": f"mcp-disposable-u6-trigger-{suffix}",
                "active": False,
                "interval": 86400,
            },
        )
        trigger_id = trigger["resource_id"]
        attached = action_resource(
            client,
            "schedule_trigger",
            "attach",
            trigger_id,
            project_id=project_id,
            payload={"snapshot_id": fixture["snapshot_id"], "allow_replace": False},
        )
        assert attached["outcome"] == "completed"
        trigger_attached = True
        attached_read = list_resources(
            client,
            "schedule_trigger",
            filters={"id": trigger_id, "snapshot_id": fixture["snapshot_id"]},
            project_id=project_id,
            page=1,
            size=2,
        )
        assert any(item["id"] == trigger_id for item in attached_read["items"])
        action_resource(
            client,
            "schedule_trigger",
            "clear_dispatch_error",
            trigger_id,
            project_id=project_id,
            payload={"snapshot_id": fixture["snapshot_id"]},
        )
        action_resource(
            client,
            "schedule_trigger",
            "detach",
            trigger_id,
            project_id=project_id,
            payload={"snapshot_id": fixture["snapshot_id"]},
        )
        trigger_attached = False
        detached_read = list_resources(
            client,
            "schedule_trigger",
            filters={"id": trigger_id, "snapshot_id": fixture["snapshot_id"]},
            project_id=project_id,
            page=1,
            size=2,
        )
        assert not detached_read["items"]

        replayed = action_resource(
            client,
            "pipeline_run",
            "replay",
            fixture["pipeline_run_id"],
            project_id=project_id,
            payload={"run_configuration": {"skip_successful_steps": True}},
        )
        replayed_run_id = replayed["new_run_id"]
        replayed_read = get_resource(
            client, "pipeline_run", replayed_run_id, project_id=project_id
        )
        assert replayed_read["item"]["id"] == replayed_run_id

        tag = create_resource(
            client,
            "tag",
            payload={"name": f"mcp-disposable-u6-tag-{suffix}", "exclusive": False},
        )
        tag_id = tag["resource_id"]
        action_resource(
            client,
            "tag",
            "attach",
            tag_id,
            project_id=project_id,
            payload={
                "target_id": fixture["pipeline_run_id"],
                "target_type": "pipeline_run",
            },
        )
        tag_attached = True
        tagged = client.get_pipeline_run(
            name_id_or_prefix=uuid.UUID(fixture["pipeline_run_id"]),
            project=project_id,
            hydrate=True,
        )
        assert tag_id in repr(tagged.model_dump(mode="json"))
        action_resource(
            client,
            "tag",
            "detach",
            tag_id,
            project_id=project_id,
            payload={
                "target_id": fixture["pipeline_run_id"],
                "target_type": "pipeline_run",
            },
        )
        tag_attached = False
        untagged = client.get_pipeline_run(
            name_id_or_prefix=uuid.UUID(fixture["pipeline_run_id"]),
            project=project_id,
            hydrate=True,
        )
        assert tag_id not in repr(untagged.model_dump(mode="json"))

        requests_page = list_resources(
            client, "resource_request", filters={}, page=1, size=1
        )
        assert requests_page["resource_type"] == "resource_request"

        deployment_cleanup.arm()
        provisioned = action_resource(
            client,
            "deployment",
            "provision",
            fixture["deployment_id"],
            project_id=project_id,
            payload={"snapshot_id": fixture["snapshot_id"], "timeout": 300},
        )
        assert provisioned["outcome"] == "completed"
        refreshed = action_resource(
            client,
            "deployment",
            "refresh",
            fixture["deployment_id"],
            project_id=project_id,
            payload={},
        )
        assert refreshed["outcome"] == "completed"
        deprovisioned = deployment_cleanup.run(
            lambda: action_resource(
                client,
                "deployment",
                "deprovision",
                fixture["deployment_id"],
                project_id=project_id,
                payload={"timeout": 300},
            )
        )
        assert deprovisioned is not None
        assert deprovisioned["outcome"] in {"accepted", "completed"}

        resolved = action_resource(
            client,
            "run_wait_condition",
            "resolve",
            fixture["wait_condition_id"],
            project_id=project_id,
            payload={"resolution": "ABORT", "result": {"source": "mcp-u6"}},
        )
        assert resolved["outcome"] == "completed"
        try:
            action_resource(
                client,
                "run_wait_condition",
                "resolve",
                fixture["wait_condition_id"],
                project_id=project_id,
                payload={"resolution": "ABORT"},
            )
        except IllegalOperationError:
            pass
        else:
            raise AssertionError(
                "resolving a completed wait condition did not conflict"
            )
    finally:
        action_failed = sys.exc_info()[0] is not None
        try:
            cleanup = deployment_cleanup.run(
                lambda: action_resource(
                    client,
                    "deployment",
                    "deprovision",
                    fixture["deployment_id"],
                    project_id=project_id,
                    payload={"timeout": 300},
                )
            )
            if cleanup is not None:
                if cleanup["outcome"] not in {"accepted", "completed"}:
                    raise AssertionError("deployment cleanup was not accepted")
        except Exception:
            if not action_failed:
                raise
        if tag_attached and tag_id:
            action_resource(
                client,
                "tag",
                "detach",
                tag_id,
                project_id=project_id,
                payload={
                    "target_id": fixture["pipeline_run_id"],
                    "target_type": "pipeline_run",
                },
            )
        if tag_id:
            delete_resource(client, "tag", tag_id)
        if trigger_attached and trigger_id:
            action_resource(
                client,
                "schedule_trigger",
                "detach",
                trigger_id,
                project_id=project_id,
                payload={"snapshot_id": fixture["snapshot_id"]},
            )
        if trigger_id:
            delete_resource(
                client, "schedule_trigger", trigger_id, project_id=project_id
            )
        if webhook_id:
            delete_resource(client, "webhook", webhook_id, project_id=project_id)
        if replayed_run_id:
            delete_resource(
                client, "pipeline_run", replayed_run_id, project_id=project_id
            )


def test_integration_gate() -> None:
    cleanup = _OneShotCleanup()
    cleanup.arm()
    deprovision_calls = 0

    def accepted_timeout() -> dict[str, Any]:
        nonlocal deprovision_calls
        deprovision_calls += 1
        return {"outcome": "accepted", "status": "timed_out"}

    explicit_result = cleanup.run(accepted_timeout)
    finally_result = cleanup.run(accepted_timeout)
    assert explicit_result == {"outcome": "accepted", "status": "timed_out"}
    assert finally_result is None
    assert deprovision_calls == 1

    with patch.dict(os.environ, {}, clear=True):
        assert _enabled_target() is None
    with patch.dict(os.environ, {"ZENML_MCP_ACTION_INTEGRATION": "1"}, clear=True):
        try:
            _enabled_target()
        except RuntimeError as error:
            assert "ZENML_MCP_DISPOSABLE_INTEGRATION=1" in str(error)
        else:
            raise AssertionError("action integration ran without the base gate")
    with patch.dict(
        os.environ,
        {
            "ZENML_MCP_DISPOSABLE_INTEGRATION": "1",
            "ZENML_STORE_URL": "https://shared.example.com",
            "ZENML_STORE_API_KEY": "disposable-key",
        },
        clear=True,
    ):
        try:
            _enabled_target()
        except RuntimeError as error:
            assert "loopback" in str(error)
        else:
            raise AssertionError("remote integration target was accepted")
    with patch.dict(
        os.environ,
        {
            "ZENML_MCP_DISPOSABLE_INTEGRATION": "1",
            "ZENML_STORE_URL": "http://127.0.0.1:8237",
        },
        clear=True,
    ):
        try:
            _enabled_target()
        except RuntimeError as error:
            assert "API_KEY" in str(error)
        else:
            raise AssertionError("disposable target without a key was accepted")
    with patch.dict(os.environ, {}, clear=True):
        try:
            _action_fixture()
        except RuntimeError as error:
            assert "ZENML_MCP_ACTION_FIXTURE" in str(error)
        else:
            raise AssertionError("missing action fixture was accepted")
    with patch.dict(os.environ, {"ZENML_MCP_ACTION_FIXTURE": "{not-json"}, clear=True):
        try:
            _action_fixture()
        except RuntimeError as error:
            assert "must be JSON" in str(error)
        else:
            raise AssertionError("malformed action fixture was accepted")
    with patch.dict(
        os.environ,
        {"ZENML_MCP_ACTION_FIXTURE": json.dumps({"snapshot_id": str(uuid.uuid4())})},
        clear=True,
    ):
        try:
            _action_fixture()
        except RuntimeError as error:
            assert "missing fixture IDs" in str(error)
        else:
            raise AssertionError("incomplete action fixture was accepted")
    with patch.dict(
        os.environ,
        {
            "ZENML_MCP_ACTION_FIXTURE": json.dumps(
                {
                    "snapshot_id": "not-a-uuid",
                    "pipeline_run_id": str(uuid.uuid4()),
                    "wait_condition_id": str(uuid.uuid4()),
                    "deployment_id": str(uuid.uuid4()),
                }
            )
        },
        clear=True,
    ):
        try:
            _action_fixture()
        except RuntimeError as error:
            assert "snapshot_id" in str(error)
        else:
            raise AssertionError("invalid action fixture UUID was accepted")


def main() -> int:
    test_integration_gate()
    print("PASS: disposable integration gate")
    target = _enabled_target()
    if target is None:
        print("SKIP: disposable resource integration is gated; no server was contacted")
        return 0
    if os.getenv("ZENML_MCP_ACTION_INTEGRATION") == "1":
        _action_fixture()
    run_disposable_crud()
    print(f"PASS: disposable CRUD persisted on loopback target {target}")
    if os.getenv("ZENML_MCP_ACTION_INTEGRATION") == "1":
        run_disposable_actions()
        print(f"PASS: disposable action persisted on loopback target {target}")
    else:
        print("SKIP: disposable action integration is separately gated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
