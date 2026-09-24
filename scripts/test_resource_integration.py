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
# extra-paths = [".", "../server"]
# ///
"""Explicitly gated CRUD and action receipts against a disposable ZenML server.

The default invocation is credential-free and performs no network or mutation.
An operator must supply a dedicated loopback server and its disposable API key.
Shared or remote ZenML targets are rejected even when the gate is enabled.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from check_pep723_requirements import pinned_version  # noqa: E402


class IntegrationIncomplete(RuntimeError):
    """The operator requested a live receipt without all of its prerequisites."""


# The zenml pin in pyproject.toml, which every PEP 723 header mirrors.
REQUIRED_ZENML_SERVER_VERSION = pinned_version("zenml")


def _require_complete_receipt() -> bool:
    return os.getenv("ZENML_MCP_REQUIRE_COMPLETE_INTEGRATION") == "1"


def _require_complete_subgates() -> None:
    """Reject a release receipt that omits restricted or action coverage."""
    if not _require_complete_receipt():
        return
    missing = [
        name
        for name in (
            "ZENML_MCP_RESTRICTED_INTEGRATION",
            "ZENML_MCP_ACTION_INTEGRATION",
        )
        if os.getenv(name) != "1"
    ]
    if missing:
        raise IntegrationIncomplete(
            "release integration requires " + ", ".join(missing)
        )


def _enabled_target() -> str | None:
    if os.getenv("ZENML_MCP_DISPOSABLE_INTEGRATION") != "1":
        if os.getenv("ZENML_MCP_ACTION_INTEGRATION") == "1":
            raise RuntimeError(
                "Action integration requires ZENML_MCP_DISPOSABLE_INTEGRATION=1"
            )
        if os.getenv("ZENML_MCP_RESTRICTED_INTEGRATION") == "1":
            raise IntegrationIncomplete(
                "restricted-credential integration requires "
                "ZENML_MCP_DISPOSABLE_INTEGRATION=1"
            )
        if _require_complete_receipt():
            raise IntegrationIncomplete(
                "release integration requires ZENML_MCP_DISPOSABLE_INTEGRATION=1"
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


def _target_identity(target: str) -> tuple[str, str, int | None, str]:
    parsed = urlparse(target)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise RuntimeError("Disposable integration requires a loopback ZenML store")
    default_port = 443 if parsed.scheme == "https" else 80
    return (
        parsed.scheme,
        hostname,
        parsed.port or default_port,
        parsed.path.rstrip("/"),
    )


def _require_client_target(client: Any, target: str) -> None:
    """Confirm the SDK resolved the exact disposable loopback store."""
    actual = str(client.zen_store.url)
    if _target_identity(actual) != _target_identity(target):
        raise RuntimeError(
            "ZenML client resolved a store other than the approved disposable target"
        )


def _verify_server_version(target: str) -> str:
    """Require the exact ZenML server version named by the release receipt."""
    import httpx

    response = httpx.get(f"{target.rstrip('/')}/api/v1/info", timeout=10.0)
    response.raise_for_status()
    body = response.json()
    version = body.get("version") if isinstance(body, dict) else None
    if version != REQUIRED_ZENML_SERVER_VERSION:
        raise IntegrationIncomplete(
            "disposable integration requires ZenML server "
            f"{REQUIRED_ZENML_SERVER_VERSION}; received {version!r}"
        )
    return REQUIRED_ZENML_SERVER_VERSION


def _restricted_api_key() -> str | None:
    if os.getenv("ZENML_MCP_RESTRICTED_INTEGRATION") != "1":
        return None
    api_key = os.getenv("ZENML_MCP_RESTRICTED_API_KEY", "")
    if not api_key:
        raise IntegrationIncomplete(
            "restricted-credential integration requires ZENML_MCP_RESTRICTED_API_KEY"
        )
    return api_key


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


def _nested_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _nested_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _nested_value(child, key)
            if found is not None:
                return found
    return None


def _disposable_code_repository_source() -> str:
    """Install a concrete no-network repository class for the live CRUD receipt."""
    from zenml.code_repositories import BaseCodeRepository

    class _DisposableCodeRepository(BaseCodeRepository):
        def login(self) -> None:
            pass

        def download_files(
            self, commit: str, directory: str, repo_sub_directory: str | None
        ) -> None:
            raise RuntimeError("The disposable repository cannot download files")

        def get_local_context(self, path: str) -> None:
            return None

    _DisposableCodeRepository.__module__ = __name__
    _DisposableCodeRepository.__qualname__ = "_DisposableCodeRepository"
    globals()["_DisposableCodeRepository"] = _DisposableCodeRepository
    return f"{__name__}._DisposableCodeRepository"


def run_disposable_crud(target: str) -> None:
    from zenml.client import Client
    from zenml_resource_dispatch import (
        ResourceNotFound,
        create_resource,
        delete_resource,
        get_resource,
        list_resources,
        update_resource,
    )

    client = Client()
    _require_client_target(client, target)
    project_id = str(client.active_project.id)
    suffix = uuid.uuid4().hex[:12]
    code_repository_source = _disposable_code_repository_source()
    cases: tuple[
        tuple[str, dict[str, Any], dict[str, Any], str, Any, str | None], ...
    ] = (
        (
            "project",
            {
                "name": f"mcp-disposable-u8-project-{suffix}",
                "description": "Disposable MCP U8 project",
            },
            {"description": "Updated disposable MCP U8 project"},
            "description",
            "Updated disposable MCP U8 project",
            None,
        ),
        (
            "tag",
            {"name": f"mcp-disposable-u8-tag-{suffix}", "exclusive": False},
            {"exclusive": True},
            "exclusive",
            True,
            None,
        ),
        (
            "model",
            {
                "name": f"mcp-disposable-u8-model-{suffix}",
                "description": "Disposable MCP U8 model",
                "save_models_to_registry": False,
            },
            {"description": "Updated disposable MCP U8 model"},
            "description",
            "Updated disposable MCP U8 model",
            project_id,
        ),
        (
            "code_repository",
            {
                "name": f"mcp-disposable-u8-repository-{suffix}",
                "source": code_repository_source,
                "config": {},
                "description": "Disposable MCP U8 repository",
            },
            {"description": "Updated disposable MCP U8 repository"},
            "description",
            "Updated disposable MCP U8 repository",
            project_id,
        ),
        (
            "webhook",
            {
                "name": f"mcp-disposable-u8-webhook-{suffix}",
                "webhook_type": "custom",
                "active": True,
            },
            {"active": False},
            "active",
            False,
            project_id,
        ),
    )
    created_records: list[tuple[str, str, str | None]] = []
    try:
        for (
            resource_type,
            create_payload,
            update_payload,
            updated_field,
            updated_value,
            scoped_project_id,
        ) in cases:
            kwargs = {"project_id": scoped_project_id} if scoped_project_id else {}
            if resource_type == "code_repository":
                allowed_prefixes = os.getenv(
                    "ZENML_MCP_ALLOWED_IMPORT_PREFIXES", "zenml."
                )
                with patch.dict(
                    os.environ,
                    {
                        "ZENML_MCP_ALLOWED_IMPORT_PREFIXES": (
                            f"{allowed_prefixes},{__name__}"
                        )
                    },
                    clear=False,
                ):
                    created = create_resource(
                        client, resource_type, payload=create_payload, **kwargs
                    )
            else:
                created = create_resource(
                    client, resource_type, payload=create_payload, **kwargs
                )
            created_id = created["resource_id"]
            assert created_id and str(uuid.UUID(created_id)) == created_id
            record = (resource_type, created_id, scoped_project_id)
            created_records.append(record)

            persisted = get_resource(
                client, resource_type, created_id, hydrate=True, **kwargs
            )
            assert persisted["item"]["id"] == created_id
            listed = list_resources(
                client,
                resource_type,
                filters={"id": created_id},
                page=1,
                size=2,
                **kwargs,
            )
            assert [item["id"] for item in listed["items"]] == [created_id]

            updated = update_resource(
                client,
                resource_type,
                created_id,
                payload=update_payload,
                **kwargs,
            )
            assert updated["outcome"] == "completed"
            persisted_update = get_resource(
                client, resource_type, created_id, hydrate=True, **kwargs
            )
            actual_value = _nested_value(persisted_update["item"], updated_field)
            assert actual_value == updated_value, (
                f"{resource_type} update did not persist {updated_field}: "
                f"expected {updated_value!r}, received {actual_value!r}"
            )

            deleted = delete_resource(client, resource_type, created_id, **kwargs)
            assert deleted["outcome"] == "completed"
            try:
                get_resource(client, resource_type, created_id, **kwargs)
            except ResourceNotFound:
                pass
            else:
                raise AssertionError(f"deleted disposable {resource_type} still exists")
            deleted_list = list_resources(
                client,
                resource_type,
                filters={"id": created_id},
                page=1,
                size=2,
                **kwargs,
            )
            assert not deleted_list["items"]
            created_records.remove(record)
    finally:
        active_error = sys.exc_info()[1]
        cleanup_errors: list[Exception] = []
        for resource_type, resource_id, scoped_project_id in reversed(created_records):
            kwargs = {"project_id": scoped_project_id} if scoped_project_id else {}
            try:
                delete_resource(client, resource_type, resource_id, **kwargs)
            except ResourceNotFound:
                pass
            # Cleanup must continue so every recorded UUID gets one attempt.
            except Exception as error:  # noqa: BLE001
                cleanup_errors.append(error)
        if cleanup_errors:
            message = "disposable CRUD cleanup failed for recorded resource UUIDs"
            if active_error is not None:
                active_error.add_note(message)
            else:
                raise ExceptionGroup(message, cleanup_errors)


def run_project_isolation(target: str) -> None:
    """Prove same-name models stay isolated in two disposable projects."""
    from zenml.client import Client
    from zenml_resource_dispatch import (
        ResourceNotFound,
        create_resource,
        delete_resource,
        get_resource,
        list_resources,
        update_resource,
    )

    client = Client()
    _require_client_target(client, target)
    suffix = uuid.uuid4().hex[:12]
    shared_name = f"mcp-disposable-u8-isolated-{suffix}"
    project_ids: list[str] = []
    model_ids: list[tuple[str, str]] = []
    try:
        for label in ("a", "b"):
            created = create_resource(
                client,
                "project",
                payload={
                    "name": f"{shared_name}-project-{label}",
                    "description": f"Disposable isolation project {label}",
                },
            )
            project_ids.append(created["resource_id"])

        for project_id in project_ids:
            with patch.dict(
                os.environ, {"ZENML_ACTIVE_PROJECT_ID": project_id}, clear=False
            ):
                created = create_resource(
                    client,
                    "model",
                    project_id=project_id,
                    payload={
                        "name": shared_name,
                        "description": f"Model in {project_id}",
                        "save_models_to_registry": False,
                    },
                )
            model_ids.append((created["resource_id"], project_id))

        assert model_ids[0][0] != model_ids[1][0]
        for model_id, project_id in model_ids:
            persisted = get_resource(
                client,
                "model",
                model_id,
                project_id=project_id,
                hydrate=True,
            )
            assert persisted["item"]["id"] == model_id
            listed = list_resources(
                client,
                "model",
                filters={"name": shared_name},
                project_id=project_id,
                page=1,
                size=2,
            )
            assert [item["id"] for item in listed["items"]] == [model_id]

        try:
            get_resource(
                client,
                "model",
                model_ids[0][0],
                project_id=project_ids[1],
            )
        except ResourceNotFound:
            pass
        else:
            raise AssertionError("exact model UUID crossed project scope")

        update_resource(
            client,
            "model",
            model_ids[0][0],
            project_id=project_ids[0],
            payload={"description": "Updated only in project a"},
        )
        first = get_resource(
            client,
            "model",
            model_ids[0][0],
            project_id=project_ids[0],
            hydrate=True,
        )
        second = get_resource(
            client,
            "model",
            model_ids[1][0],
            project_id=project_ids[1],
            hydrate=True,
        )
        assert _nested_value(first["item"], "description") == (
            "Updated only in project a"
        )
        assert _nested_value(second["item"], "description") != (
            "Updated only in project a"
        )
    finally:
        active_error = sys.exc_info()[1]
        cleanup_errors: list[Exception] = []
        for model_id, project_id in reversed(model_ids):
            try:
                delete_resource(client, "model", model_id, project_id=project_id)
            except ResourceNotFound:
                pass
            # Cleanup must continue so every recorded UUID gets one attempt.
            except Exception as error:  # noqa: BLE001
                cleanup_errors.append(error)
        for project_id in reversed(project_ids):
            try:
                delete_resource(client, "project", project_id)
            except ResourceNotFound:
                pass
            # Cleanup must continue so every recorded UUID gets one attempt.
            except Exception as error:  # noqa: BLE001
                cleanup_errors.append(error)
        if cleanup_errors:
            message = "project-isolation cleanup failed for recorded resource UUIDs"
            if active_error is not None:
                active_error.add_note(message)
            else:
                raise ExceptionGroup(message, cleanup_errors)


async def _call_restricted_mcp(
    target: str, restricted_api_key: str, project_id: str
) -> None:
    """Read a forbidden project through a separately credentialed MCP process."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "ZENML_STORE_API_KEY",
            "ZENML_ACTIVE_PROJECT_ID",
            "ZENML_MCP_DISPOSABLE_INTEGRATION",
            "ZENML_MCP_RESTRICTED_INTEGRATION",
            "ZENML_MCP_ACTION_INTEGRATION",
            "ZENML_MCP_ACTION_FIXTURE",
            "ZENML_MCP_REQUIRE_COMPLETE_INTEGRATION",
        }
    }
    env.update(
        {
            "ZENML_STORE_URL": target,
            "ZENML_STORE_API_KEY": restricted_api_key,
            "ZENML_MCP_ANALYTICS_ENABLED": "false",
            "ZENML_MCP_PROFILE": "compact",
            "ZENML_MCP_WRITE_POLICY": "read_only",
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1] / "server" / "zenml_server.py")],
        env=env,
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "zenml_get_resource",
                {"resource_type": "project", "resource_id": project_id},
            )
    if not result.is_error or result.structured_content is None:
        raise IntegrationIncomplete(
            "restricted MCP read did not return a structured permission error"
        )
    error = result.structured_content.get("error", {})
    if error.get("type") != "PermissionDenied" or project_id in str(result):
        raise IntegrationIncomplete(
            "restricted MCP read did not return the sanitized permission denial"
        )


def run_restricted_forbidden(target: str, restricted_api_key: str) -> None:
    """Require a real permission denial through the MCP implementation."""
    from zenml.client import Client
    from zenml_resource_dispatch import (
        ResourceNotFound,
        create_resource,
        delete_resource,
    )

    client = Client()
    _require_client_target(client, target)
    project_id: str | None = None
    try:
        created = create_resource(
            client,
            "project",
            payload={
                "name": f"mcp-disposable-u8-restricted-{uuid.uuid4().hex[:12]}",
                "description": "Restricted-credential denial target",
            },
        )
        project_id = created["resource_id"]
        asyncio.run(_call_restricted_mcp(target, restricted_api_key, project_id))
    finally:
        if project_id:
            try:
                delete_resource(client, "project", project_id)
            except ResourceNotFound:
                pass


def run_disposable_actions(target: str) -> None:
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
    _require_client_target(client, target)
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
                "webhook_type": "custom",
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
                "start_time": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
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
        active_error = sys.exc_info()[1]
        cleanup_errors: list[Exception] = []

        def cleanup_deployment() -> None:
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

        cleanup_steps: list[Callable[[], Any]] = [cleanup_deployment]
        if tag_attached and tag_id:
            cleanup_steps.append(
                lambda: action_resource(
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
            )
        if tag_id:
            cleanup_steps.append(lambda: delete_resource(client, "tag", tag_id))
        if trigger_attached and trigger_id:
            cleanup_steps.append(
                lambda: action_resource(
                    client,
                    "schedule_trigger",
                    "detach",
                    trigger_id,
                    project_id=project_id,
                    payload={"snapshot_id": fixture["snapshot_id"]},
                )
            )
        if trigger_id:
            cleanup_steps.append(
                lambda: delete_resource(
                    client, "schedule_trigger", trigger_id, project_id=project_id
                )
            )
        if webhook_id:
            cleanup_steps.append(
                lambda: delete_resource(
                    client, "webhook", webhook_id, project_id=project_id
                )
            )
        if replayed_run_id:
            cleanup_steps.append(
                lambda: delete_resource(
                    client, "pipeline_run", replayed_run_id, project_id=project_id
                )
            )
        for cleanup_step in cleanup_steps:
            try:
                cleanup_step()
            except Exception as error:  # noqa: BLE001
                cleanup_errors.append(error)
        if cleanup_errors:
            message = "action cleanup failed for one or more recorded resources"
            if active_error is not None:
                active_error.add_note(message)
            else:
                raise ExceptionGroup(message, cleanup_errors)


def test_integration_gate() -> None:
    from zenml.client import Client
    from zenml.config.source import Source

    repository_source = _disposable_code_repository_source()
    Client._validate_code_repository_config(
        Source.from_import_path(repository_source), {}
    )

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
        assert _restricted_api_key() is None
    with patch.dict(
        os.environ, {"ZENML_MCP_REQUIRE_COMPLETE_INTEGRATION": "1"}, clear=True
    ):
        try:
            _enabled_target()
        except IntegrationIncomplete as error:
            assert "ZENML_MCP_DISPOSABLE_INTEGRATION=1" in str(error)
        else:
            raise AssertionError("required release receipt silently skipped")
        try:
            _require_complete_subgates()
        except IntegrationIncomplete as error:
            assert "ZENML_MCP_RESTRICTED_INTEGRATION" in str(error)
            assert "ZENML_MCP_ACTION_INTEGRATION" in str(error)
        else:
            raise AssertionError("required release sub-gates silently skipped")
    with patch.dict(
        os.environ,
        {
            "ZENML_MCP_REQUIRE_COMPLETE_INTEGRATION": "1",
            "ZENML_MCP_RESTRICTED_INTEGRATION": "1",
            "ZENML_MCP_ACTION_INTEGRATION": "1",
        },
        clear=True,
    ):
        _require_complete_subgates()
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

    matching_client = Mock()
    matching_client.zen_store.url = "http://localhost:8237/"
    _require_client_target(matching_client, "http://localhost:8237")
    mismatched_client = Mock()
    mismatched_client.zen_store.url = "http://localhost:8238"
    try:
        _require_client_target(mismatched_client, "http://localhost:8237")
    except RuntimeError as error:
        assert "other than the approved" in str(error)
    else:
        raise AssertionError("SDK store mismatch passed the disposable safety guard")
    with patch.dict(os.environ, {"ZENML_MCP_RESTRICTED_INTEGRATION": "1"}, clear=True):
        try:
            _enabled_target()
        except IntegrationIncomplete as error:
            assert "ZENML_MCP_DISPOSABLE_INTEGRATION=1" in str(error)
        else:
            raise AssertionError("restricted integration ran without the base gate")
    with patch.dict(os.environ, {"ZENML_MCP_RESTRICTED_INTEGRATION": "1"}, clear=True):
        try:
            _restricted_api_key()
        except IntegrationIncomplete as error:
            assert "ZENML_MCP_RESTRICTED_API_KEY" in str(error)
        else:
            raise AssertionError("restricted integration ran without its credential")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"version": REQUIRED_ZENML_SERVER_VERSION}
    with patch("httpx.get", return_value=response):
        assert _verify_server_version("http://127.0.0.1:8237") == (
            REQUIRED_ZENML_SERVER_VERSION
        )
    response.json.return_value = {"version": "0.95.0"}
    with patch("httpx.get", return_value=response):
        try:
            _verify_server_version("http://127.0.0.1:8237")
        except IntegrationIncomplete as error:
            assert REQUIRED_ZENML_SERVER_VERSION in str(error)
        else:
            raise AssertionError("wrong disposable server version was accepted")
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
    try:
        target = _enabled_target()
        restricted_api_key = _restricted_api_key()
    except IntegrationIncomplete as error:
        print(f"INCOMPLETE: {error}", file=sys.stderr)
        return 1
    if target is None:
        print("SKIP: disposable resource integration is gated; no server was contacted")
        return 0
    try:
        _require_complete_subgates()
    except IntegrationIncomplete as error:
        print(f"INCOMPLETE: {error}", file=sys.stderr)
        return 1
    if os.getenv("ZENML_MCP_ACTION_INTEGRATION") == "1":
        _action_fixture()
    try:
        server_version = _verify_server_version(target)
    except (IntegrationIncomplete, ValueError) as error:
        print(f"INCOMPLETE: {error}", file=sys.stderr)
        return 1
    run_disposable_crud(target)
    print(
        "PASS: disposable CRUD persisted on "
        f"ZenML {server_version} loopback target {target}"
    )
    run_project_isolation(target)
    print(f"PASS: same-name project isolation persisted on loopback target {target}")
    if restricted_api_key:
        try:
            run_restricted_forbidden(target, restricted_api_key)
        except IntegrationIncomplete as error:
            print(f"INCOMPLETE: {error}", file=sys.stderr)
            return 1
        print(
            f"PASS: restricted MCP credential received PermissionDenied from {target}"
        )
    else:
        print("SKIP: restricted-credential integration is separately gated")
    if os.getenv("ZENML_MCP_ACTION_INTEGRATION") == "1":
        run_disposable_actions(target)
        print(f"PASS: disposable action persisted on loopback target {target}")
    else:
        print("SKIP: disposable action integration is separately gated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
