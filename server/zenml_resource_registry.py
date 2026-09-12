"""Static resource-operation catalog for the generic ZenML MCP tools.

This module intentionally depends only on the standard library.  The registry is
the authority for what the generic tools advertise and accept; SDK introspection
is used by tests only to detect drift.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping, cast

from zenml_tool_catalog import configured_write_policy

Operation = Literal["list", "get", "create", "update", "delete"]
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


def _matches_schema(value: Any, schema: Mapping[str, Any]) -> bool:
    if "const" in schema and value != schema["const"]:
        return False
    alternatives = schema.get("anyOf")
    if alternatives and not any(
        _matches_schema(value, alternative) for alternative in alternatives
    ):
        return False
    alternatives = schema.get("oneOf")
    if (
        alternatives
        and sum(_matches_schema(value, alternative) for alternative in alternatives)
        != 1
    ):
        return False
    excluded = schema.get("not")
    if excluded and _matches_schema(value, excluded):
        return False
    requirements = schema.get("allOf")
    if requirements and not all(
        _matches_schema(value, requirement) for requirement in requirements
    ):
        return False
    expected = schema.get("type")
    if expected == "null":
        return value is None
    if expected == "string":
        if not isinstance(value, str):
            return False
        if len(value) < schema.get("minLength", 0):
            return False
        if "enum" in schema and value not in schema["enum"]:
            return False
        if schema.get("format") == "uuid":
            try:
                return str(uuid.UUID(value)) == value.lower()
            except ValueError:
                return False
        if schema.get("format") == "date-time":
            try:
                datetime_value = value.replace("Z", "+00:00")
                from datetime import datetime

                datetime.fromisoformat(datetime_value)
            except ValueError:
                return False
        return True
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= schema.get("minimum", value)
            and value <= schema.get("maximum", value)
        )
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= schema.get("minimum", value)
            and value <= schema.get("maximum", value)
        )
    if expected == "array":
        return (
            isinstance(value, list)
            and len(value) >= schema.get("minItems", 0)
            and all(_matches_schema(item, schema.get("items", {})) for item in value)
        )
    if expected == "object":
        if not isinstance(value, dict):
            return False
        if len(value) < schema.get("minProperties", 0):
            return False
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        if not set(schema.get("required", ())) <= set(value):
            return False
        additional = schema.get("additionalProperties")
        return all(
            _matches_schema(
                item,
                properties.get(key, additional if isinstance(additional, dict) else {}),
            )
            for key, item in value.items()
        )
    return True


def validate_mutation_payload(
    resource_type: str, operation: str, payload: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Validate a mutation payload against its advertised operation schema."""
    resource = get_resource_spec(resource_type)
    resource.operation_spec(operation)
    mutation = resource.mutations[operation]
    value = dict(payload or {})
    if mutation.payload_required and payload is None:
        raise ResourceRegistryError(f"{resource_type!r} {operation} requires payload")
    unsupported = sorted(set(value) - set(mutation.payload_properties))
    if unsupported:
        allowed = ", ".join(mutation.payload_properties) or "none"
        raise ResourceRegistryError(
            f"Unsupported fields for {resource_type!r} {operation}: "
            f"{', '.join(unsupported)}. Allowed fields: {allowed}"
        )
    missing = sorted(set(mutation.required_payload) - set(value))
    if missing:
        raise ResourceRegistryError(
            f"{resource_type!r} {operation} requires fields: {', '.join(missing)}"
        )
    for field, item in value.items():
        if not _matches_schema(item, mutation.payload_properties[field]):
            raise ResourceRegistryError(
                f"Invalid value for {resource_type!r} {operation} field {field!r}"
            )
    if not _matches_schema(value, mutation.payload_schema(resource_type)):
        raise ResourceRegistryError(
            f"Invalid payload for {resource_type!r} {operation}"
        )
    if operation == "update":
        empty_collection_noops = _EMPTY_COLLECTION_UPDATE_NOOPS.get(resource_type, ())
        false_noops = _FALSE_UPDATE_NOOPS.get(resource_type, ())
        effective_fields = {
            field
            for field, item in value.items()
            if not (field in empty_collection_noops and not item)
            and not (field in false_noops and item is False)
        }
        if not effective_fields:
            raise ResourceRegistryError(
                f"{resource_type!r} update requires at least one effective field"
            )
    return value


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
    property_schemas: Mapping[str, dict[str, Any]] | None = None

    def schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "resource_type": {"type": "string", "const": self.resource_type},
            **{
                field: {"type": "string"}
                for field in self.fields
                if field not in {"page", "size"}
            },
        }
        if self.property_schemas:
            properties.update(self.property_schemas)
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
            "required": ["resource_type", *self.required_fields],
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
    mutations: Mapping[str, "MutationSpec"] = MappingProxyType({})

    @property
    def operations(self) -> tuple[Operation, ...]:
        reads: tuple[Operation, ...] = (
            ("list", "get") if self.get_fields is not None else ("list",)
        )
        return (*reads, *(cast(Operation, operation) for operation in self.mutations))

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
                fields=(*scope_fields, *self.get_fields, "hydrate"),
                required_fields=self.get_required,
                description=f"Get one {self.description.lower()} by an identifier.",
                property_schemas=MappingProxyType({"hydrate": {"type": "boolean"}}),
            )
        mutation = self.mutations.get(operation)
        if mutation is not None:
            return mutation.operation_spec(self)
        raise ResourceRegistryError(
            f"Unsupported operation {operation!r} for {self.resource_type!r}; "
            f"supported operations: {', '.join(self.operations)}"
        )


@dataclass(frozen=True)
class MutationSpec:
    """Static contract for one ordinary create, update, or delete operation."""

    operation: Literal["create", "update", "delete"]
    sdk_method: str
    payload_properties: Mapping[str, dict[str, Any]] = MappingProxyType({})
    required_payload: tuple[str, ...] = ()
    parent_fields: tuple[str, ...] = ()
    payload_required: bool = False
    description: str = ""
    payload_constraints: Mapping[str, Any] = MappingProxyType({})
    example_payload: Mapping[str, Any] | None = None

    def payload_schema(self, resource_type: str) -> dict[str, Any]:
        properties = deepcopy(dict(self.payload_properties))
        if self.operation == "update":
            for field in _EMPTY_COLLECTION_UPDATE_NOOPS.get(resource_type, ()):
                schema = properties[field]
                if schema.get("type") == "array":
                    schema["minItems"] = 1
                elif schema.get("type") == "object":
                    schema["minProperties"] = 1

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": list(self.required_payload),
            "additionalProperties": False,
            **deepcopy(dict(self.payload_constraints)),
        }
        if self.operation == "update":
            schema["minProperties"] = 1
            false_noops = _FALSE_UPDATE_NOOPS.get(resource_type, ())
            if false_noops:
                schema["anyOf"] = [
                    {
                        "type": "object",
                        "properties": {field: {"const": True}},
                        "required": [field],
                    }
                    if field in false_noops
                    else {"type": "object", "required": [field]}
                    for field in properties
                ]
        return schema

    def operation_spec(self, resource: ResourceSpec) -> OperationSpec:
        fields: list[str] = []
        required: list[str] = []
        properties: dict[str, dict[str, Any]] = {}
        if self.operation != "create":
            fields.append("resource_id")
            required.append("resource_id")
            properties["resource_id"] = {"type": "string", "format": "uuid"}
        if (
            resource.scope == "project"
            or self.operation == "create"
            and resource.resource_type in _PROJECT_CREATE_RESOURCES
        ):
            fields.append("project_id")
            required.append("project_id")
            properties["project_id"] = {"type": "string", "format": "uuid"}
        for field in self.parent_fields:
            fields.append(field)
            required.append(field)
            properties[field] = (
                _COMPONENT_TYPE
                if field == "component_type"
                else {"type": "string", "format": "uuid"}
            )
        if self.payload_properties or self.payload_required:
            fields.append("payload")
            if self.payload_required:
                required.append("payload")
            properties["payload"] = self.payload_schema(resource.resource_type)
        return OperationSpec(
            resource_type=resource.resource_type,
            operation=self.operation,
            sdk_method=self.sdk_method,
            fields=tuple(fields),
            required_fields=tuple(required),
            description=self.description
            or f"{self.operation.title()} {resource.description.lower()}.",
            property_schemas=MappingProxyType(properties),
        )


@dataclass(frozen=True)
class ActionSpec:
    """Static contract for one explicitly supported resource action."""

    resource_type: str
    action: str
    sdk_method: str
    payload_properties: Mapping[str, dict[str, Any]] = MappingProxyType({})
    required_payload: tuple[str, ...] = ()
    prerequisites: tuple[str, ...] = ()

    def schema(self, *, project_scoped: bool = True) -> dict[str, Any]:
        payload_schema: dict[str, Any] = {
            "type": "object",
            "properties": dict(self.payload_properties),
            "required": list(self.required_payload),
            "additionalProperties": False,
        }
        if self.resource_type == "tag":
            payload_schema["oneOf"] = [
                {
                    "properties": {"target_type": {"const": "artifact_version"}},
                    "required": ["target_type", "artifact_id"],
                    "not": {"required": ["model_id"]},
                },
                {
                    "properties": {"target_type": {"const": "model_version"}},
                    "required": ["target_type", "model_id"],
                    "not": {"required": ["artifact_id"]},
                },
                {
                    "properties": {
                        "target_type": {
                            "enum": [
                                value
                                for value in _TAGGABLE_RESOURCE["enum"]
                                if value not in {"artifact_version", "model_version"}
                            ]
                        }
                    },
                    "required": ["target_type"],
                    "not": {
                        "anyOf": [
                            {"required": ["artifact_id"]},
                            {"required": ["model_id"]},
                        ]
                    },
                },
            ]
        properties: dict[str, Any] = {
            "resource_type": {"type": "string", "const": self.resource_type},
            "action": {"type": "string", "const": self.action},
            "resource_id": {"type": "string", "format": "uuid"},
            "payload": payload_schema,
        }
        required = ["resource_type", "action", "resource_id"]
        if self.required_payload:
            required.append("payload")
        if project_scoped:
            properties["project_id"] = {"type": "string", "format": "uuid"}
            required.append("project_id")
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }


_STR = {"type": "string"}
_NONEMPTY = {"type": "string", "minLength": 1}
_UUID = {"type": "string", "format": "uuid"}
_BOOL = {"type": "boolean"}
_POSITIVE_INT = {"type": "integer", "minimum": 1}
_ACTION_TIMEOUT = {"type": "integer", "minimum": 1, "maximum": 300}
_NONNEGATIVE_INT = {"type": "integer", "minimum": 0}
_STRING_LIST = {"type": "array", "items": _NONEMPTY}
_STRING_MAP = {"type": "object", "additionalProperties": {"type": "string"}}
_CONFIG_MAP = {"type": "object", "additionalProperties": True}
_NULLABLE_STRING_MAP = {
    "type": "object",
    "additionalProperties": {"anyOf": [{"type": "string"}, {"type": "null"}]},
}
_ANY_MAP = {"type": "object"}
_COMPONENT_TYPE = {
    "type": "string",
    "enum": [
        "alerter",
        "annotator",
        "artifact_store",
        "container_registry",
        "data_validator",
        "deployer",
        "experiment_tracker",
        "feature_store",
        "image_builder",
        "log_store",
        "model_deployer",
        "model_registry",
        "orchestrator",
        "sandbox",
        "step_operator",
    ],
}
_COLOR = {"type": "string", "enum": list(_ENUM_FILTERS[("tag", "color")])}
_CONCURRENCY = {"type": "string", "enum": ["skip", "submit"]}
_MODEL_STAGE = {
    "type": "string",
    "enum": ["none", "staging", "production", "archived"],
}
_SOURCE_TYPE = {
    "type": "string",
    "enum": ["pipeline", "pipeline_run", "pipeline_snapshot"],
}
_DATETIME = {"type": "string", "format": "date-time"}
_EMPTY_COLLECTION_UPDATE_NOOPS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "artifact": frozenset({"add_tags", "remove_tags"}),
        "artifact_version": frozenset({"add_tags", "remove_tags"}),
        "code_repository": frozenset({"config"}),
        "model": frozenset({"add_tags", "remove_tags"}),
        "model_version": frozenset({"add_tags", "remove_tags"}),
        "run_template": frozenset({"add_tags", "remove_tags"}),
        "service": frozenset({"endpoint", "labels", "status"}),
        "service_connector": frozenset({"labels"}),
        "snapshot": frozenset({"add_tags", "remove_tags"}),
        "stack": frozenset({"component_updates"}),
        "stack_component": frozenset({"configuration"}),
    }
)
_FALSE_UPDATE_NOOPS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "model_version": frozenset({"force"}),
        "snapshot": frozenset({"replace"}),
        "stack_component": frozenset({"disconnect"}),
    }
)


def _mutation(
    operation: Literal["create", "update", "delete"],
    sdk_method: str,
    properties: Mapping[str, dict[str, Any]] | None = None,
    *,
    required: tuple[str, ...] = (),
    parents: tuple[str, ...] = (),
    payload_required: bool | None = None,
    constraints: Mapping[str, Any] | None = None,
    example: Mapping[str, Any] | None = None,
) -> MutationSpec:
    return MutationSpec(
        operation=operation,
        sdk_method=sdk_method,
        payload_properties=MappingProxyType(dict(properties or {})),
        required_payload=required,
        parent_fields=parents,
        payload_required=bool(properties)
        if payload_required is None
        else payload_required,
        payload_constraints=MappingProxyType(dict(constraints or {})),
        example_payload=MappingProxyType(dict(example))
        if example is not None
        else None,
    )


_CARD_FIELDS = {
    "name": _NONEMPTY,
    "license": _STR,
    "description": _STR,
    "audience": _STR,
    "use_cases": _STR,
    "limitations": _STR,
    "trade_offs": _STR,
    "ethics": _STR,
}
_TAG_UPDATES = {"add_tags": _STRING_LIST, "remove_tags": _STRING_LIST}
_TRIGGER_COMMON = {
    "name": _NONEMPTY,
    "active": _BOOL,
    "concurrency": _CONCURRENCY,
}

_MUTATION_SPECS: Mapping[str, Mapping[str, MutationSpec]] = MappingProxyType(
    {
        "project": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_project",
                    {"name": _NONEMPTY, "description": _STR},
                    required=("name", "description"),
                ),
                "update": _mutation(
                    "update",
                    "update_project",
                    {"name": _NONEMPTY, "description": _NONEMPTY},
                ),
                "delete": _mutation("delete", "delete_project", payload_required=False),
            }
        ),
        "stack": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_stack",
                    {
                        "name": _NONEMPTY,
                        "components": {
                            "type": "object",
                            "properties": {
                                component_type: {
                                    "anyOf": [
                                        _UUID,
                                        {
                                            "type": "array",
                                            "items": _UUID,
                                            "minItems": 1,
                                        },
                                    ]
                                }
                                for component_type in _COMPONENT_TYPE["enum"]
                            },
                            "required": ["orchestrator", "artifact_store"],
                            "additionalProperties": False,
                        },
                    },
                    required=("name", "components"),
                ),
                "update": _mutation(
                    "update",
                    "update_stack",
                    {
                        "name": _NONEMPTY,
                        "description": _NONEMPTY,
                        "component_updates": {
                            "type": "object",
                            "additionalProperties": {
                                "anyOf": [
                                    _UUID,
                                    {"type": "array", "items": _UUID, "minItems": 1},
                                ]
                            },
                        },
                    },
                ),
                "delete": _mutation("delete", "delete_stack", payload_required=False),
            }
        ),
        "stack_component": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_stack_component",
                    {
                        "name": _NONEMPTY,
                        "flavor": _NONEMPTY,
                        "component_type": _COMPONENT_TYPE,
                        "configuration": _CONFIG_MAP,
                    },
                    required=("name", "flavor", "component_type", "configuration"),
                ),
                "update": _mutation(
                    "update",
                    "update_stack_component",
                    {
                        "name": _NONEMPTY,
                        "configuration": _CONFIG_MAP,
                        "disconnect": _BOOL,
                        "connector_id": _UUID,
                        "connector_resource_id": _STR,
                    },
                    parents=("component_type",),
                    constraints={
                        "allOf": [
                            {
                                "not": {
                                    "allOf": [
                                        {
                                            "type": "object",
                                            "required": ["connector_resource_id"],
                                        },
                                        {
                                            "not": {
                                                "type": "object",
                                                "required": ["connector_id"],
                                            }
                                        },
                                    ]
                                }
                            },
                            {
                                "not": {
                                    "type": "object",
                                    "properties": {"disconnect": {"const": True}},
                                    "required": ["disconnect", "connector_id"],
                                }
                            },
                            {
                                "not": {
                                    "type": "object",
                                    "properties": {"disconnect": {"const": True}},
                                    "required": [
                                        "disconnect",
                                        "connector_resource_id",
                                    ],
                                }
                            },
                        ]
                    },
                ),
                "delete": _mutation(
                    "delete",
                    "delete_stack_component",
                    parents=("component_type",),
                    payload_required=False,
                ),
            }
        ),
        "flavor": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_flavor",
                    {"source": _NONEMPTY, "component_type": _COMPONENT_TYPE},
                    required=("source", "component_type"),
                ),
                "delete": _mutation("delete", "delete_flavor", payload_required=False),
            }
        ),
        "service": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_service",
                    {
                        "config": {
                            "type": "object",
                            "properties": {
                                field: _NONEMPTY
                                if field in {"name", "model_name", "service_name"}
                                else _STR
                                for field in (
                                    "name",
                                    "description",
                                    "pipeline_name",
                                    "pipeline_step_name",
                                    "model_name",
                                    "model_version",
                                    "service_name",
                                )
                            },
                            "anyOf": [
                                {"type": "object", "required": ["name"]},
                                {"type": "object", "required": ["model_name"]},
                            ],
                            "additionalProperties": False,
                        },
                        "service_type": {
                            "type": "object",
                            "properties": {
                                "type": _NONEMPTY,
                                "flavor": _NONEMPTY,
                                "name": _STR,
                                "description": _STR,
                                "logo_url": _STR,
                            },
                            "required": ["type", "flavor"],
                            "additionalProperties": False,
                        },
                        "model_version_id": _UUID,
                    },
                    required=("config", "service_type"),
                    example={
                        "config": {"name": "<name>"},
                        "service_type": {"type": "<type>", "flavor": "<flavor>"},
                    },
                ),
                "update": _mutation(
                    "update",
                    "update_service",
                    {
                        "name": _NONEMPTY,
                        "admin_state": {
                            "type": "string",
                            "enum": [
                                "inactive",
                                "active",
                                "pending_startup",
                                "pending_shutdown",
                                "error",
                                "scaled_to_zero",
                            ],
                        },
                        "status": _ANY_MAP,
                        "endpoint": _ANY_MAP,
                        "labels": _STRING_MAP,
                        "prediction_url": _NONEMPTY,
                        "health_check_url": _NONEMPTY,
                        "model_version_id": _UUID,
                    },
                ),
                "delete": _mutation("delete", "delete_service", payload_required=False),
            }
        ),
        "pipeline": MappingProxyType(
            {"delete": _mutation("delete", "delete_pipeline", payload_required=False)}
        ),
        "pipeline_run": MappingProxyType(
            {
                "delete": _mutation(
                    "delete", "delete_pipeline_run", payload_required=False
                )
            }
        ),
        "snapshot": MappingProxyType(
            {
                "update": _mutation(
                    "update",
                    "update_snapshot",
                    {
                        "name": _NONEMPTY,
                        "description": _STR,
                        "replace": _BOOL,
                        **_TAG_UPDATES,
                    },
                ),
                "delete": _mutation(
                    "delete", "delete_snapshot", payload_required=False
                ),
            }
        ),
        "build": MappingProxyType(
            {"delete": _mutation("delete", "delete_build", payload_required=False)}
        ),
        "run_template": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_run_template",
                    {
                        "name": _NONEMPTY,
                        "snapshot_id": _UUID,
                        "description": _STR,
                        "tags": _STRING_LIST,
                    },
                    required=("name", "snapshot_id"),
                ),
                "update": _mutation(
                    "update",
                    "update_run_template",
                    {
                        "name": _NONEMPTY,
                        "description": _STR,
                        "hidden": _BOOL,
                        **_TAG_UPDATES,
                    },
                ),
                "delete": _mutation(
                    "delete", "delete_run_template", payload_required=False
                ),
            }
        ),
        "deployment": MappingProxyType(
            {
                "delete": _mutation(
                    "delete",
                    "delete_deployment",
                    {"force": _BOOL, "timeout": _ACTION_TIMEOUT},
                    payload_required=False,
                )
            }
        ),
        "artifact": MappingProxyType(
            {
                "update": _mutation(
                    "update",
                    "update_artifact",
                    {"name": _NONEMPTY, "has_custom_name": _BOOL, **_TAG_UPDATES},
                ),
                "delete": _mutation(
                    "delete", "delete_artifact", payload_required=False
                ),
            }
        ),
        "artifact_version": MappingProxyType(
            {
                "update": _mutation(
                    "update",
                    "update_artifact_version",
                    _TAG_UPDATES,
                    parents=("artifact_id",),
                ),
                "delete": _mutation(
                    "delete",
                    "delete_artifact_version",
                    {"delete_metadata": _BOOL, "delete_from_artifact_store": _BOOL},
                    parents=("artifact_id",),
                    payload_required=False,
                    constraints={
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {"delete_metadata": {"const": True}},
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "delete_from_artifact_store": {"const": True}
                                },
                                "required": ["delete_from_artifact_store"],
                            },
                        ]
                    },
                ),
            }
        ),
        "model": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_model",
                    {
                        **_CARD_FIELDS,
                        "tags": _STRING_LIST,
                        "save_models_to_registry": _BOOL,
                    },
                    required=("name",),
                ),
                "update": _mutation(
                    "update",
                    "update_model",
                    {**_CARD_FIELDS, **_TAG_UPDATES, "save_models_to_registry": _BOOL},
                ),
                "delete": _mutation("delete", "delete_model", payload_required=False),
            }
        ),
        "model_version": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_model_version",
                    {"name": _NONEMPTY, "description": _STR, "tags": _STRING_LIST},
                    parents=("model_id",),
                    payload_required=False,
                ),
                "update": _mutation(
                    "update",
                    "update_model_version",
                    {
                        "stage": _MODEL_STAGE,
                        "force": _BOOL,
                        "name": _NONEMPTY,
                        "description": _STR,
                        **_TAG_UPDATES,
                    },
                    parents=("model_id",),
                ),
                "delete": _mutation(
                    "delete",
                    "delete_model_version",
                    parents=("model_id",),
                    payload_required=False,
                ),
            }
        ),
        "tag": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_tag",
                    {"name": _NONEMPTY, "exclusive": _BOOL, "color": _COLOR},
                    required=("name",),
                ),
                "update": _mutation(
                    "update",
                    "update_tag",
                    {"name": _NONEMPTY, "exclusive": _BOOL, "color": _COLOR},
                ),
                "delete": _mutation("delete", "delete_tag", payload_required=False),
            }
        ),
        "service_connector": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_service_connector",
                    {
                        "name": _NONEMPTY,
                        "connector_type": _NONEMPTY,
                        "resource_type": _STR,
                        "auth_method": _NONEMPTY,
                        "configuration": _STRING_MAP,
                        "resource_id": _STR,
                        "description": _STR,
                        "expiration_seconds": _NONNEGATIVE_INT,
                        "expires_at": _DATETIME,
                        "expires_skew_tolerance": _NONNEGATIVE_INT,
                        "labels": _STRING_MAP,
                    },
                    required=("name", "connector_type"),
                ),
                "update": _mutation(
                    "update",
                    "update_service_connector",
                    {
                        "name": _NONEMPTY,
                        "auth_method": _NONEMPTY,
                        "resource_type": _STR,
                        "configuration": _STRING_MAP,
                        "resource_id": _STR,
                        "description": _STR,
                        "expiration_seconds": _NONNEGATIVE_INT,
                        "expires_at": _DATETIME,
                        "expires_skew_tolerance": _NONNEGATIVE_INT,
                        "labels": _NULLABLE_STRING_MAP,
                    },
                ),
                "delete": _mutation(
                    "delete", "delete_service_connector", payload_required=False
                ),
            }
        ),
        "code_repository": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_code_repository",
                    {
                        "name": _NONEMPTY,
                        "source": _NONEMPTY,
                        "config": _ANY_MAP,
                        "description": _STR,
                        "logo_url": _STR,
                    },
                    required=("name", "source", "config"),
                ),
                "update": _mutation(
                    "update",
                    "update_code_repository",
                    {
                        "name": _NONEMPTY,
                        "description": _STR,
                        "logo_url": _STR,
                        "config": {"type": "object", "additionalProperties": True},
                    },
                ),
                "delete": _mutation(
                    "delete", "delete_code_repository", payload_required=False
                ),
            }
        ),
        "webhook": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_webhook",
                    {
                        "name": _NONEMPTY,
                        "webhook_type": _NONEMPTY,
                        "active": _BOOL,
                        "secret": _NONEMPTY,
                    },
                    required=("name", "webhook_type"),
                ),
                "update": _mutation(
                    "update", "update_webhook", {"name": _NONEMPTY, "active": _BOOL}
                ),
                "delete": _mutation("delete", "delete_webhook", payload_required=False),
            }
        ),
        "schedule_trigger": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_schedule_trigger",
                    {
                        **_TRIGGER_COMMON,
                        "cron_expression": _NONEMPTY,
                        "interval": {"type": "integer", "minimum": 60},
                        "run_once_start_time": _DATETIME,
                        "start_time": _DATETIME,
                        "end_time": _DATETIME,
                        "max_runs": _POSITIVE_INT,
                    },
                    required=("name",),
                    constraints={
                        "oneOf": [
                            {
                                "type": "object",
                                "required": ["cron_expression"],
                                "not": {
                                    "anyOf": [
                                        {"type": "object", "required": ["interval"]},
                                        {
                                            "type": "object",
                                            "required": ["run_once_start_time"],
                                        },
                                    ]
                                },
                            },
                            {
                                "type": "object",
                                "required": ["interval", "start_time"],
                                "not": {
                                    "anyOf": [
                                        {
                                            "type": "object",
                                            "required": ["cron_expression"],
                                        },
                                        {
                                            "type": "object",
                                            "required": ["run_once_start_time"],
                                        },
                                    ]
                                },
                            },
                            {
                                "type": "object",
                                "required": ["run_once_start_time"],
                                "not": {
                                    "anyOf": [
                                        {
                                            "type": "object",
                                            "required": ["cron_expression"],
                                        },
                                        {"type": "object", "required": ["interval"]},
                                    ]
                                },
                            },
                        ]
                    },
                    example={"name": "<name>", "cron_expression": "0 * * * *"},
                ),
                "update": _mutation(
                    "update",
                    "update_schedule_trigger",
                    {
                        **_TRIGGER_COMMON,
                        "cron_expression": _NONEMPTY,
                        "interval": {"type": "integer", "minimum": 60},
                        "run_once_start_time": _DATETIME,
                        "start_time": _DATETIME,
                        "end_time": _DATETIME,
                        "max_runs": _POSITIVE_INT,
                    },
                    constraints={
                        "not": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "required": ["cron_expression", "interval"],
                                },
                                {
                                    "type": "object",
                                    "required": [
                                        "cron_expression",
                                        "run_once_start_time",
                                    ],
                                },
                                {
                                    "type": "object",
                                    "required": ["interval", "run_once_start_time"],
                                },
                            ]
                        }
                    },
                ),
                "delete": _mutation("delete", "delete_trigger", payload_required=False),
            }
        ),
        "platform_event_trigger": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_platform_event_trigger",
                    {
                        **_TRIGGER_COMMON,
                        "source_type": _SOURCE_TYPE,
                        "source_id": _UUID,
                        "target_events": {
                            "type": "array",
                            "items": _NONEMPTY,
                            "minItems": 1,
                        },
                    },
                    required=("name", "source_type", "source_id", "target_events"),
                ),
                "update": _mutation(
                    "update",
                    "update_platform_event_trigger",
                    {
                        **_TRIGGER_COMMON,
                        "source_type": _SOURCE_TYPE,
                        "source_id": _UUID,
                        "target_events": {
                            "type": "array",
                            "items": _NONEMPTY,
                            "minItems": 1,
                        },
                    },
                ),
                "delete": _mutation("delete", "delete_trigger", payload_required=False),
            }
        ),
        "webhook_trigger": MappingProxyType(
            {
                "create": _mutation(
                    "create",
                    "create_webhook_trigger",
                    {**_TRIGGER_COMMON, "webhook_id": _UUID, "configuration": _ANY_MAP},
                    required=("name", "webhook_id", "configuration"),
                ),
                "update": _mutation(
                    "update",
                    "update_webhook_trigger",
                    {**_TRIGGER_COMMON, "configuration": _ANY_MAP},
                ),
                "delete": _mutation("delete", "delete_trigger", payload_required=False),
            }
        ),
        "hook_invocation": MappingProxyType(
            {
                "delete": _mutation(
                    "delete", "delete_hook_invocation", payload_required=False
                )
            }
        ),
    }
)

_PROJECT_CREATE_RESOURCES = frozenset(
    {
        "service",
        "run_template",
        "model",
        "model_version",
        "code_repository",
        "webhook",
        "schedule_trigger",
        "platform_event_trigger",
        "webhook_trigger",
    }
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
        mutations=_MUTATION_SPECS.get(resource_type, MappingProxyType({})),
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

_REPLAY_CONFIGURATION = {
    "type": "object",
    "properties": {
        "skip_successful_steps": _BOOL,
        "steps_to_skip": {"type": "array", "items": _NONEMPTY, "uniqueItems": True},
        "step_input_overrides": {
            "type": "object",
            "additionalProperties": _CONFIG_MAP,
        },
        "step_default_input_overrides": {
            "type": "object",
            "additionalProperties": _CONFIG_MAP,
        },
    },
    "additionalProperties": False,
}
_TRIGGER_RUN_CONFIGURATION = {
    "type": "object",
    "properties": {
        "run_name": _NONEMPTY,
        "enable_cache": _BOOL,
        "enable_artifact_metadata": _BOOL,
        "enable_artifact_visualization": _BOOL,
        "enable_step_logs": _BOOL,
        "enable_pipeline_logs": _BOOL,
        "enable_heartbeat": _BOOL,
        "substitutions": _STRING_MAP,
        "execution_mode": {
            "type": "string",
            "enum": ["fail_fast", "stop_on_failure", "continue_on_failure"],
        },
    },
    "additionalProperties": False,
}
_TAGGABLE_RESOURCE = {
    "type": "string",
    "enum": [
        "artifact",
        "artifact_version",
        "model",
        "model_version",
        "pipeline",
        "pipeline_run",
        "run_template",
        "pipeline_snapshot",
        "deployment",
    ],
}


def _action(
    resource_type: str,
    action: str,
    sdk_method: str,
    properties: Mapping[str, dict[str, Any]] | None = None,
    *,
    required: tuple[str, ...] = (),
    prerequisites: tuple[str, ...] = (),
) -> ActionSpec:
    return ActionSpec(
        resource_type=resource_type,
        action=action,
        sdk_method=sdk_method,
        payload_properties=MappingProxyType(dict(properties or {})),
        required_payload=required,
        prerequisites=prerequisites,
    )


_TRIGGER_ACTIONS = (
    tuple(
        _action(
            resource_type,
            "attach",
            "attach_trigger_to_snapshot",
            {
                "snapshot_id": _UUID,
                "allow_replace": _BOOL,
                "run_configuration": _TRIGGER_RUN_CONFIGURATION,
            },
            required=("snapshot_id",),
        )
        for resource_type in (
            "schedule_trigger",
            "platform_event_trigger",
            "webhook_trigger",
        )
    )
    + tuple(
        _action(
            resource_type,
            "detach",
            "detach_trigger_from_snapshot",
            {"snapshot_id": _UUID},
            required=("snapshot_id",),
        )
        for resource_type in (
            "schedule_trigger",
            "platform_event_trigger",
            "webhook_trigger",
        )
    )
    + tuple(
        _action(
            resource_type,
            "clear_dispatch_error",
            "clear_trigger_dispatch_error",
            {"snapshot_id": _UUID},
        )
        for resource_type in (
            "schedule_trigger",
            "platform_event_trigger",
            "webhook_trigger",
        )
    )
)

_ACTION_SPECS = (
    _action(
        "pipeline_run",
        "replay",
        "replay_pipeline_run",
        {"run_configuration": _REPLAY_CONFIGURATION},
    ),
    *_TRIGGER_ACTIONS,
    _action(
        "deployment",
        "provision",
        "provision_deployment",
        {"snapshot_id": _UUID, "timeout": _ACTION_TIMEOUT},
        prerequisites=(
            "An installed deployer integration and its external credentials are required.",
        ),
    ),
    _action(
        "deployment",
        "deprovision",
        "deprovision_deployment",
        {"timeout": _ACTION_TIMEOUT},
        prerequisites=(
            "The deployment must retain an installed deployer integration and its external credentials.",
        ),
    ),
    _action(
        "deployment",
        "refresh",
        "refresh_deployment",
        prerequisites=(
            "The deployment must retain an installed deployer integration and its external credentials.",
        ),
    ),
    _action(
        "run_wait_condition",
        "resolve",
        "resolve_run_wait_condition",
        {
            "resolution": {"type": "string", "enum": ["CONTINUE", "ABORT"]},
            "result": {},
        },
        required=("resolution",),
    ),
    _action(
        "tag",
        "attach",
        "zen_store.batch_create_tag_resource",
        {
            "target_id": _UUID,
            "target_type": _TAGGABLE_RESOURCE,
            "allow_exclusive_replace": _BOOL,
            "artifact_id": _UUID,
            "model_id": _UUID,
        },
        required=("target_id", "target_type"),
    ),
    _action(
        "tag",
        "detach",
        "zen_store.batch_delete_tag_resource",
        {
            "target_id": _UUID,
            "target_type": _TAGGABLE_RESOURCE,
            "artifact_id": _UUID,
            "model_id": _UUID,
        },
        required=("target_id", "target_type"),
    ),
    _action(
        "webhook",
        "rotate_secret",
        "rotate_webhook_secret",
        {"secret": _NONEMPTY},
    ),
)

ACTION_REGISTRY: Mapping[tuple[str, str], ActionSpec] = MappingProxyType(
    {(spec.resource_type, spec.action): spec for spec in _ACTION_SPECS}
)


def get_action_spec(resource_type: str, action: str) -> ActionSpec:
    """Return one exact action pair, rejecting aliases and inferred methods."""
    try:
        return ACTION_REGISTRY[(resource_type, action)]
    except KeyError as error:
        supported = sorted(
            candidate.action
            for candidate in ACTION_REGISTRY.values()
            if candidate.resource_type == resource_type
        )
        suffix = f"; supported actions: {', '.join(supported)}" if supported else ""
        raise ResourceRegistryError(
            f"Unsupported action {action!r} for {resource_type!r}{suffix}"
        ) from error


def validate_action_payload(
    resource_type: str, action: str, payload: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Validate action input before any client or SDK method is inspected."""
    spec = get_action_spec(resource_type, action)
    value = dict(payload or {})
    unsupported = sorted(set(value) - set(spec.payload_properties))
    if unsupported:
        allowed = ", ".join(spec.payload_properties) or "none"
        raise ResourceRegistryError(
            f"Unsupported fields for {resource_type!r} {action}: "
            f"{', '.join(unsupported)}. Allowed fields: {allowed}"
        )
    missing = sorted(set(spec.required_payload) - set(value))
    if missing:
        raise ResourceRegistryError(
            f"{resource_type!r} {action} requires fields: {', '.join(missing)}"
        )
    for field, item in value.items():
        if not _matches_schema(item, spec.payload_properties[field]):
            raise ResourceRegistryError(
                f"Invalid value for {resource_type!r} {action} field {field!r}"
            )
    return value


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
    resource_type: str | None = None,
    operation: str | None = None,
    *,
    write_policy: str | None = None,
) -> dict[str, Any]:
    """Return the bounded catalog or one operation schema."""
    effective_policy = write_policy or configured_write_policy()
    read_only = effective_policy == "read_only"

    def has_write_operations(spec: ResourceSpec) -> bool:
        return any(
            item in spec.operations for item in ("create", "update", "delete")
        ) or any(key[0] == spec.resource_type for key in ACTION_REGISTRY)

    def available_operations(spec: ResourceSpec) -> list[str]:
        operations: list[str] = [
            str(item)
            for item in spec.operations
            if not (read_only and item in {"create", "update", "delete"})
        ]
        if not read_only and any(
            key[0] == spec.resource_type for key in ACTION_REGISTRY
        ):
            operations.append("action")
        return operations

    if resource_type is None:
        if operation is not None:
            raise ResourceRegistryError("operation requires resource_type")
        return {
            "resources": [
                {
                    "resource_type": spec.resource_type,
                    "operations": available_operations(spec),
                    "scope": spec.scope,
                    "policy": "read_only"
                    if read_only
                    else "read_write"
                    if has_write_operations(spec)
                    else "read_only",
                    "description": spec.description,
                }
                for spec in RESOURCE_REGISTRY.values()
            ]
        }

    spec = get_resource_spec(resource_type)
    if operation is None:
        return {
            "resource_type": spec.resource_type,
            "operations": available_operations(spec),
            "actions": []
            if read_only
            else [
                action
                for candidate, action in ACTION_REGISTRY
                if candidate == resource_type
            ],
            "scope": spec.scope,
            "policy": "read_only"
            if read_only
            else "read_write"
            if has_write_operations(spec)
            else "read_only",
            "description": spec.description,
        }

    if read_only and operation in {"create", "update", "delete", "action"}:
        raise ResourceRegistryError(
            f"Operation {operation!r} is disabled by the read-only policy"
        )
    if operation == "action":
        actions = [
            action_spec
            for (candidate, _), action_spec in ACTION_REGISTRY.items()
            if candidate == resource_type
        ]
        if not actions:
            raise ResourceRegistryError(
                f"Unsupported operation 'action' for {resource_type!r}"
            )
        return {
            "resource_type": resource_type,
            "operation": "action",
            "scope": "project",
            "resource_scope": spec.scope,
            "scope_note": (
                "project_id selects and verifies the affected resource or relation target; "
                "it does not change the client's active project."
            ),
            "actions": [
                {
                    "action": action_spec.action,
                    "sdk_method": action_spec.sdk_method,
                    "input_schema": action_spec.schema(),
                    "prerequisites": list(action_spec.prerequisites),
                }
                for action_spec in actions
            ],
            "output_projection": (
                "Credential keys and opaque configuration, environment, parameter, "
                "settings, secret, and value containers are omitted recursively. "
                "A newly issued webhook secret is returned once."
            ),
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
    elif operation == "get":
        example.update(
            {field: f"<{field}>" for field in operation_spec.required_fields}
        )
    else:
        for field in operation_spec.required_fields:
            if field == "payload":
                mutation = spec.mutations[operation]
                example[field] = (
                    dict(mutation.example_payload)
                    if mutation.example_payload is not None
                    else {key: f"<{key}>" for key in mutation.required_payload}
                )
            else:
                example[field] = f"<{field}>"
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
