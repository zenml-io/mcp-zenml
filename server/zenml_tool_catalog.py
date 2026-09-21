"""Static tool membership for ZenML MCP registration profiles."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

ToolProfile = Literal["compact", "legacy"]
WritePolicy = Literal["read_only", "read_write"]

GENERIC_READ_TOOLS = (
    "zenml_describe_resources",
    "zenml_list_resources",
    "zenml_get_resource",
)
GENERIC_WRITE_TOOLS = (
    "zenml_create_resource",
    "zenml_update_resource",
    "zenml_delete_resource",
    "zenml_action_resource",
)
GENERIC_TOOLS = (*GENERIC_READ_TOOLS, *GENERIC_WRITE_TOOLS)

COMPACT_SPECIALIZED_TOOLS = (
    "diagnose_zenml_setup",
    "get_active_user",
    "get_active_project",
    "trigger_pipeline",
    "get_step_logs",
    "get_step_code",
    "get_deployment_logs",
    "open_pipeline_run_dashboard",
    "open_run_activity_chart",
)

# Captured by scripts/fixtures/legacy_tool_schemas.json before consolidation.
LEGACY_TOOLS = (
    "diagnose_zenml_setup",
    "get_step_logs",
    "list_users",
    "get_user",
    "get_active_user",
    "get_active_project",
    "get_project",
    "list_projects",
    "get_stack",
    "easter_egg",
    "list_stacks",
    "list_pipelines",
    "get_pipeline_details",
    "get_service",
    "list_services",
    "get_stack_component",
    "list_stack_components",
    "get_flavor",
    "list_flavors",
    "trigger_pipeline",
    "get_run_template",
    "list_run_templates",
    "get_snapshot",
    "list_snapshots",
    "get_deployment",
    "list_deployments",
    "get_deployment_logs",
    "get_schedule",
    "list_schedules",
    "get_pipeline_run",
    "list_pipeline_runs",
    "get_run_step",
    "list_run_steps",
    "list_artifacts",
    "get_artifact_version",
    "list_artifact_versions",
    "list_secrets",
    "get_service_connector",
    "list_service_connectors",
    "get_model",
    "list_models",
    "get_model_version",
    "list_model_versions",
    "get_step_code",
    "get_tag",
    "list_tags",
    "get_build",
    "list_builds",
    "open_pipeline_run_dashboard",
    "open_run_activity_chart",
)

ALL_TOOL_NAMES = tuple(
    dict.fromkeys((*LEGACY_TOOLS[:2], *GENERIC_TOOLS, *LEGACY_TOOLS[2:]))
)

MUTATING_TOOLS = frozenset((*GENERIC_WRITE_TOOLS, "trigger_pipeline"))


def configured_profile(environ: Mapping[str, str] | None = None) -> ToolProfile:
    """Return the validated registration profile, defaulting to compact."""
    source = os.environ if environ is None else environ
    value = source.get("ZENML_MCP_PROFILE", "compact").strip().lower()
    if value not in {"compact", "legacy"}:
        raise ValueError("ZENML_MCP_PROFILE must be 'compact' or 'legacy'")
    return value


def configured_write_policy(environ: Mapping[str, str] | None = None) -> WritePolicy:
    """Return the fail-closed write policy, including the legacy flag."""
    source = os.environ if environ is None else environ
    legacy = source.get("ZENML_MCP_READ_ONLY")
    if legacy is not None:
        normalized = legacy.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return "read_only"
        if normalized not in {"0", "false", "no", "off"}:
            return "read_only"
    value = source.get("ZENML_MCP_WRITE_POLICY", "read_write").strip().lower()
    return "read_write" if value == "read_write" else "read_only"


def tool_names(
    profile: ToolProfile = "compact",
    write_policy: WritePolicy = "read_write",
) -> tuple[str, ...]:
    """Return one ordered, duplicate-free advertised tool inventory."""
    if profile not in {"compact", "legacy"}:
        raise ValueError(f"Unknown tool profile: {profile}")
    if write_policy not in {"read_only", "read_write"}:
        raise ValueError(f"Unknown write policy: {write_policy}")
    permitted = (
        set((*GENERIC_TOOLS, *COMPACT_SPECIALIZED_TOOLS))
        if profile == "compact"
        else set(ALL_TOOL_NAMES)
    )
    names = tuple(name for name in ALL_TOOL_NAMES if name in permitted)
    if write_policy == "read_only":
        names = tuple(name for name in names if name not in MUTATING_TOOLS)
    return tuple(dict.fromkeys(names))


def configured_tool_names(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return tool membership for the configured profile and write policy."""
    return tool_names(configured_profile(environ), configured_write_policy(environ))


assert len(tool_names("compact", "read_write")) == 16
assert len(tool_names("compact", "read_only")) == 11
assert len(tool_names("legacy", "read_write")) == 57
assert len(tool_names("legacy", "read_only")) == 52
assert set(tool_names("legacy", "read_write")) == set(ALL_TOOL_NAMES)
