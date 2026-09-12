"""Validated dispatch for the generic ZenML resource read tools."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from zenml_resource_registry import (
    DATETIME_FILTERS,
    MAX_PAGE_SIZE,
    RESOURCE_REGISTRY,
    ResourceRegistryError,
    ResourceSpec,
    get_resource_spec,
    validate_filter_value,
)


class ResourceDispatchError(ValueError):
    """Base class for locally detectable generic-resource errors."""


class ResourceFeatureUnavailable(ResourceDispatchError):
    """The server supports the resource, but the backing feature is disabled."""


class ResourcePermissionDenied(ResourceDispatchError):
    """The authenticated principal cannot read the requested resource."""


class ResourceNotFound(ResourceDispatchError):
    """An exact resource or required parent does not exist."""


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


# Every callable below names a released ZenML 0.96.4 method in source.  The
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
) -> dict[str, Any]:
    """Validate and run one allowlisted get adapter."""
    spec = get_resource_spec(resource_type)
    spec.operation_spec("get")
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise ResourceDispatchError("resource_id must be a non-empty identifier")
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
    effective_scope, sdk_project = _effective_scope(client, spec, project_id)
    kwargs: dict[str, Any] = {}
    if sdk_project is not None and resource_type not in {"run_step", "hook_invocation"}:
        kwargs["project"] = sdk_project
    if resource_type == "stack_component":
        kwargs["component_type"] = component_type
    elif resource_type == "model_version":
        kwargs["model_name_or_id"] = model_id
    kwargs["hydrate"] = resource_type == "run_step"
    if resource_type == "service_connector":
        kwargs["expand_secrets"] = False
    if resource_type in {"service_connector_type", "resource_request"}:
        kwargs.pop("hydrate", None)
    item = _invoke_adapter(
        lambda: GET_ADAPTERS[resource_type](client, resource_id, kwargs)
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


assert set(LIST_ADAPTERS) == set(RESOURCE_REGISTRY)
assert set(GET_ADAPTERS) == {
    name for name, spec in RESOURCE_REGISTRY.items() if "get" in spec.operations
}
