"""Static resource-operation catalog for the generic ZenML MCP tools.

This module intentionally depends only on the standard library.  The registry is
the authority for what the generic tools advertise and accept; SDK introspection
is used by tests only to detect drift.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping

Operation = Literal["list", "get"]
ScopeKind = Literal["global", "project"]

MAX_PAGE_SIZE = 200
DATETIME_FILTERS = frozenset(
    {
        "created",
        "updated",
        "start_time",
        "end_time",
        "cache_expires_at",
        "next_occurrence",
        "resolved_at",
        "run_once_start_time",
    }
)

BOOLEAN_FILTERS = frozenset(
    {
        "active",
        "cache_expired",
        "catchup",
        "contains_code",
        "deployable",
        "deployed",
        "email_opted_in",
        "exclude_retried",
        "exclusive",
        "has_custom_name",
        "hidden",
        "in_progress",
        "is_archived",
        "is_local",
        "named_only",
        "only_unused",
        "preemptible",
        "private",
        "root_runs_only",
        "runnable",
        "running",
        "templatable",
    }
)
INTEGER_FILTERS = frozenset(
    {"duration", "index", "number", "version", "version_number"}
)
NUMBER_FILTERS = frozenset({"interval_second"})
_SCALAR_STRING_FILTERS = frozenset(
    {
        ("artifact_version", "name"),
        ("pipeline", "latest_run_status"),
        ("pipeline", "latest_run_user"),
        ("service_connector", "resource_id"),
        ("service_connector", "resource_type"),
        ("service_connector_type", "auth_method"),
        ("service_connector_type", "connector_type"),
        ("service_connector_type", "resource_type"),
    }
)
_UUID_ONLY_FILTERS = frozenset({("snapshot", "trigger_id")})

_ENUM_FILTERS: Mapping[tuple[str, str], tuple[str, ...]] = MappingProxyType(
    {
        ("model_version", "stage"): (
            "none",
            "staging",
            "production",
            "archived",
            "latest",
        ),
        ("tag", "color"): (
            "grey",
            "purple",
            "red",
            "green",
            "yellow",
            "orange",
            "lime",
            "teal",
            "turquoise",
            "magenta",
            "blue",
        ),
        ("tag", "resource_type"): (
            "artifact",
            "artifact_version",
            "model",
            "model_version",
            "pipeline",
            "pipeline_run",
            "run_template",
            "pipeline_snapshot",
            "deployment",
        ),
        ("schedule_trigger", "concurrency"): ("skip", "submit"),
        ("platform_event_trigger", "concurrency"): ("skip", "submit"),
        ("webhook_trigger", "concurrency"): ("skip", "submit"),
    }
)

_COMMON = ("sort_by", "logical_operator", "id", "created", "updated")


class ResourceRegistryError(ValueError):
    """A resource type or operation is outside the static public catalog."""


def _scalar_or_array_schema(scalar: dict[str, Any]) -> dict[str, Any]:
    """Build the JSON shape used by ZenML scalar-or-list filter parameters."""
    return {
        "anyOf": [
            scalar,
            {"type": "array", "items": scalar, "minItems": 1},
        ]
    }


def filter_schema(resource_type: str, field: str) -> dict[str, Any]:
    """Return the public JSON schema for one allowlisted filter."""
    if field == "logical_operator":
        return {"type": "string", "enum": ["and", "or"]}
    if field == "sort_by":
        return {"type": "string", "minLength": 1}
    if field == "labels":
        return {
            "type": "object",
            "additionalProperties": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        }
    if (resource_type, field) in _UUID_ONLY_FILTERS:
        return {"type": "string", "format": "uuid"}
    if (resource_type, field) in _SCALAR_STRING_FILTERS:
        return {"type": "string", "minLength": 1}
    if field in BOOLEAN_FILTERS:
        return {"type": "boolean"}
    if field in INTEGER_FILTERS:
        return _scalar_or_array_schema(
            {"anyOf": [{"type": "integer"}, {"type": "string", "minLength": 1}]}
        )
    if field in NUMBER_FILTERS:
        return _scalar_or_array_schema(
            {
                "anyOf": [
                    {"type": "number"},
                    {"type": "string", "minLength": 1},
                ]
            }
        )
    enum_values = _ENUM_FILTERS.get((resource_type, field))
    if enum_values is not None:
        return _scalar_or_array_schema({"type": "string", "enum": list(enum_values)})
    return _scalar_or_array_schema({"type": "string", "minLength": 1})


def validate_filter_value(resource_type: str, field: str, value: Any) -> None:
    """Validate against the same static types exposed in discovery."""
    if field == "logical_operator":
        valid = isinstance(value, str) and value in {"and", "or"}
    elif field == "sort_by":
        valid = isinstance(value, str) and bool(value)
    elif field == "labels":
        valid = isinstance(value, dict) and all(
            isinstance(key, str) and (item is None or isinstance(item, str))
            for key, item in value.items()
        )
    elif (resource_type, field) in _UUID_ONLY_FILTERS:
        try:
            valid = isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
        except (AttributeError, ValueError):
            valid = False
    elif (resource_type, field) in _SCALAR_STRING_FILTERS:
        valid = isinstance(value, str) and bool(value)
    elif field in BOOLEAN_FILTERS:
        valid = isinstance(value, bool)
    else:
        values = value if isinstance(value, list) else [value]
        valid = bool(values)
        if field in INTEGER_FILTERS:
            valid = valid and all(
                (isinstance(item, int) and not isinstance(item, bool))
                or (isinstance(item, str) and bool(item))
                for item in values
            )
        elif field in NUMBER_FILTERS:
            valid = valid and all(
                (isinstance(item, (int, float)) and not isinstance(item, bool))
                or (isinstance(item, str) and bool(item))
                for item in values
            )
        else:
            enum_values = _ENUM_FILTERS.get((resource_type, field))
            valid = valid and all(
                isinstance(item, str)
                and bool(item)
                and (enum_values is None or item in enum_values)
                for item in values
            )
    if not valid:
        raise ResourceRegistryError(
            f"Invalid value for {resource_type!r} filter {field!r}"
        )


@dataclass(frozen=True)
class OperationSpec:
    """One public generic operation with a bounded input contract."""

    resource_type: str
    operation: Operation
    sdk_method: str
    fields: tuple[str, ...]
    required_fields: tuple[str, ...] = ()
    filter_fields: tuple[str, ...] = ()
    required_filter_fields: tuple[str, ...] = ()
    description: str = ""

    def schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            field: {"type": "string"}
            for field in self.fields
            if field not in {"page", "size"}
        }
        if "page" in self.fields:
            properties["page"] = {"type": "integer", "minimum": 1}
        if "size" in self.fields:
            properties["size"] = {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PAGE_SIZE,
            }
        if "filters" in self.fields:
            properties["filters"] = {
                "type": "object",
                "properties": {
                    field: filter_schema(self.resource_type, field)
                    for field in self.filter_fields
                },
                "required": list(self.required_filter_fields),
                "additionalProperties": False,
            }
        return {
            "type": "object",
            "properties": properties,
            "required": list(self.required_fields),
            "additionalProperties": False,
        }


@dataclass(frozen=True)
class ResourceSpec:
    """Static public contract for one ZenML resource type."""

    resource_type: str
    description: str
    scope: ScopeKind
    default_size: int
    list_filters: tuple[str, ...]
    list_required_filters: tuple[str, ...]
    get_fields: tuple[str, ...] | None
    get_required: tuple[str, ...] = ("resource_id",)
    non_paginated: bool = False

    @property
    def operations(self) -> tuple[Operation, ...]:
        return ("list", "get") if self.get_fields is not None else ("list",)

    def operation_spec(self, operation: str) -> OperationSpec:
        scope_fields = ("project_id",) if self.scope == "project" else ()
        if operation == "list":
            return OperationSpec(
                resource_type=self.resource_type,
                operation="list",
                sdk_method=LIST_SDK_METHODS[self.resource_type],
                fields=("page", "size", *scope_fields, "filters"),
                required_fields=("filters",) if self.list_required_filters else (),
                filter_fields=self.list_filters,
                required_filter_fields=self.list_required_filters,
                description=f"List {self.description.lower()} with bounded filters.",
            )
        if operation == "get" and self.get_fields is not None:
            return OperationSpec(
                resource_type=self.resource_type,
                operation="get",
                sdk_method=GET_SDK_METHODS[self.resource_type],
                fields=(*scope_fields, *self.get_fields),
                required_fields=self.get_required,
                description=f"Get one {self.description.lower()} by an identifier.",
            )
        raise ResourceRegistryError(
            f"Unsupported operation {operation!r} for {self.resource_type!r}; "
            f"supported operations: {', '.join(self.operations)}"
        )


def _filters(*fields: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*_COMMON, *fields)))


def _spec(
    resource_type: str,
    description: str,
    *,
    scope: ScopeKind = "project",
    default_size: int = 20,
    filters: tuple[str, ...] = _COMMON,
    list_required_filters: tuple[str, ...] = (),
    get_fields: tuple[str, ...] | None = ("resource_id",),
    get_required: tuple[str, ...] = ("resource_id",),
    non_paginated: bool = False,
) -> ResourceSpec:
    return ResourceSpec(
        resource_type=resource_type,
        description=description,
        scope=scope,
        default_size=default_size,
        list_filters=filters,
        list_required_filters=list_required_filters,
        get_fields=get_fields,
        get_required=get_required,
        non_paginated=non_paginated,
    )


_RESOURCE_SPECS = (
    _spec(
        "project",
        "Projects",
        scope="global",
        default_size=50,
        filters=_filters("name", "display_name"),
    ),
    _spec(
        "user",
        "Users",
        scope="global",
        default_size=50,
        filters=_filters(
            "external_user_id", "name", "full_name", "email", "active", "email_opted_in"
        ),
    ),
    _spec(
        "stack",
        "Stacks",
        scope="global",
        filters=_filters("name", "description", "component_id", "user", "component"),
    ),
    _spec(
        "stack_component",
        "Stack components",
        scope="global",
        filters=_filters("name", "flavor", "type", "connector_id", "stack_id", "user"),
        get_fields=("resource_id", "component_type"),
        get_required=("resource_id", "component_type"),
    ),
    _spec(
        "flavor",
        "Flavors",
        scope="global",
        filters=_filters("name", "display_name", "type", "integration", "user"),
    ),
    _spec(
        "service",
        "Services",
        filters=_filters(
            "type",
            "flavor",
            "user",
            "running",
            "service_name",
            "pipeline_name",
            "pipeline_run_id",
            "pipeline_step_name",
            "model_version_id",
        ),
    ),
    _spec(
        "pipeline",
        "Pipelines",
        filters=_filters(
            "name", "latest_run_status", "latest_run_user", "user", "tags"
        ),
    ),
    _spec(
        "pipeline_run",
        "Pipeline runs",
        default_size=10,
        filters=_filters(
            "name",
            "pipeline_id",
            "pipeline_name",
            "stack_id",
            "schedule_id",
            "build_id",
            "snapshot_id",
            "code_repository_id",
            "template_id",
            "source_snapshot_id",
            "model_version_id",
            "linked_to_model_version_id",
            "orchestrator_run_id",
            "status",
            "index",
            "start_time",
            "end_time",
            "templatable",
            "tags",
            "user",
            "run_metadata",
            "pipeline",
            "code_repository",
            "model",
            "stack",
            "stack_component",
            "in_progress",
            "triggered_by_step_run_id",
            "triggered_by_deployment_id",
            "trigger_id",
            "parent_run_id",
            "root_runs_only",
        ),
    ),
    _spec(
        "run_step",
        "Run steps",
        default_size=10,
        filters=_filters(
            "name",
            "cache_key",
            "cache_expires_at",
            "cache_expired",
            "code_hash",
            "status",
            "start_time",
            "end_time",
            "pipeline_run_id",
            "snapshot_id",
            "original_step_run_id",
            "user",
            "model_version_id",
            "model",
            "run_metadata",
            "exclude_retried",
            "version",
        ),
        list_required_filters=("pipeline_run_id",),
        get_fields=("resource_id", "pipeline_run_id"),
        get_required=("resource_id", "pipeline_run_id"),
    ),
    _spec(
        "snapshot",
        "Snapshots",
        filters=_filters(
            "user",
            "name",
            "named_only",
            "pipeline",
            "stack",
            "build_id",
            "schedule_id",
            "source_snapshot_id",
            "runnable",
            "deployable",
            "deployed",
            "tags",
            "trigger_id",
        ),
    ),
    _spec(
        "build",
        "Builds",
        filters=_filters(
            "user",
            "pipeline_id",
            "stack_id",
            "container_registry_id",
            "is_local",
            "contains_code",
            "zenml_version",
            "python_version",
            "checksum",
            "stack_checksum",
            "duration",
        ),
    ),
    _spec(
        "run_template",
        "Run templates",
        filters=_filters(
            "name",
            "hidden",
            "pipeline_id",
            "build_id",
            "stack_id",
            "code_repository_id",
            "user",
            "pipeline",
            "stack",
        ),
    ),
    _spec(
        "deployment",
        "Deployments",
        filters=_filters(
            "name",
            "snapshot_id",
            "deployer_id",
            "status",
            "url",
            "user",
            "pipeline",
            "tags",
        ),
    ),
    _spec(
        "schedule",
        "Schedules",
        filters=_filters(
            "name",
            "user",
            "pipeline_id",
            "orchestrator_id",
            "active",
            "cron_expression",
            "start_time",
            "end_time",
            "interval_second",
            "catchup",
            "run_once_start_time",
            "is_archived",
        ),
    ),
    _spec(
        "artifact",
        "Artifacts",
        default_size=10,
        filters=_filters("name", "has_custom_name", "user", "tags"),
    ),
    _spec(
        "artifact_version",
        "Artifact versions",
        default_size=10,
        filters=_filters(
            "artifact_id",
            "name",
            "version",
            "version_number",
            "artifact_store_id",
            "type",
            "data_type",
            "uri",
            "materializer",
            "model_version_id",
            "only_unused",
            "has_custom_name",
            "user",
            "model",
            "pipeline_run",
            "run_metadata",
            "tags",
        ),
        list_required_filters=("artifact_id",),
        get_fields=("resource_id", "artifact_id"),
        get_required=("resource_id", "artifact_id"),
    ),
    _spec("model", "Models", filters=_filters("name", "user", "tags")),
    _spec(
        "model_version",
        "Model versions",
        filters=_filters(
            "model_id", "name", "number", "stage", "run_metadata", "user", "tags"
        ),
        list_required_filters=("model_id",),
        get_fields=("resource_id", "model_id"),
        get_required=("resource_id", "model_id"),
    ),
    _spec(
        "tag",
        "Tags",
        scope="global",
        default_size=50,
        filters=_filters("user", "name", "color", "exclusive", "resource_type"),
    ),
    _spec(
        "secret",
        "Secret metadata",
        scope="global",
        default_size=50,
        filters=_filters("name", "private", "user"),
        get_fields=None,
    ),
    _spec(
        "service_connector",
        "Service connectors",
        scope="global",
        filters=_filters(
            "name",
            "connector_type",
            "auth_method",
            "resource_type",
            "resource_id",
            "user",
            "labels",
        ),
    ),
    _spec(
        "service_connector_type",
        "Service connector types",
        scope="global",
        filters=("connector_type", "resource_type", "auth_method"),
        non_paginated=True,
    ),
    _spec("code_repository", "Code repositories", filters=_filters("name", "user")),
    _spec(
        "webhook",
        "Webhooks",
        filters=_filters("name", "user", "webhook_type", "active"),
    ),
    _spec(
        "schedule_trigger",
        "Schedule triggers",
        filters=_filters(
            "user",
            "name",
            "active",
            "concurrency",
            "is_archived",
            "next_occurrence",
            "pipeline_id",
            "snapshot_id",
        ),
    ),
    _spec(
        "platform_event_trigger",
        "Platform event triggers",
        filters=_filters(
            "user",
            "name",
            "active",
            "concurrency",
            "is_archived",
            "pipeline_id",
            "snapshot_id",
        ),
    ),
    _spec(
        "webhook_trigger",
        "Webhook triggers",
        filters=_filters(
            "user",
            "name",
            "active",
            "concurrency",
            "is_archived",
            "webhook_id",
            "pipeline_id",
            "snapshot_id",
        ),
    ),
    _spec(
        "resource_request",
        "Resource requests",
        scope="global",
        filters=(
            "sort_by",
            "logical_operator",
            "id",
            "created",
            "updated",
            "scope_user",
            "user",
            "preemptible",
            "component_id",
            "step_run_id",
            "preemption_initiated_by_id",
            "status",
            "pipeline_run_id",
        ),
    ),
    _spec(
        "run_wait_condition",
        "Run wait conditions",
        default_size=10,
        filters=_filters(
            "name",
            "resolved_by",
            "resolved_at",
            "resolution",
            "pipeline_run",
            "user",
            "run_metadata",
            "status",
            "type",
        ),
        get_fields=None,
    ),
    _spec(
        "hook_invocation",
        "Hook invocations",
        default_size=10,
        filters=_filters(
            "pipeline_run_id",
            "step_run_id",
            "hook_type",
            "name",
            "status",
            "start_time",
            "end_time",
            "user",
        ),
    ),
)

RESOURCE_REGISTRY: Mapping[str, ResourceSpec] = MappingProxyType(
    {spec.resource_type: spec for spec in _RESOURCE_SPECS}
)

LIST_SDK_METHODS: Mapping[str, str] = MappingProxyType(
    {
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
)

GET_SDK_METHODS: Mapping[str, str] = MappingProxyType(
    {
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
)


def get_resource_spec(resource_type: str) -> ResourceSpec:
    """Return the exact resource spec; aliases are deliberately unsupported."""
    try:
        return RESOURCE_REGISTRY[resource_type]
    except KeyError as error:
        allowed = ", ".join(RESOURCE_REGISTRY)
        raise ResourceRegistryError(
            f"Unknown resource type {resource_type!r}; allowed resource types: {allowed}"
        ) from error


def describe_resources(
    resource_type: str | None = None, operation: str | None = None
) -> dict[str, Any]:
    """Return the bounded catalog or one operation schema."""
    if resource_type is None:
        if operation is not None:
            raise ResourceRegistryError("operation requires resource_type")
        return {
            "resources": [
                {
                    "resource_type": spec.resource_type,
                    "operations": list(spec.operations),
                    "scope": spec.scope,
                    "policy": "read_only" if spec.operations == ("list",) else "read",
                    "description": spec.description,
                }
                for spec in RESOURCE_REGISTRY.values()
            ]
        }

    spec = get_resource_spec(resource_type)
    if operation is None:
        return {
            "resource_type": spec.resource_type,
            "operations": list(spec.operations),
            "scope": spec.scope,
            "policy": "read_only" if spec.operations == ("list",) else "read",
            "description": spec.description,
        }

    operation_spec = spec.operation_spec(operation)
    example: dict[str, Any] = {"resource_type": resource_type}
    if operation == "list":
        example.update(
            {
                "page": 1,
                "size": spec.default_size,
                "filters": {
                    field: f"<{field}>" for field in spec.list_required_filters
                },
            }
        )
    else:
        example.update(
            {field: f"<{field}>" for field in operation_spec.required_fields}
        )
    return {
        "resource_type": resource_type,
        "operation": operation,
        "scope": spec.scope,
        "description": operation_spec.description,
        "input_schema": operation_spec.schema(),
        "example": example,
        "output_projection": (
            "Credential keys and opaque configuration, environment, parameter, "
            "settings, secret, and value containers are omitted recursively."
        ),
    }
