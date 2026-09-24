"""Validated dispatch for generic ZenML resource reads and mutations."""

from __future__ import annotations

import json
import os
import re
import ssl
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

import requests
from zenml_resource_registry import (
    ACTION_REGISTRY,
    DATETIME_FILTERS,
    MAX_PAGE_SIZE,
    RESOURCE_REGISTRY,
    ResourceRegistryError,
    ResourceSpec,
    get_action_spec,
    get_resource_spec,
    validate_action_payload,
    validate_filter_value,
    validate_mutation_payload,
)
from zenml_tool_catalog import configured_write_policy


class ResourceDispatchError(ValueError):
    """Base class for locally detectable generic-resource errors."""


class ResourceFeatureUnavailable(ResourceDispatchError):
    """The server supports the resource, but the backing feature is disabled."""


class ResourcePermissionDenied(ResourceDispatchError):
    """The authenticated principal cannot read the requested resource."""


class ResourceNotFound(ResourceDispatchError):
    """An exact resource or required parent does not exist."""


class ResourceReadOnly(ResourcePermissionDenied):
    """The operator disabled all mutating tools."""


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$"
)
_SPACE_FRAC_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+$")
_RANGE_RE = re.compile(r"^range:(?P<lower>.+?)\.\.(?P<upper>.+)$")
_KNOWN_OPS = frozenset(
    {
        "equals",
        "notequals",
        "contains",
        "startswith",
        "endswith",
        "oneof",
        "notoneof",
        "gte",
        "gt",
        "lte",
        "lt",
        "in",
    }
)


def _parse_iso_to_zenml(value: str) -> str | None:
    try:
        adjusted = value.replace("Z", "+00:00") if value.endswith("Z") else value
        parsed = datetime.fromisoformat(adjusted)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _normalize_datetime_token(value: str, *, upper_bound: bool) -> str:
    value = value.strip()
    if _ISO_DT_RE.match(value):
        parsed = _parse_iso_to_zenml(value)
        if parsed:
            return parsed
    fractional = _SPACE_FRAC_RE.match(value)
    if fractional:
        return fractional.group(1)
    if _DATE_ONLY_RE.match(value):
        return f"{value} {'23:59:59' if upper_bound else '00:00:00'}"
    return value


def normalize_datetime_filter(value: str) -> str:
    """Normalize a generic list datetime filter without touching action data."""
    raw = value.strip()
    if not raw:
        return value
    range_match = _RANGE_RE.match(raw)
    if range_match:
        lower = _normalize_datetime_token(range_match.group("lower"), upper_bound=False)
        upper = _normalize_datetime_token(range_match.group("upper"), upper_bound=True)
        return f"in:{lower},{upper}"
    head, separator, tail = raw.partition(":")
    operation, operand = (
        (head, tail) if separator and head in _KNOWN_OPS else (None, raw)
    )
    if operation == "in" and "," in operand:
        lower, upper = operand.split(",", 1)
        return (
            f"in:{_normalize_datetime_token(lower, upper_bound=False)},"
            f"{_normalize_datetime_token(upper, upper_bound=True)}"
        )
    normalized = _normalize_datetime_token(
        operand, upper_bound=operation in {"lte", "lt"}
    )
    return f"{operation}:{normalized}" if operation else normalized


def _validate_filter_expression(value: Any) -> None:
    if not isinstance(value, str):
        return
    operation, separator, operand = value.partition(":")
    if not separator or operation not in {"oneof", "notoneof"}:
        return
    try:
        decoded = json.loads(operand)
    except json.JSONDecodeError as error:
        raise ResourceDispatchError(
            'List filters require a JSON array, for example oneof:["running","error"].'
        ) from error
    if not isinstance(decoded, list):
        raise ResourceDispatchError(
            'List filters require a JSON array, for example oneof:["running","error"].'
        )


def _has_filter_operator(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    operation, separator, _ = value.partition(":")
    return bool(separator and operation in _KNOWN_OPS)


def _model_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return value


_SENSITIVE_KEY_PARTS = frozenset(
    {
        "accesskey",
        "apikey",
        "authkey",
        "credential",
        "databaseurl",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "signingkey",
        "token",
    }
)
_OPAQUE_SENSITIVE_FIELDS = frozenset(
    {
        "config",
        "configuration",
        "environment",
        "environmentvariables",
        "parameters",
        "secrets",
        "settings",
        "values",
    }
)


def _is_sensitive_key(key: str) -> bool:
    collapsed = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(part in collapsed for part in _SENSITIVE_KEY_PARTS)


def _is_opaque_sensitive_field(key: str) -> bool:
    collapsed = re.sub(r"[^a-z0-9]", "", key.lower())
    return collapsed in _OPAQUE_SENSITIVE_FIELDS or collapsed.endswith(
        ("config", "configuration", "environment", "parameters", "settings")
    )


def safe_project(value: Any, *, resource_type: str) -> Any:
    """Recursively convert SDK models to JSON-safe data and omit credentials."""
    return _safe_project(value, resource_type=resource_type)


def _safe_project(value: Any, *, resource_type: str) -> Any:
    value = _model_dump(value)
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if _is_sensitive_key(lowered):
                continue
            if _is_opaque_sensitive_field(lowered):
                continue
            if resource_type == "secret" and lowered in {
                "value",
                "values",
                "secrets",
                "secret",
            }:
                continue
            if resource_type == "service" and lowered in {
                "config",
                "endpoint",
                "service_source",
                "status",
            }:
                continue
            projected[key] = _safe_project(
                child,
                resource_type=resource_type,
            )
        return projected
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _safe_project(
                item,
                resource_type=resource_type,
            )
            for item in value
        ]
    return value


ListAdapter = Callable[[Any, dict[str, Any]], Any]
GetAdapter = Callable[[Any, str, dict[str, Any]], Any]


def _list_resource_requests(client: Any, kwargs: dict[str, Any]) -> Any:
    from zenml.models import ResourceRequestFilter

    hydrate = kwargs.pop("hydrate", False)
    filter_model = ResourceRequestFilter(**kwargs)
    return client.zen_store.list_resource_requests(
        filter_model=filter_model, hydrate=hydrate
    )


def _get_resource_request(client: Any, resource_id: str, kwargs: dict[str, Any]) -> Any:
    del kwargs
    return client.zen_store.get_resource_request(
        resource_request_id=resource_id, hydrate=False
    )


# Every callable below names a released ZenML 0.97.0 method in source.  The
# resource string only indexes this fixed map; it is never used with getattr.
LIST_ADAPTERS: Mapping[str, ListAdapter] = MappingProxyType(
    {
        "project": lambda c, k: c.list_projects(**k),
        "user": lambda c, k: c.list_users(**k),
        "stack": lambda c, k: c.list_stacks(**k),
        "stack_component": lambda c, k: c.list_stack_components(**k),
        "flavor": lambda c, k: c.list_flavors(**k),
        "service": lambda c, k: c.list_services(**k),
        "pipeline": lambda c, k: c.list_pipelines(**k),
        "pipeline_run": lambda c, k: c.list_pipeline_runs(**k),
        "run_step": lambda c, k: c.list_run_steps(**k),
        "snapshot": lambda c, k: c.list_snapshots(**k),
        "build": lambda c, k: c.list_builds(**k),
        "run_template": lambda c, k: c.list_run_templates(**k),
        "deployment": lambda c, k: c.list_deployments(**k),
        "schedule": lambda c, k: c.list_schedules(**k),
        "artifact": lambda c, k: c.list_artifacts(**k),
        "artifact_version": lambda c, k: c.list_artifact_versions(**k),
        "model": lambda c, k: c.list_models(**k),
        "model_version": lambda c, k: c.list_model_versions(**k),
        "tag": lambda c, k: c.list_tags(**k),
        "secret": lambda c, k: c.list_secrets(**k),
        "service_connector": lambda c, k: c.list_service_connectors(**k),
        "service_connector_type": lambda c, k: c.list_service_connector_types(**k),
        "code_repository": lambda c, k: c.list_code_repositories(**k),
        "webhook": lambda c, k: c.list_webhooks(**k),
        "schedule_trigger": lambda c, k: c.list_schedule_triggers(**k),
        "platform_event_trigger": lambda c, k: c.list_platform_event_triggers(**k),
        "webhook_trigger": lambda c, k: c.list_webhook_triggers(**k),
        "resource_request": _list_resource_requests,
        "run_wait_condition": lambda c, k: c.list_run_wait_conditions(**k),
        "hook_invocation": lambda c, k: c.list_hook_invocations(**k),
    }
)


GET_ADAPTERS: Mapping[str, GetAdapter] = MappingProxyType(
    {
        "project": lambda c, i, k: c.get_project(name_id_or_prefix=i, **k),
        "user": lambda c, i, k: c.get_user(name_id_or_prefix=i, **k),
        "stack": lambda c, i, k: c.get_stack(name_id_or_prefix=i, **k),
        "stack_component": lambda c, i, k: c.get_stack_component(
            name_id_or_prefix=i, **k
        ),
        "flavor": lambda c, i, k: c.get_flavor(name_id_or_prefix=i, **k),
        "service": lambda c, i, k: c.get_service(name_id_or_prefix=i, **k),
        "pipeline": lambda c, i, k: c.get_pipeline(name_id_or_prefix=i, **k),
        "pipeline_run": lambda c, i, k: c.get_pipeline_run(name_id_or_prefix=i, **k),
        "run_step": lambda c, i, k: c.get_run_step(step_run_id=i, **k),
        "snapshot": lambda c, i, k: c.get_snapshot(name_id_or_prefix=i, **k),
        "build": lambda c, i, k: c.get_build(id_or_prefix=i, **k),
        "run_template": lambda c, i, k: c.get_run_template(name_id_or_prefix=i, **k),
        "deployment": lambda c, i, k: c.get_deployment(name_id_or_prefix=i, **k),
        "schedule": lambda c, i, k: c.get_schedule(name_id_or_prefix=i, **k),
        "artifact": lambda c, i, k: c.get_artifact(name_id_or_prefix=i, **k),
        "artifact_version": lambda c, i, k: c.get_artifact_version(
            name_id_or_prefix=i, **k
        ),
        "model": lambda c, i, k: c.get_model(model_name_or_id=i, **k),
        "model_version": lambda c, i, k: c.get_model_version(
            model_version_name_or_number_or_id=i, **k
        ),
        "tag": lambda c, i, k: c.get_tag(tag_name_or_id=i, **k),
        "service_connector": lambda c, i, k: c.get_service_connector(
            name_id_or_prefix=i, **k
        ),
        "service_connector_type": lambda c, i, k: c.get_service_connector_type(
            connector_type=i, **k
        ),
        "code_repository": lambda c, i, k: c.get_code_repository(
            name_id_or_prefix=i, **k
        ),
        "webhook": lambda c, i, k: c.get_webhook(name_id_or_prefix=i, **k),
        "schedule_trigger": lambda c, i, k: c.get_schedule_trigger(
            trigger_name_id_or_prefix=i, **k
        ),
        "platform_event_trigger": lambda c, i, k: c.get_platform_event_trigger(
            trigger_name_id_or_prefix=i, **k
        ),
        "webhook_trigger": lambda c, i, k: c.get_webhook_trigger(
            trigger_name_id_or_prefix=i, **k
        ),
        "resource_request": _get_resource_request,
        "hook_invocation": lambda c, i, k: c.get_hook_invocation(
            hook_invocation_id=i, **k
        ),
    }
)


def _effective_scope(
    client: Any, spec: ResourceSpec, project_id: str | None
) -> tuple[dict[str, Any], str | None]:
    if spec.scope == "global":
        if project_id is not None:
            raise ResourceDispatchError(
                f"{spec.resource_type!r} is global and does not accept project_id"
            )
        return {"kind": "global"}, None
    if project_id is not None:
        try:
            normalized_project_id = str(uuid.UUID(project_id.strip()))
        except (AttributeError, ValueError) as error:
            raise ResourceDispatchError(
                "project_id must be a non-empty UUID"
            ) from error
        return {
            "kind": "project",
            "project_id": normalized_project_id,
            "source": "requested",
        }, normalized_project_id
    active_project = client.active_project
    active_id = str(active_project.id)
    return {
        "kind": "project",
        "project_id": active_id,
        "source": "active_project",
    }, active_id


def _normalize_parent_id(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except (AttributeError, ValueError) as error:
        raise ResourceDispatchError(f"{field} must be a non-empty UUID") from error


def _exact_uuid(field: str, value: str | None) -> uuid.UUID:
    if not isinstance(value, str) or value != value.strip():
        raise ResourceDispatchError(f"{field} must be an exact UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise ResourceDispatchError(f"{field} must be an exact UUID") from error
    if str(parsed) != value.lower():
        raise ResourceDispatchError(f"{field} must be an exact UUID")
    return parsed


def writes_are_disabled() -> bool:
    """Return the operator-selected write policy."""
    return configured_write_policy() == "read_only"


def ensure_writes_enabled(*, read_only: bool | None = None) -> None:
    """Reject every retained or generic mutation through one policy check."""
    if writes_are_disabled() if read_only is None else read_only:
        raise ResourceReadOnly("ZenML MCP mutations are disabled by operator policy")


def _invoke_adapter(call: Callable[[], Any]) -> Any:
    """Translate stable SDK exception classes without exposing their messages."""
    try:
        return call()
    except Exception as error:
        error_name = type(error).__name__
        if error_name in {
            "ForbiddenError",
            "IllegalOperationError",
            "PermissionDenied",
        }:
            raise ResourcePermissionDenied("Permission denied") from error
        if error_name in {
            "FeatureDisabledError",
            "FeatureNotEnabledError",
            "NotImplementedError",
            "SubscriptionUpgradeRequiredError",
        }:
            raise ResourceFeatureUnavailable("Feature unavailable") from error
        if error_name in {
            "DoesNotExistException",
            "EntityNotFoundError",
            "KeyError",
            "NotFoundError",
            "ZenKeyError",
        }:
            raise ResourceNotFound("Resource not found") from error
        raise


def _validate_pagination(page: int, size: int) -> None:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ResourceDispatchError(
            "page must be an integer greater than or equal to 1"
        )
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not 1 <= size <= MAX_PAGE_SIZE
    ):
        raise ResourceDispatchError(f"size must be between 1 and {MAX_PAGE_SIZE}")


def _list_kwargs(
    spec: ResourceSpec,
    filters: Mapping[str, Any] | None,
    *,
    page: int,
    size: int,
    project_id: str | None,
) -> dict[str, Any]:
    supplied = dict(filters or {})
    unsupported = sorted(set(supplied) - set(spec.list_filters))
    if unsupported:
        raise ResourceDispatchError(
            f"Unsupported filters for {spec.resource_type!r}: {', '.join(unsupported)}. "
            f"Allowed filters: {', '.join(spec.list_filters)}"
        )
    missing = [
        field for field in spec.list_required_filters if supplied.get(field) is None
    ]
    if missing:
        raise ResourceDispatchError(
            f"{spec.resource_type!r} list requires filters: {', '.join(missing)}"
        )
    for key, value in supplied.items():
        try:
            validate_filter_value(spec.resource_type, key, value)
        except ResourceRegistryError as error:
            raise ResourceDispatchError(str(error)) from error
        _validate_filter_expression(value)
        if key in DATETIME_FILTERS:
            if isinstance(value, str):
                supplied[key] = normalize_datetime_filter(value)
            elif isinstance(value, list):
                supplied[key] = [normalize_datetime_filter(item) for item in value]
        elif isinstance(value, list) and not any(
            _has_filter_operator(item) for item in value
        ):
            supplied[key] = f"oneof:{json.dumps(value, separators=(',', ':'))}"
    if spec.resource_type == "artifact_version":
        supplied["artifact"] = supplied.pop("artifact_id")
    elif spec.resource_type == "model_version":
        supplied["model"] = supplied.pop("model_id")
    if spec.resource_type in {"schedule_trigger", "platform_event_trigger"}:
        from zenml.enums import TriggerFlavor

        supplied["flavor"] = (
            TriggerFlavor.NATIVE_SCHEDULE
            if spec.resource_type == "schedule_trigger"
            else TriggerFlavor.PLATFORM_EVENT
        )
    if project_id is not None:
        supplied["project"] = project_id
    if spec.non_paginated:
        return supplied
    supplied.update({"page": page, "size": size, "hydrate": False})
    if spec.resource_type == "service_connector":
        supplied["expand_secrets"] = False
    return supplied


def _compact_service_connector_list_item(value: Any) -> Any:
    dumped = _model_dump(value)
    if not isinstance(dumped, Mapping):
        return dumped
    compact = dict(dumped)
    body = _model_dump(compact.get("body"))
    if not isinstance(body, Mapping):
        return compact
    compact_body = dict(body)
    connector_type = _model_dump(compact_body.get("connector_type"))
    if isinstance(connector_type, Mapping):
        compact_body["connector_type"] = connector_type.get(
            "connector_type"
        ) or connector_type.get("name")
    compact["body"] = compact_body
    return compact


def _page_payload(
    result: Any, *, resource_type: str, page: int, size: int, non_paginated: bool
) -> dict[str, Any]:
    dumped = _model_dump(result)
    if non_paginated:
        all_items = list(
            dumped
            if isinstance(dumped, Sequence)
            and not isinstance(dumped, (str, bytes, bytearray))
            else []
        )
        total = len(all_items)
        start = (page - 1) * size
        items = all_items[start : start + size]
        return {
            "items": safe_project(items, resource_type=resource_type),
            "total": total,
            "page": page,
            "size": size,
        }
    if not isinstance(dumped, Mapping):
        raise ResourceDispatchError(
            f"{resource_type!r} list adapter returned a non-page result"
        )
    raw_items = dumped.get("items", [])
    if (
        resource_type == "service_connector"
        and isinstance(raw_items, Sequence)
        and not isinstance(raw_items, (str, bytes, bytearray))
    ):
        raw_items = [_compact_service_connector_list_item(item) for item in raw_items]
    total = dumped.get(
        "total", len(raw_items) if isinstance(raw_items, Sequence) else 0
    )
    return {
        "items": safe_project(raw_items, resource_type=resource_type),
        "total": int(total),
        "page": int(dumped.get("page", dumped.get("index", page))),
        "size": int(dumped.get("size", dumped.get("max_size", size))),
    }


def list_resources(
    client: Any,
    resource_type: str,
    *,
    filters: Mapping[str, Any] | None = None,
    project_id: str | None = None,
    page: int = 1,
    size: int | None = None,
) -> dict[str, Any]:
    """Validate and run one allowlisted list adapter."""
    spec = get_resource_spec(resource_type)
    effective_size = spec.default_size if size is None else size
    _validate_pagination(page, effective_size)
    effective_scope, sdk_project = _effective_scope(client, spec, project_id)
    kwargs = _list_kwargs(
        spec, filters, page=page, size=effective_size, project_id=sdk_project
    )
    result = _invoke_adapter(lambda: LIST_ADAPTERS[resource_type](client, kwargs))
    payload = _page_payload(
        result,
        resource_type=resource_type,
        page=page,
        size=effective_size,
        non_paginated=spec.non_paginated,
    )
    return {
        "resource_type": resource_type,
        **payload,
        "effective_scope": effective_scope,
    }


def _find_nested_id(value: Any, direct_key: str, nested_key: str) -> str | None:
    """Find a response relation in the SDK's body/metadata/resources nesting."""
    dumped = _model_dump(value)
    if isinstance(dumped, Mapping):
        direct = dumped.get(direct_key)
        if direct is not None:
            return str(direct)
        nested = _model_dump(dumped.get(nested_key))
        if isinstance(nested, Mapping) and nested.get("id") is not None:
            return str(nested["id"])
        for child in dumped.values():
            found = _find_nested_id(child, direct_key, nested_key)
            if found is not None:
                return found
    elif isinstance(dumped, Sequence) and not isinstance(
        dumped, (str, bytes, bytearray)
    ):
        for child in dumped:
            found = _find_nested_id(child, direct_key, nested_key)
            if found is not None:
                return found
    return None


def _find_nested_value(value: Any, key: str) -> Any:
    dumped = _model_dump(value)
    if isinstance(dumped, Mapping):
        if key in dumped:
            return dumped[key]
        for child in dumped.values():
            found = _find_nested_value(child, key)
            if found is not None:
                return found
    elif isinstance(dumped, Sequence) and not isinstance(
        dumped, (str, bytes, bytearray)
    ):
        for child in dumped:
            found = _find_nested_value(child, key)
            if found is not None:
                return found
    return None


def _component_type_value(value: Any) -> str | None:
    """Normalize SDK component-type enum and string representations."""
    if value is None:
        return None
    raw_value = getattr(value, "value", value)
    return str(raw_value).rsplit(".", 1)[-1].lower()


def get_resource(
    client: Any,
    resource_type: str,
    resource_id: str,
    *,
    project_id: str | None = None,
    artifact_id: str | None = None,
    model_id: str | None = None,
    pipeline_run_id: str | None = None,
    component_type: str | None = None,
    hydrate: bool | None = None,
) -> dict[str, Any]:
    """Validate and run one allowlisted get adapter."""
    spec = get_resource_spec(resource_type)
    spec.operation_spec("get")
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise ResourceDispatchError("resource_id must be a non-empty identifier")
    if hydrate is not None and not isinstance(hydrate, bool):
        raise ResourceDispatchError("hydrate must be a boolean")
    supplied_parents = {
        "artifact_id": _normalize_parent_id("artifact_id", artifact_id),
        "model_id": _normalize_parent_id("model_id", model_id),
        "pipeline_run_id": _normalize_parent_id("pipeline_run_id", pipeline_run_id),
        "component_type": component_type,
    }
    required = [field for field in spec.get_required if field != "resource_id"]
    missing = [field for field in required if supplied_parents.get(field) is None]
    if missing:
        raise ResourceDispatchError(
            f"{resource_type!r} get requires: {', '.join(missing)}"
        )
    unexpected = sorted(
        field
        for field, value in supplied_parents.items()
        if value is not None and field not in (spec.get_fields or ())
    )
    if unexpected:
        raise ResourceDispatchError(
            f"Unexpected identifiers for {resource_type!r}: {', '.join(unexpected)}"
        )
    artifact_id = supplied_parents["artifact_id"]
    model_id = supplied_parents["model_id"]
    pipeline_run_id = supplied_parents["pipeline_run_id"]
    normalized_component_type: Any = component_type
    if resource_type == "stack_component":
        from zenml.enums import StackComponentType

        try:
            normalized_component_type = StackComponentType(component_type)
        except (TypeError, ValueError) as error:
            raise ResourceDispatchError("Invalid component_type") from error
    effective_scope, sdk_project = _effective_scope(client, spec, project_id)
    kwargs: dict[str, Any] = {}
    if sdk_project is not None and resource_type not in {"run_step", "hook_invocation"}:
        kwargs["project"] = sdk_project
    if resource_type == "stack_component":
        kwargs["component_type"] = normalized_component_type
    elif resource_type == "model_version":
        kwargs["model_name_or_id"] = model_id
    kwargs["hydrate"] = (
        resource_type in {"run_step", "tag"} if hydrate is None else hydrate
    )
    if resource_type == "service_connector":
        kwargs["expand_secrets"] = False
    if resource_type in {"service_connector_type", "resource_request"}:
        kwargs.pop("hydrate", None)
    item = _invoke_adapter(
        lambda: GET_ADAPTERS[resource_type](client, resource_id, kwargs)
    )
    if resource_type == "stack_component":
        observed_component_type = _component_type_value(
            _find_nested_value(item, "type")
        )
        if observed_component_type != _component_type_value(normalized_component_type):
            raise ResourceNotFound(
                "'stack_component' was not found for the supplied component_type"
            )
    relation_checks = {
        "artifact_version": (artifact_id, "artifact_id", "artifact"),
        "model_version": (model_id, "model_id", "model"),
        "run_step": (pipeline_run_id, "pipeline_run_id", "pipeline_run"),
    }
    if resource_type in relation_checks:
        expected, direct_key, nested_key = relation_checks[resource_type]
        observed = _find_nested_id(item, direct_key, nested_key)
        if observed is None or observed != str(expected):
            raise ResourceNotFound(
                f"{resource_type!r} does not belong to the supplied parent"
            )
    if spec.scope == "project":
        observed_project = _find_nested_id(item, "project_id", "project")
        if observed_project is None or observed_project != str(sdk_project):
            raise ResourceNotFound(
                f"{resource_type!r} was not found in the effective project"
            )
    return {
        "resource_type": resource_type,
        "item": safe_project(item, resource_type=resource_type),
        "effective_scope": effective_scope,
    }


_PROJECT_ACTIVE_CREATES = frozenset(
    {"service", "run_template", "model", "code_repository", "webhook"}
)
_TRIGGER_TYPES = frozenset(
    {"schedule_trigger", "platform_event_trigger", "webhook_trigger"}
)


def _mutation_scope(
    client: Any, resource_type: str, project_id: str | None, *, operation: str
) -> tuple[dict[str, Any], str | None]:
    spec = get_resource_spec(resource_type)
    if spec.scope == "global" and resource_type not in _PROJECT_ACTIVE_CREATES:
        return _effective_scope(client, spec, project_id)
    if project_id is None:
        raise ResourceDispatchError(
            f"project_id is required for {resource_type!r} {operation}"
        )
    requested = str(_exact_uuid("project_id", project_id))
    if operation == "create" and resource_type in _PROJECT_ACTIVE_CREATES:
        active = str(client.active_project.id)
        if active != requested:
            raise ResourceDispatchError(
                "The requested project is not the client's active project"
            )
    return {
        "kind": "project",
        "project_id": requested,
        "source": "requested",
    }, requested


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _allowed_import_source(source: str) -> None:
    prefixes = tuple(
        item.strip().rstrip(".")
        for item in os.getenv("ZENML_MCP_ALLOWED_IMPORT_PREFIXES", "zenml.").split(",")
        if item.strip()
    )
    module = source.split(":", 1)[0]
    allowed = any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes
    )
    if not allowed:
        raise ResourceDispatchError(
            "source is outside ZENML_MCP_ALLOWED_IMPORT_PREFIXES"
        )


def _validate_related(
    client: Any,
    resource_type: str,
    resource_id: str,
    *,
    project_id: str | None,
    artifact_id: str | None = None,
    model_id: str | None = None,
    component_type: str | None = None,
) -> dict[str, Any]:
    result = get_resource(
        client,
        resource_type,
        resource_id,
        project_id=project_id,
        artifact_id=artifact_id,
        model_id=model_id,
        component_type=component_type,
    )
    observed = _find_nested_id(result["item"], "id", "id")
    if observed != resource_id:
        raise ResourceNotFound(
            f"{resource_type!r} did not resolve to the exact supplied UUID"
        )
    return result


def _validate_model_version_project(
    client: Any, model_version_id: str, project_id: str | None
) -> None:
    identifier = _exact_uuid("model_version_id", model_version_id)
    item = _invoke_adapter(
        lambda: client.get_model_version(
            model_version_name_or_number_or_id=identifier,
            project=project_id,
            hydrate=False,
        )
    )
    observed_id = _find_nested_id(item, "id", "id")
    observed_project = _find_nested_id(item, "project_id", "project")
    if observed_id != str(identifier) or observed_project != project_id:
        raise ResourceNotFound(
            "The model version was not found in the requested project"
        )


def _validate_create_relations(
    client: Any,
    resource_type: str,
    payload: dict[str, Any],
    project_id: str | None,
    model_id: str | None,
    *,
    require_core_stack_components: bool = True,
) -> None:
    if resource_type == "run_template":
        _validate_related(
            client,
            "snapshot",
            str(_exact_uuid("snapshot_id", payload["snapshot_id"])),
            project_id=project_id,
        )
    elif resource_type == "model_version":
        _validate_related(
            client,
            "model",
            str(_exact_uuid("model_id", model_id)),
            project_id=project_id,
        )
    elif resource_type == "webhook_trigger":
        _validate_related(
            client,
            "webhook",
            str(_exact_uuid("webhook_id", payload["webhook_id"])),
            project_id=project_id,
        )
    elif resource_type == "platform_event_trigger":
        source_resource = {
            "pipeline": "pipeline",
            "pipeline_run": "pipeline_run",
            "pipeline_snapshot": "snapshot",
        }[payload["source_type"]]
        _validate_related(
            client,
            source_resource,
            str(_exact_uuid("source_id", payload["source_id"])),
            project_id=project_id,
        )
    elif resource_type == "service" and payload.get("model_version_id"):
        _validate_model_version_project(client, payload["model_version_id"], project_id)
    elif resource_type == "stack":
        from zenml.enums import StackComponentType

        missing_types = {"orchestrator", "artifact_store"} - set(payload["components"])
        if require_core_stack_components and missing_types:
            raise ResourceDispatchError(
                "stack create requires orchestrator and artifact_store component bindings"
            )
        for component_type, identifiers in payload["components"].items():
            try:
                StackComponentType(component_type)
            except ValueError as error:
                raise ResourceDispatchError(
                    f"Invalid stack component type {component_type!r}"
                ) from error
            values = identifiers if isinstance(identifiers, list) else [identifiers]
            for identifier in values:
                _validate_related(
                    client,
                    "stack_component",
                    str(_exact_uuid("component_id", identifier)),
                    project_id=None,
                    component_type=component_type,
                )
    elif resource_type == "stack_component" and payload.get("connector_id"):
        _validate_related(
            client,
            "service_connector",
            str(_exact_uuid("connector_id", payload["connector_id"])),
            project_id=None,
        )


def _create_call(
    client: Any,
    resource_type: str,
    payload: dict[str, Any],
    project: str | None,
    model_id: str | None,
) -> Any:
    if resource_type == "project":
        return client.create_project(**payload)
    if resource_type == "stack":
        from zenml.enums import StackComponentType

        components = {
            StackComponentType(key): value
            for key, value in payload["components"].items()
        }
        return client.create_stack(name=payload["name"], components=components)
    if resource_type == "stack_component":
        from zenml.enums import StackComponentType

        kwargs = dict(payload)
        kwargs["component_type"] = StackComponentType(kwargs["component_type"])
        return client.create_stack_component(**kwargs)
    if resource_type == "flavor":
        from zenml.enums import StackComponentType

        return client.create_flavor(
            source=payload["source"],
            component_type=StackComponentType(payload["component_type"]),
        )
    if resource_type == "service":
        from zenml.models.v2.misc.service import ServiceType
        from zenml.services import ServiceConfig

        config = ServiceConfig(**payload["config"])
        if not config.name and not config.model_name:
            raise ResourceDispatchError("service config requires name or model_name")
        return client.create_service(
            config=config,
            service_type=ServiceType(**payload["service_type"]),
            model_version_id=(
                _exact_uuid("model_version_id", payload["model_version_id"])
                if payload.get("model_version_id")
                else None
            ),
        )
    if resource_type == "run_template":
        kwargs = dict(payload)
        kwargs["snapshot_id"] = _exact_uuid("snapshot_id", kwargs["snapshot_id"])
        return client.create_run_template(**kwargs)
    if resource_type == "model":
        return client.create_model(**payload)
    if resource_type == "model_version":
        return client.create_model_version(
            model_name_or_id=_exact_uuid("model_id", model_id),
            project=project,
            **payload,
        )
    if resource_type == "tag":
        return client.create_tag(**payload)
    if resource_type == "service_connector":
        kwargs = dict(payload)
        if "expires_at" in kwargs:
            kwargs["expires_at"] = _parse_datetime(kwargs["expires_at"])
        result, _ = client.create_service_connector(
            **kwargs,
            auto_configure=False,
            verify=False,
            list_resources=False,
            register=True,
        )
        return result
    if resource_type == "code_repository":
        from zenml.config.source import Source

        kwargs = dict(payload)
        kwargs["source"] = Source.from_import_path(kwargs["source"])
        return client.create_code_repository(**kwargs)
    if resource_type == "webhook":
        return client.create_webhook(**payload)
    if resource_type == "schedule_trigger":
        from zenml.enums import TriggerRunConcurrency

        modes = [
            key
            for key in ("cron_expression", "interval", "run_once_start_time")
            if key in payload
        ]
        if len(modes) != 1:
            raise ResourceDispatchError("Exactly one schedule mode is required")
        if "interval" in payload and "start_time" not in payload:
            raise ResourceDispatchError("Interval schedules require start_time")
        kwargs = _trigger_kwargs(payload)
        kwargs["concurrency"] = TriggerRunConcurrency(kwargs.get("concurrency", "skip"))
        return client.create_schedule_trigger(project_id=project, **kwargs)
    if resource_type == "platform_event_trigger":
        from zenml.enums import SourceType, TriggerRunConcurrency

        kwargs = dict(payload)
        kwargs["source_type"] = SourceType(kwargs["source_type"])
        kwargs["source_id"] = _exact_uuid("source_id", kwargs["source_id"])
        kwargs["concurrency"] = TriggerRunConcurrency(kwargs.get("concurrency", "skip"))
        return client.create_platform_event_trigger(project_id=project, **kwargs)
    if resource_type == "webhook_trigger":
        from zenml.enums import TriggerRunConcurrency

        kwargs = dict(payload)
        kwargs["webhook"] = _exact_uuid("webhook_id", kwargs.pop("webhook_id"))
        kwargs["concurrency"] = TriggerRunConcurrency(kwargs.get("concurrency", "skip"))
        return client.create_webhook_trigger(project_id=project, **kwargs)
    raise ResourceDispatchError(f"Unsupported create for {resource_type!r}")


def _trigger_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    kwargs = dict(payload)
    modes = [
        key
        for key in ("cron_expression", "interval", "run_once_start_time")
        if key in kwargs
    ]
    if len(modes) > 1:
        raise ResourceDispatchError("Only one schedule mode may be supplied")
    for field in ("run_once_start_time", "start_time", "end_time"):
        if field in kwargs:
            kwargs[field] = _parse_datetime(kwargs[field])
    return kwargs


def _update_call(
    client: Any,
    resource_type: str,
    target: uuid.UUID,
    payload: dict[str, Any],
    project: str | None,
    *,
    model_id: str | None,
    component_type: str | None,
) -> Any:
    kwargs = dict(payload)
    if resource_type == "project":
        return client.update_project(
            name_id_or_prefix=target,
            new_name=kwargs.get("name"),
            new_display_name=kwargs.get("display_name"),
            new_description=kwargs.get("description"),
        )
    if resource_type == "stack":
        from zenml.enums import StackComponentType

        updates = kwargs.get("component_updates")
        if updates is not None:
            kwargs["component_updates"] = {
                StackComponentType(key): value for key, value in updates.items()
            }
        return client.update_stack(name_id_or_prefix=target, **kwargs)
    if resource_type == "stack_component":
        from zenml.enums import StackComponentType

        if kwargs.get("connector_id"):
            kwargs["connector_id"] = _exact_uuid("connector_id", kwargs["connector_id"])
        return client.update_stack_component(
            name_id_or_prefix=target,
            component_type=StackComponentType(component_type),
            **kwargs,
        )
    if resource_type == "service":
        if "admin_state" in kwargs:
            from zenml.enums import ServiceState

            kwargs["admin_state"] = ServiceState(kwargs["admin_state"])
        if kwargs.get("model_version_id"):
            kwargs["model_version_id"] = _exact_uuid(
                "model_version_id", kwargs["model_version_id"]
            )
        return client.update_service(id=target, **kwargs)
    if resource_type == "snapshot":
        return client.update_snapshot(
            name_id_or_prefix=target, project=project, **kwargs
        )
    if resource_type == "run_template":
        return client.update_run_template(
            name_id_or_prefix=target, project=project, **kwargs
        )
    if resource_type == "artifact":
        if "name" in kwargs:
            kwargs["new_name"] = kwargs.pop("name")
        return client.update_artifact(
            name_id_or_prefix=target, project=project, **kwargs
        )
    if resource_type == "artifact_version":
        return client.update_artifact_version(
            name_id_or_prefix=target, project=project, **kwargs
        )
    if resource_type == "model":
        return client.update_model(model_name_or_id=target, project=project, **kwargs)
    if resource_type == "model_version":
        return client.update_model_version(
            model_name_or_id=_exact_uuid("model_id", model_id),
            version_name_or_id=target,
            project=project,
            **kwargs,
        )
    if resource_type == "tag":
        return client.update_tag(tag_name_or_id=target, **kwargs)
    if resource_type == "service_connector":
        if "expires_at" in kwargs:
            kwargs["expires_at"] = _parse_datetime(kwargs["expires_at"])
        result, _ = client.update_service_connector(
            name_id_or_prefix=target,
            **kwargs,
            verify=False,
            list_resources=False,
            update=True,
        )
        return result
    if resource_type == "code_repository":
        return client.update_code_repository(
            name_id_or_prefix=target, project=project, **kwargs
        )
    if resource_type == "webhook":
        return client.update_webhook(
            name_id_or_prefix=target, project=project, **kwargs
        )
    if resource_type == "schedule_trigger":
        from zenml.enums import TriggerRunConcurrency

        kwargs = _trigger_kwargs(kwargs)
        if "concurrency" in kwargs:
            kwargs["concurrency"] = TriggerRunConcurrency(kwargs["concurrency"])
        return client.update_schedule_trigger(
            trigger_name_id_or_prefix=target, **kwargs
        )
    if resource_type == "platform_event_trigger":
        from zenml.enums import SourceType, TriggerRunConcurrency

        if "source_type" in kwargs:
            kwargs["source_type"] = SourceType(kwargs["source_type"])
        if "source_id" in kwargs:
            kwargs["source_id"] = _exact_uuid("source_id", kwargs["source_id"])
        if "concurrency" in kwargs:
            kwargs["concurrency"] = TriggerRunConcurrency(kwargs["concurrency"])
        return client.update_platform_event_trigger(
            trigger_name_id_or_prefix=target, **kwargs
        )
    if resource_type == "webhook_trigger":
        from zenml.enums import TriggerRunConcurrency

        if "concurrency" in kwargs:
            kwargs["concurrency"] = TriggerRunConcurrency(kwargs["concurrency"])
        return client.update_webhook_trigger(trigger_name_id_or_prefix=target, **kwargs)
    raise ResourceDispatchError(f"Unsupported update for {resource_type!r}")


def _delete_call(
    client: Any,
    resource_type: str,
    target: uuid.UUID,
    payload: dict[str, Any],
    project: str | None,
    *,
    component_type: str | None,
) -> None:
    if resource_type == "project":
        if str(client.active_project.id) == str(target):
            raise ResourceDispatchError("The active project cannot be deleted")
        return client.delete_project(name_id_or_prefix=str(target))
    if resource_type == "stack":
        return client.delete_stack(name_id_or_prefix=target, recursive=False)
    if resource_type == "stack_component":
        from zenml.enums import StackComponentType

        return client.delete_stack_component(
            name_id_or_prefix=target, component_type=StackComponentType(component_type)
        )
    if resource_type == "flavor":
        return client.delete_flavor(name_id_or_prefix=str(target))
    if resource_type in {
        "service",
        "pipeline",
        "pipeline_run",
        "snapshot",
        "run_template",
        "artifact",
        "model",
        "code_repository",
        "webhook",
    }:
        keyword = {
            "service": "name_id_or_prefix",
            "pipeline": "name_id_or_prefix",
            "pipeline_run": "name_id_or_prefix",
            "snapshot": "name_id_or_prefix",
            "run_template": "name_id_or_prefix",
            "artifact": "name_id_or_prefix",
            "model": "model_name_or_id",
            "code_repository": "name_id_or_prefix",
            "webhook": "name_id_or_prefix",
        }[resource_type]
        return getattr(client, f"delete_{resource_type}")(
            **{keyword: target, "project": project}
        )
    if resource_type == "build":
        return client.delete_build(id_or_prefix=str(target), project=project)
    if resource_type == "deployment":
        return client.delete_deployment(
            name_id_or_prefix=target, project=project, **payload
        )
    if resource_type == "artifact_version":
        delete_metadata = payload.get("delete_metadata", True)
        delete_data = payload.get("delete_from_artifact_store", False)
        if not delete_metadata and not delete_data:
            raise ResourceDispatchError(
                "At least one artifact-version deletion option must be true"
            )
        return client.delete_artifact_version(
            name_id_or_prefix=target,
            delete_metadata=delete_metadata,
            delete_from_artifact_store=delete_data,
            project=project,
            server_side=delete_data,
        )
    if resource_type == "model_version":
        return client.delete_model_version(model_version_id=target)
    if resource_type == "tag":
        return client.delete_tag(tag_name_or_id=target)
    if resource_type == "service_connector":
        return client.delete_service_connector(name_id_or_prefix=target)
    if resource_type in _TRIGGER_TYPES:
        return client.delete_trigger(trigger_id=target, soft=True)
    if resource_type == "hook_invocation":
        return client.delete_hook_invocation(hook_invocation_id=target)
    raise ResourceDispatchError(f"Unsupported delete for {resource_type!r}")


def _is_pre_dispatch_connection_failure(error: BaseException) -> bool:
    """Recognize connection failures that happened before a request was sent."""
    pending = [error]
    seen: set[int] = set()
    pre_dispatch_names = {
        "ConnectTimeoutError",
        "NewConnectionError",
        "NameResolutionError",
        "ConnectionRefusedError",
    }
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, (requests.ConnectTimeout, ssl.SSLCertVerificationError)):
            return True
        if type(current).__name__ in pre_dispatch_names:
            return True
        for attribute in ("__cause__", "__context__", "reason"):
            try:
                nested = getattr(current, attribute, None)
            except Exception:
                continue
            if isinstance(nested, BaseException):
                pending.append(nested)
        pending.extend(item for item in current.args if isinstance(item, BaseException))
    return False


def _reconciliation(
    resource_type: str,
    operation: str,
    *,
    resource_id: str | None,
    project_id: str | None,
    payload: Mapping[str, Any],
    artifact_id: str | None = None,
    model_id: str | None = None,
    component_type: str | None = None,
) -> dict[str, Any]:
    if resource_type in _TRIGGER_TYPES and operation == "delete":
        return {
            "operation": "list",
            "resource_type": resource_type,
            "filters": {"id": resource_id, "is_archived": True},
            **({"project_id": project_id} if project_id else {}),
            "note": "Confirm that the trigger is archived.",
        }
    if resource_id:
        result = {
            "operation": "get",
            "resource_type": resource_type,
            "resource_id": resource_id,
            **({"project_id": project_id} if project_id else {}),
            **({"artifact_id": artifact_id} if artifact_id else {}),
            **({"model_id": model_id} if model_id else {}),
            **({"component_type": component_type} if component_type else {}),
        }
        if resource_type == "artifact_version" and operation == "delete":
            result["note"] = (
                "A remaining metadata record cannot prove whether stored data was deleted."
            )
        elif resource_type == "deployment" and operation == "delete":
            result["note"] = (
                "Metadata absence does not prove that external infrastructure was removed."
            )
        return result
    if (
        resource_type == "model_version"
        and operation == "create"
        and "name" not in payload
    ):
        return {
            "operation": None,
            "resource_type": resource_type,
            **({"project_id": project_id} if project_id else {}),
            "model_id": model_id,
            "new_version_id": None,
            "reconcilable": False,
            "note": (
                "The model version may have been created with an auto-generated name, "
                "but its ID was lost with the response. The model ID alone cannot "
                "identify the new version; do not retry automatically."
            ),
        }
    if resource_type == "flavor" and operation == "create":
        return {
            "operation": None,
            "resource_type": resource_type,
            "source": payload["source"],
            "component_type": payload["component_type"],
            "new_flavor_id": None,
            "reconcilable": False,
            "note": (
                "The flavor may have been created, but its generated ID and name were "
                "lost with the response. Source and component type do not uniquely "
                "identify it; do not retry automatically."
            ),
        }
    if resource_type == "service" and operation == "create":
        from zenml.services import ServiceConfig

        service_config = ServiceConfig(**payload["config"])
        filters = {"service_name": service_config.service_name}
    elif resource_type == "stack_component" and operation == "create":
        filters = {"name": payload["name"], "type": payload["component_type"]}
    else:
        filters = {"name": payload["name"]} if "name" in payload else {}
    if resource_type == "model_version" and model_id:
        filters["model_id"] = model_id
    return {
        "operation": "list",
        "resource_type": resource_type,
        "filters": filters,
        **({"project_id": project_id} if project_id else {}),
        "note": (
            "An auto-generated webhook secret cannot be recovered after an unknown create outcome."
            if resource_type == "webhook" and "secret" not in payload
            else "Confirm the mutation by reading the resource; do not repeat it automatically."
        ),
    }


def mutate_resource(
    client: Any,
    resource_type: str,
    operation: str,
    *,
    resource_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    project_id: str | None = None,
    artifact_id: str | None = None,
    model_id: str | None = None,
    component_type: str | None = None,
    read_only: bool | None = None,
) -> dict[str, Any]:
    """Validate and perform exactly one allowlisted ordinary mutation."""
    ensure_writes_enabled(read_only=read_only)
    spec = get_resource_spec(resource_type)
    try:
        spec.operation_spec(operation)
        mutation_payload = validate_mutation_payload(resource_type, operation, payload)
    except ResourceRegistryError as error:
        raise ResourceDispatchError(str(error)) from error
    if operation not in {"create", "update", "delete"}:
        raise ResourceDispatchError(f"Unsupported mutation operation {operation!r}")
    effective_scope, project = _mutation_scope(
        client, resource_type, project_id, operation=operation
    )
    target = None if operation == "create" else _exact_uuid("resource_id", resource_id)
    expected_parents = set(spec.mutations[operation].parent_fields)
    supplied_parents = {
        "artifact_id": artifact_id,
        "model_id": model_id,
        "component_type": component_type,
    }
    missing = sorted(field for field in expected_parents if not supplied_parents[field])
    unexpected = sorted(
        field
        for field, value in supplied_parents.items()
        if value is not None and field not in expected_parents
    )
    if missing:
        raise ResourceDispatchError(
            f"{resource_type!r} {operation} requires: {', '.join(missing)}"
        )
    if unexpected:
        raise ResourceDispatchError(
            f"Unexpected identifiers for {resource_type!r} {operation}: {', '.join(unexpected)}"
        )
    if "component_type" in expected_parents:
        from zenml.enums import StackComponentType

        try:
            StackComponentType(component_type)
        except ValueError as error:
            raise ResourceDispatchError("Invalid component_type") from error
    if resource_type in {"flavor", "code_repository"} and "source" in mutation_payload:
        _allowed_import_source(mutation_payload["source"])
    if operation == "create":
        _validate_create_relations(
            client, resource_type, mutation_payload, project, model_id
        )
    else:
        existing = _validate_related(
            client,
            resource_type,
            str(target),
            project_id=project,
            artifact_id=artifact_id,
            model_id=model_id,
            component_type=component_type,
        )
        if resource_type == "stack" and "component_updates" in mutation_payload:
            _validate_create_relations(
                client,
                "stack",
                {"components": mutation_payload["component_updates"]},
                None,
                None,
                require_core_stack_components=False,
            )
        if resource_type == "stack_component" and mutation_payload.get("connector_id"):
            _validate_related(
                client,
                "service_connector",
                mutation_payload["connector_id"],
                project_id=None,
            )
        if resource_type == "service" and mutation_payload.get("model_version_id"):
            _validate_model_version_project(
                client, mutation_payload["model_version_id"], project
            )
        if resource_type == "platform_event_trigger" and operation == "update":
            source_type = mutation_payload.get("source_type") or _find_nested_value(
                existing["item"], "source_type"
            )
            source_id = mutation_payload.get("source_id") or _find_nested_value(
                existing["item"], "source_id"
            )
            if source_type is None or source_id is None:
                raise ResourceDispatchError(
                    "Unable to validate the effective platform-event source"
                )
            source_resource = {
                "pipeline": "pipeline",
                "pipeline_run": "pipeline_run",
                "pipeline_snapshot": "snapshot",
            }.get(str(source_type))
            if source_resource is None:
                raise ResourceDispatchError("Invalid platform-event source_type")
            _validate_related(
                client,
                source_resource,
                str(_exact_uuid("source_id", str(source_id))),
                project_id=project,
            )
    reconciliation = _reconciliation(
        resource_type,
        operation,
        resource_id=str(target) if target else None,
        project_id=project,
        payload=mutation_payload,
        artifact_id=artifact_id,
        model_id=model_id,
        component_type=component_type,
    )
    try:
        if operation == "create":
            result = _invoke_adapter(
                lambda: _create_call(
                    client, resource_type, mutation_payload, project, model_id
                )
            )
        elif operation == "update":
            assert target is not None
            result = _invoke_adapter(
                lambda: _update_call(
                    client,
                    resource_type,
                    target,
                    mutation_payload,
                    project,
                    model_id=model_id,
                    component_type=component_type,
                )
            )
        else:
            assert target is not None
            result = _invoke_adapter(
                lambda: _delete_call(
                    client,
                    resource_type,
                    target,
                    mutation_payload,
                    project,
                    component_type=component_type,
                )
            )
    except (
        json.JSONDecodeError,
        requests.ReadTimeout,
        requests.ConnectionError,
        requests.exceptions.JSONDecodeError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    ) as error:
        if _is_pre_dispatch_connection_failure(error):
            raise
        unknown = {
            "resource_type": resource_type,
            "operation": operation,
            "outcome": "unknown",
            "effective_scope": effective_scope,
            "reconciliation": reconciliation,
        }
        if reconciliation.get("operation"):
            recovery_message = (
                "Use the supplied reconciliation read and do not repeat automatically."
            )
        else:
            recovery_message = (
                "The created resource cannot be identified from the request; "
                "do not retry automatically."
            )
        return {
            **unknown,
            "error": {
                "tool": f"zenml_{operation}_resource",
                "message": (
                    "The response was lost while executing the mutation or could not be "
                    "decoded; it may have been dispatched, so the outcome is unknown. "
                    + recovery_message
                ),
                "type": "UnknownOutcome",
                "details": unknown,
            },
        }
    projected = safe_project(result, resource_type=resource_type)
    result_id = _find_nested_id(result, "id", "id") if result is not None else None
    if operation == "create" and result_id and spec.get_fields is not None:
        reconciliation_component_type = component_type
        if resource_type == "stack_component":
            reconciliation_component_type = mutation_payload["component_type"]
        reconciliation = _reconciliation(
            resource_type,
            operation,
            resource_id=result_id,
            project_id=project,
            payload=mutation_payload,
            artifact_id=artifact_id,
            model_id=model_id,
            component_type=reconciliation_component_type,
        )
    response = {
        "resource_type": resource_type,
        "operation": operation,
        "outcome": "completed",
        "resource_id": result_id or (str(target) if target else None),
        "effective_scope": effective_scope,
        "reconciliation": reconciliation,
    }
    if result is not None:
        response["item"] = projected
    if resource_type == "webhook" and operation == "create":
        issued_secret = _find_nested_value(result, "secret")
        if issued_secret is not None:
            response["issued_secret"] = issued_secret
    if resource_type == "deployment" and operation == "delete":
        response["force_requested"] = mutation_payload.get("force", False)
    if resource_type in _TRIGGER_TYPES and operation == "delete":
        response["archived"] = True
    if resource_type == "artifact_version" and operation == "delete":
        response["delete_metadata"] = mutation_payload.get("delete_metadata", True)
        response["delete_from_artifact_store"] = mutation_payload.get(
            "delete_from_artifact_store", False
        )
    return response


def create_resource(client: Any, resource_type: str, **kwargs: Any) -> dict[str, Any]:
    return mutate_resource(client, resource_type, "create", **kwargs)


def update_resource(
    client: Any, resource_type: str, resource_id: str, **kwargs: Any
) -> dict[str, Any]:
    return mutate_resource(
        client, resource_type, "update", resource_id=resource_id, **kwargs
    )


def delete_resource(
    client: Any, resource_type: str, resource_id: str, **kwargs: Any
) -> dict[str, Any]:
    return mutate_resource(
        client, resource_type, "delete", resource_id=resource_id, **kwargs
    )


_ACTION_PROJECT_RESOURCES = frozenset(
    {
        "pipeline_run",
        "schedule_trigger",
        "platform_event_trigger",
        "webhook_trigger",
        "deployment",
        "run_wait_condition",
        "tag",
        "webhook",
    }
)
_TAG_TARGET_RESOURCES = MappingProxyType(
    {
        "artifact": "artifact",
        "artifact_version": "artifact_version",
        "model": "model",
        "model_version": "model_version",
        "pipeline": "pipeline",
        "pipeline_run": "pipeline_run",
        "run_template": "run_template",
        "pipeline_snapshot": "snapshot",
        "deployment": "deployment",
    }
)


def _action_scope(
    client: Any, resource_type: str, project_id: str | None
) -> tuple[dict[str, Any], str]:
    if resource_type not in _ACTION_PROJECT_RESOURCES:
        raise ResourceDispatchError(f"Unsupported action resource {resource_type!r}")
    if project_id is None:
        raise ResourceDispatchError(
            f"project_id is required for {resource_type!r} actions"
        )
    project = str(_exact_uuid("project_id", project_id))
    return {
        "kind": "project",
        "project_id": project,
        "source": "requested",
    }, project


def _validate_tag_parent_payload(action: str, payload: Mapping[str, Any]) -> None:
    target_type = payload["target_type"]
    expected_parent = {
        "artifact_version": "artifact_id",
        "model_version": "model_id",
    }.get(target_type)
    supplied_parents = {
        field for field in ("artifact_id", "model_id") if payload.get(field) is not None
    }
    if expected_parent and expected_parent not in supplied_parents:
        raise ResourceDispatchError(
            f"tag {action} for {target_type!r} requires {expected_parent}"
        )
    allowed_parents = {expected_parent} if expected_parent else set()
    unexpected_parents = supplied_parents - allowed_parents
    if unexpected_parents:
        raise ResourceDispatchError(
            "Unexpected tag target parent identifiers: "
            + ", ".join(sorted(unexpected_parents))
        )


def _validate_listed_action_target(
    client: Any, resource_type: str, resource_id: str, project_id: str
) -> dict[str, Any]:
    target = _exact_uuid("target_id", resource_id)
    if resource_type == "artifact_version":
        item = _invoke_adapter(
            lambda: client.get_artifact_version(
                name_id_or_prefix=target,
                project=project_id,
                hydrate=False,
            )
        )
    elif resource_type == "model_version":
        item = _invoke_adapter(
            lambda: client.get_model_version(
                model_version_name_or_number_or_id=target,
                project=project_id,
                hydrate=False,
            )
        )
    else:
        item = None
    if item is not None:
        observed_id = _find_nested_id(item, "id", "id")
        observed_project = _find_nested_id(item, "project_id", "project")
        if observed_id != resource_id or observed_project != project_id:
            raise ResourceNotFound(
                f"{resource_type!r} was not found in the effective project"
            )
        return {"item": safe_project(item, resource_type=resource_type)}

    listed = list_resources(
        client,
        resource_type,
        filters={"id": resource_id},
        project_id=project_id,
        page=1,
        size=2,
    )
    exact = [
        item
        for item in listed["items"]
        if _find_nested_id(item, "id", "id") == resource_id
    ]
    if len(exact) != 1:
        raise ResourceNotFound(
            f"{resource_type!r} was not found in the effective project"
        )
    return exact[0]


def _action_reconciliation(
    resource_type: str,
    action: str,
    resource_id: str,
    project_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if resource_type == "pipeline_run" and action == "replay":
        return {
            "operation": None,
            "resource_type": resource_type,
            "source_run_id": resource_id,
            "project_id": project_id,
            "new_run_id": None,
            "reconcilable": False,
            "note": (
                "The replay may have created a new run, but its ID was lost with "
                "the response. Reading the source run cannot confirm the replay "
                "outcome; do not replay automatically."
            ),
        }
    if (
        resource_type in _TRIGGER_TYPES
        and action in {"attach", "detach"}
        and payload.get("snapshot_id")
    ):
        return {
            "operation": "list",
            "resource_type": resource_type,
            "project_id": project_id,
            "filters": {"id": resource_id, "snapshot_id": payload["snapshot_id"]},
            "note": (
                "Confirm that the trigger attachment is present."
                if action == "attach"
                else "Confirm that the trigger attachment is absent."
            ),
        }
    if resource_type in _TRIGGER_TYPES and action == "clear_dispatch_error":
        reconciliation = {
            "operation": "get",
            "resource_type": resource_type,
            "resource_id": resource_id,
            "project_id": project_id,
            "hydrate": True,
            "note": "Inspect snapshot_dispatch_states and confirm the dispatch error fields are empty; do not repeat automatically.",
        }
        if payload.get("snapshot_id"):
            reconciliation["expected_snapshot_id"] = payload["snapshot_id"]
        return reconciliation
    if resource_type == "run_wait_condition":
        return {
            "operation": "list",
            "resource_type": resource_type,
            "project_id": project_id,
            "filters": {"id": resource_id},
            "note": "Inspect status and resolution; do not resolve again automatically.",
        }
    if resource_type == "tag":
        target_type = payload["target_type"]
        target_resource = _TAG_TARGET_RESOURCES[target_type]
        reconciliation: dict[str, Any] = {
            "operation": "get",
            "resource_type": target_resource,
            "resource_id": payload["target_id"],
            "project_id": project_id,
            "hydrate": True,
            "expected_tag_id": resource_id,
            "note": "Inspect the hydrated target tags for this exact tag ID; do not repeat automatically.",
        }
        if target_type == "artifact_version":
            reconciliation["artifact_id"] = payload["artifact_id"]
        elif target_type == "model_version":
            reconciliation["model_id"] = payload["model_id"]
        return reconciliation
    note = "Inspect the resource state; do not repeat the action automatically."
    if resource_type == "webhook" and action == "rotate_secret":
        note = "The newly issued webhook secret cannot be recovered by a later read."
    return {
        "operation": "get",
        "resource_type": resource_type,
        "resource_id": resource_id,
        "project_id": project_id,
        "note": note,
    }


def _run_action_call(
    client: Any,
    resource_type: str,
    action: str,
    target: uuid.UUID,
    payload: Mapping[str, Any],
    project: str,
) -> Any:
    if resource_type == "pipeline_run":
        from zenml.config.pipeline_run_configuration import ReplayRunConfiguration

        configuration = payload.get("run_configuration")
        run_configuration = (
            ReplayRunConfiguration.model_validate(configuration)
            if configuration is not None
            else None
        )
        return client.replay_pipeline_run(
            name_id_or_prefix=target,
            run_configuration=run_configuration,
            project=project,
            synchronous=False,
        )
    if resource_type in _TRIGGER_TYPES:
        snapshot_id = payload.get("snapshot_id")
        snapshot = (
            _exact_uuid("snapshot_id", snapshot_id) if snapshot_id is not None else None
        )
        if action == "attach":
            from zenml.config.pipeline_run_configuration import PipelineRunConfiguration

            configuration = payload.get("run_configuration")
            run_configuration = (
                PipelineRunConfiguration.model_validate(configuration)
                if configuration is not None
                else None
            )
            return client.attach_trigger_to_snapshot(
                trigger_id=target,
                pipeline_snapshot_id=snapshot,
                run_configuration=run_configuration,
                allow_replace=payload.get("allow_replace", False),
            )
        if action == "detach":
            return client.detach_trigger_from_snapshot(
                trigger_id=target, pipeline_snapshot_id=snapshot
            )
        return client.clear_trigger_dispatch_error(
            trigger_id=target, pipeline_snapshot_id=snapshot
        )
    if resource_type == "deployment":
        if action == "provision":
            snapshot_id = payload.get("snapshot_id")
            return client.provision_deployment(
                name_id_or_prefix=target,
                project=project,
                snapshot_id=(
                    _exact_uuid("snapshot_id", snapshot_id)
                    if snapshot_id is not None
                    else None
                ),
                timeout=payload.get("timeout", 300),
            )
        if action == "deprovision":
            return client.deprovision_deployment(
                name_id_or_prefix=target,
                project=project,
                timeout=payload.get("timeout", 300),
            )
        return client.refresh_deployment(name_id_or_prefix=target, project=project)
    if resource_type == "run_wait_condition":
        from zenml.enums import RunWaitConditionResolution

        return client.resolve_run_wait_condition(
            run_wait_condition_id=target,
            resolution=RunWaitConditionResolution(payload["resolution"].lower()),
            result=payload.get("result"),
        )
    if resource_type == "tag":
        from zenml.enums import TaggableResourceTypes
        from zenml.models import TagResourceRequest

        request = TagResourceRequest(
            tag_id=target,
            resource_id=_exact_uuid("target_id", payload["target_id"]),
            resource_type=TaggableResourceTypes(payload["target_type"]),
        )
        if action == "attach":
            return client.zen_store.batch_create_tag_resource(tag_resources=[request])
        return client.zen_store.batch_delete_tag_resource(tag_resources=[request])
    return client.rotate_webhook_secret(
        name_id_or_prefix=target,
        secret=payload.get("secret"),
        project=project,
    )


def action_resource(
    client: Any,
    resource_type: str,
    action: str,
    resource_id: str,
    *,
    payload: Mapping[str, Any] | None = None,
    project_id: str | None = None,
    read_only: bool | None = None,
) -> dict[str, Any]:
    """Validate and perform one finite lifecycle or relation action."""
    ensure_writes_enabled(read_only=read_only)
    try:
        get_resource_spec(resource_type)
        action_spec = get_action_spec(resource_type, action)
        action_payload = validate_action_payload(resource_type, action, payload)
    except ResourceRegistryError as error:
        raise ResourceDispatchError(str(error)) from error
    if resource_type == "tag":
        _validate_tag_parent_payload(action, action_payload)
    target = _exact_uuid("resource_id", resource_id)
    effective_scope, project = _action_scope(client, resource_type, project_id)

    if resource_type == "run_wait_condition":
        _validate_listed_action_target(client, resource_type, str(target), project)
    else:
        existing = _validate_related(
            client,
            resource_type,
            str(target),
            project_id=None if resource_type == "tag" else project,
        )
        if resource_type == "tag" and action == "attach":
            is_exclusive = bool(_find_nested_value(existing["item"], "exclusive"))
            if is_exclusive and not action_payload.get(
                "allow_exclusive_replace", False
            ):
                raise ResourceDispatchError(
                    "Attaching an exclusive tag requires allow_exclusive_replace=true"
                )

    if (
        resource_type in _TRIGGER_TYPES
        and action in {"attach", "detach", "clear_dispatch_error"}
        and action_payload.get("snapshot_id") is not None
    ):
        _validate_related(
            client,
            "snapshot",
            str(_exact_uuid("snapshot_id", action_payload["snapshot_id"])),
            project_id=project,
        )
    elif resource_type == "deployment" and action == "provision":
        snapshot_id = action_payload.get("snapshot_id")
        if snapshot_id is not None:
            _validate_related(
                client,
                "snapshot",
                str(_exact_uuid("snapshot_id", snapshot_id)),
                project_id=project,
            )
    elif resource_type == "tag":
        target_type = action_payload["target_type"]
        target_resource = _TAG_TARGET_RESOURCES[target_type]
        expected_parent = {
            "artifact_version": "artifact_id",
            "model_version": "model_id",
        }.get(target_type)
        if expected_parent:
            _validate_related(
                client,
                target_resource,
                action_payload["target_id"],
                project_id=project,
                artifact_id=action_payload.get("artifact_id"),
                model_id=action_payload.get("model_id"),
            )
        else:
            _validate_listed_action_target(
                client, target_resource, action_payload["target_id"], project
            )

    reconciliation = _action_reconciliation(
        resource_type, action, str(target), project, action_payload
    )
    from zenml.deployers.exceptions import DeploymentTimeoutError

    try:
        result = _invoke_adapter(
            lambda: _run_action_call(
                client, resource_type, action, target, action_payload, project
            )
        )
    except DeploymentTimeoutError:
        if resource_type != "deployment" or action not in {"provision", "deprovision"}:
            raise
        return {
            "resource_type": resource_type,
            "action": action,
            "resource_id": str(target),
            "outcome": "accepted",
            "status": "timed_out",
            "effective_scope": effective_scope,
            "reconciliation": reconciliation,
        }
    except (
        json.JSONDecodeError,
        requests.ReadTimeout,
        requests.ConnectionError,
        requests.exceptions.JSONDecodeError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    ) as error:
        if _is_pre_dispatch_connection_failure(error):
            raise
        unknown = {
            "resource_type": resource_type,
            "action": action,
            "resource_id": str(target),
            "outcome": "unknown",
            "effective_scope": effective_scope,
            "reconciliation": reconciliation,
        }
        return {
            **unknown,
            "error": {
                "tool": "zenml_action_resource",
                "message": (
                    "The response was lost while executing the action or could not be "
                    "decoded; it may have been dispatched, so the outcome is unknown. "
                    + (
                        "The created run ID is unavailable, so reading the source run "
                        "cannot reconcile the outcome; do not replay automatically."
                        if resource_type == "pipeline_run" and action == "replay"
                        else "Use the supplied reconciliation read and do not repeat automatically."
                    )
                ),
                "type": "UnknownOutcome",
                "details": unknown,
            },
        }

    response: dict[str, Any] = {
        "resource_type": resource_type,
        "action": action,
        "resource_id": str(target),
        "outcome": "accepted" if resource_type == "pipeline_run" else "completed",
        "effective_scope": effective_scope,
        "reconciliation": reconciliation,
        "sdk_method": action_spec.sdk_method,
    }
    if result is not None:
        response["item"] = safe_project(result, resource_type=resource_type)
        result_id = _find_nested_id(result, "id", "id")
        result_status = _find_nested_value(result, "status")
        if result_status is not None:
            response["status"] = result_status
        if resource_type == "pipeline_run" and result_id:
            response["new_run_id"] = result_id
            response["reconciliation"] = {
                "operation": "get",
                "resource_type": resource_type,
                "resource_id": result_id,
                "project_id": project,
                "note": (
                    "Inspect the replayed run status; do not replay automatically."
                ),
            }
    if resource_type == "webhook" and action == "rotate_secret":
        issued_secret = _find_nested_value(result, "secret")
        if issued_secret is not None:
            response["issued_secret"] = issued_secret
    if resource_type == "tag" and action == "attach":
        response["exclusive_replacement_authorized"] = action_payload.get(
            "allow_exclusive_replace", False
        )
    return response


assert set(LIST_ADAPTERS) == set(RESOURCE_REGISTRY)
assert set(GET_ADAPTERS) == {
    name for name, spec in RESOURCE_REGISTRY.items() if "get" in spec.operations
}
assert set(ACTION_REGISTRY) == {
    (spec.resource_type, spec.action) for spec in ACTION_REGISTRY.values()
}
