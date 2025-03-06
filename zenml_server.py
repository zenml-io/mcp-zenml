# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "mcp[cli]",
#     "zenml",
# ]
# ///
import sys
import logging
import functools
import os
from typing import Callable, Any, TypeVar, cast
from zenml.models.v2.core.pipeline import PipelineResponse

# Configure minimal logging to stderr
log_level_name = os.environ.get("LOGLEVEL", "WARNING").upper()
log_level = getattr(logging, log_level_name, logging.WARNING)

# Simple stderr logging configuration
logging.basicConfig(
    level=log_level,
    format="%(levelname)s: %(message)s",
)

# Type variable for function return type
T = TypeVar("T")


# Decorator for handling exceptions in tool functions
def handle_exceptions(func: Callable[..., T]) -> Callable[..., T]:
    """Decorator to handle exceptions in tool functions and return a friendly error message."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            # Print error to stderr for MCP to capture
            print(f"Error in {func.__name__}: {str(e)}", file=sys.stderr)
            return cast(T, f"Error in {func.__name__}: {str(e)}")

    return wrapper


try:
    from mcp.server.fastmcp import FastMCP
    from zenml.client import Client

    # Initialize FastMCP server
    mcp = FastMCP("zenml")

    # Initialize ZenML client
    zenml_client = Client()
except Exception as e:
    print(f"Error during initialization: {str(e)}", file=sys.stderr)
    raise


@mcp.tool()
@handle_exceptions
def get_settings() -> str:
    """Get the current settings for the ZenML server."""
    settings = zenml_client.get_settings()
    return f"Settings: {settings}"


@mcp.tool()
@handle_exceptions
def list_users(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    id: str = None,
    external_user_id: str = None,
    created: str = None,
    updated: str = None,
    active: bool = None,
) -> str:
    """List all users in the ZenML workspace."""
    users = zenml_client.list_users(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        id=id,
        external_user_id=external_user_id,
        created=created,
        updated=updated,
        active=active,
    )
    return f"""# Users: {users}"""


@mcp.tool()
@handle_exceptions
def get_user(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific user."""
    user = zenml_client.get_user(name_id_or_prefix)
    return f"""# User: {user}"""


@mcp.tool()
@handle_exceptions
def get_active_user() -> str:
    """Get the currently active user."""
    user = zenml_client.active_user
    return f"""# Active User: {user}"""


@mcp.tool()
@handle_exceptions
def get_stack(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific stack."""
    stack = zenml_client.get_stack(name_id_or_prefix)
    return f"""# Stack: {stack}"""


@mcp.tool()
@handle_exceptions
def list_stacks(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str = None,
    updated: str = None,
    name: str = None,
) -> str:
    """List all stacks in the ZenML workspace.

    By default, the stacks are sorted by creation date in descending order.

    Args:
        sort_by: The field to sort the stacks by
        page: The page number to return
        size: The number of stacks to return
        created: The creation date of the stacks
    """
    stacks = zenml_client.list_stacks(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
    )
    return f"""# Stacks: {stacks}"""


@mcp.tool()
@handle_exceptions
def list_pipelines(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    created: str = None,
    updated: str = None,
) -> str:
    """List all pipelines in the ZenML workspace.

    By default, the pipelines are sorted by creation date in descending order.

    Args:
        sort_by: The field to sort the pipelines by
        page: The page number to return
        size: The number of pipelines to return
        created: The creation date of the pipelines
        updated: The last update date of the pipelines
    """
    pipelines = zenml_client.list_pipelines(
        sort_by=sort_by,
        page=page,
        size=size,
        created=created,
        updated=updated,
    )

    # Format pipeline data for readable output
    formatted_pipelines = []
    for pipeline in pipelines:
        formatted_pipelines.append(
            f"Pipeline: {pipeline.name}\n"
            f"ID: {pipeline.id}\n"
            f"Status of latest run: {pipeline.latest_run_status}\n"
            f"Created: {pipeline.created}\n"
            "---"
        )

    return (
        "\n".join(formatted_pipelines) if formatted_pipelines else "No pipelines found."
    )


def get_latest_runs_status(
    pipeline_response: PipelineResponse, num_runs: int = 5
) -> str:
    """Get the status of the latest run of a pipeline."""
    latest_runs = pipeline_response.runs[:num_runs]
    statuses = [run.status for run in latest_runs]
    return f"Status of latest {num_runs} runs: {statuses}"


@mcp.tool()
@handle_exceptions
def get_pipeline_details(name_id_or_prefix: str, num_runs: int = 5) -> str:
    """Get detailed information about a specific pipeline.

    Args:
        name_id_or_prefix: The name, ID or prefix of the pipeline to retrieve
    """
    pipeline = zenml_client.get_pipeline(name_id_or_prefix)
    return f"""
Pipeline Details:
Name: {pipeline.name}
ID: {pipeline.id}
Status of latest run: {pipeline.latest_run_status}
Created: {pipeline.created}
Last Updated: {pipeline.updated}
Status of latest {num_runs} runs: {get_latest_runs_status(pipeline, num_runs)}
"""


@mcp.tool()
@handle_exceptions
def get_service(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific service."""
    service = zenml_client.get_service(name_id_or_prefix)
    return f"""# Service: {service.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_exceptions
def list_services(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    id: str = None,
    created: str = None,
    updated: str = None,
    name: str = None,
    running: bool = None,
    service_name: str = None,
    pipeline_name: str = None,
    pipeline_run_id: str = None,
    pipeline_step_name: str = None,
    model_version_id: str = None,
) -> str:
    """List all services in the ZenML workspace."""
    services = zenml_client.list_services(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        id=id,
        created=created,
        updated=updated,
        name=name,
        running=running,
        service_name=service_name,
        pipeline_name=pipeline_name,
        pipeline_run_id=pipeline_run_id,
        pipeline_step_name=pipeline_step_name,
        model_version_id=model_version_id,
    )
    return (
        f"""# Services: {[service.model_dump_json(indent=2) for service in services]}"""
    )


@mcp.tool()
@handle_exceptions
def get_stack_component(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific stack component."""
    stack_component = zenml_client.get_stack_component(name_id_or_prefix)
    return f"""# Stack Component: {stack_component.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_exceptions
def list_stack_components(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str = None,
    updated: str = None,
    name: str = None,
    flavor: str = None,
    stack_id: str = None,
) -> str:
    """List all stack components in the ZenML workspace."""
    stack_components = zenml_client.list_stack_components(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        flavor=flavor,
        stack_id=stack_id,
    )
    return f"""# Stack Components: {[component.model_dump_json(indent=2) for component in stack_components]}"""


@mcp.tool()
@handle_exceptions
def get_flavor(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific flavor."""
    flavor = zenml_client.get_flavor(name_id_or_prefix)
    return f"""# Flavor: {flavor.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_exceptions
def list_flavors(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    id: str = None,
    created: str = None,
    updated: str = None,
    name: str = None,
    integration: str = None,
) -> str:
    """List all flavors in the ZenML workspace."""
    flavors = zenml_client.list_flavors(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        id=id,
        created=created,
        updated=updated,
        name=name,
        integration=integration,
    )
    return f"""# Flavors: {[flavor.model_dump_json(indent=2) for flavor in flavors]}"""


@mcp.tool()
@handle_exceptions
def trigger_pipeline(
    pipeline_name_or_id: str,
    template_id: str = None,
    stack_name_or_id: str = None,
) -> str:
    """Trigger a pipeline to run from the server.

    Usage examples:
        * Run the latest runnable template for a pipeline:
        ```python
        trigger_pipeline(pipeline_name_or_id=<NAME>)
        ```
        * Run the latest runnable template for a pipeline on a specific stack:
        ```python
        trigger_pipeline(
            pipeline_name_or_id=<NAME>,
            stack_name_or_id=<STACK_NAME_OR_ID>
        )
        ```
        * Run a specific template:
        ```python
        trigger_pipeline(template_id=<ID>)
        ```
    """
    pipeline_run = zenml_client.trigger_pipeline(
        pipeline_name_or_id=pipeline_name_or_id,
        template_id=template_id,
        stack_name_or_id=stack_name_or_id,
    )
    return f"""# Pipeline Run Response: {pipeline_run.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_exceptions
def get_run_template(name_id_or_prefix: str) -> str:
    """Get a run template for a pipeline."""
    run_template = zenml_client.get_run_template(name_id_or_prefix)
    return f"""# Run Template: {run_template.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_exceptions
def list_run_templates(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    created: str = None,
    updated: str = None,
    name: str = None,
    tag: str = None,
) -> str:
    """List all run templates in the ZenML workspace."""
    run_templates = zenml_client.list_run_templates(
        sort_by=sort_by,
        page=page,
        size=size,
        created=created,
        updated=updated,
        name=name,
        tag=tag,
    )
    return f"""# Run Templates: {[run_template.model_dump_json(indent=2) for run_template in run_templates]}"""


@mcp.tool()
@handle_exceptions
def get_schedule(name_id_or_prefix: str) -> str:
    """Get a schedule for a pipeline."""
    schedule = zenml_client.get_schedule(name_id_or_prefix)
    return f"""# Schedule: {schedule.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_exceptions
def list_schedules(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str = None,
    updated: str = None,
    name: str = None,
    pipeline_id: str = None,
    orchestrator_id: str = None,
    active: bool = None,
) -> str:
    """List all schedules in the ZenML workspace.

    Args:
        sort_by: The field to sort the schedules by
        page: The page number to return
        size: The number of schedules to return
        created: The creation date of the schedules
        updated: The last update date of the schedules
        name: The name of the schedules
    """
    schedules = zenml_client.list_schedules(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        pipeline_id=pipeline_id,
        orchestrator_id=orchestrator_id,
        active=active,
    )
    return f"""# Schedules: {[schedule.model_dump_json(indent=2) for schedule in schedules]}"""


@mcp.tool()
@handle_exceptions
def get_pipeline_run(name_id_or_prefix: str) -> str:
    """Get a pipeline run by name, ID, or prefix."""
    pipeline_run = zenml_client.get_pipeline_run(name_id_or_prefix)
    return f"""# Pipeline Run: {pipeline_run.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_exceptions
def list_pipeline_runs(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str = None,
    updated: str = None,
    name: str = None,
    pipeline_id: str = None,
    pipeline_name: str = None,
    stack_id: str = None,
    status: str = None,
    start_time: str = None,
    end_time: str = None,
    num_steps: int = None,
    stack: str = None,
    stack_component: str = None,
) -> str:
    """List all pipeline runs in the ZenML workspace."""
    pipeline_runs = zenml_client.list_pipeline_runs(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        pipeline_id=pipeline_id,
        pipeline_name=pipeline_name,
        stack_id=stack_id,
        status=status,
        start_time=start_time,
        end_time=end_time,
        num_steps=num_steps,
        stack=stack,
    )
    return f"""# Pipeline Runs: {[pipeline_run.model_dump_json(indent=2) for pipeline_run in pipeline_runs]}"""


@mcp.tool()
@handle_exceptions
def get_run_step(step_run_id: str) -> str:
    """Get a run step by name, ID, or prefix."""
    run_step = zenml_client.get_run_step(step_run_id)
    return f"""# Run Step: {run_step.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_exceptions
def list_run_steps(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str = None,
    updated: str = None,
    name: str = None,
    status: str = None,
    start_time: str = None,
    end_time: str = None,
    pipeline_run_id: str = None,
) -> str:
    """List all run steps in the ZenML workspace."""
    run_steps = zenml_client.list_run_steps(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        status=status,
        start_time=start_time,
        end_time=end_time,
        pipeline_run_id=pipeline_run_id,
    )
    return f"""# Run Steps: {[run_step.model_dump_json(indent=2) for run_step in run_steps]}"""


@mcp.tool()
@handle_exceptions
def list_artifacts(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str = None,
    updated: str = None,
    name: str = None,
    tag: str = None,
) -> str:
    """List all artifacts in the ZenML workspace."""
    artifacts = zenml_client.list_artifacts(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        tag=tag,
    )
    return f"""# Artifacts: {[artifact.model_dump_json(indent=2) for artifact in artifacts]}"""


@mcp.tool()
@handle_exceptions
def list_secrets(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str = None,
    updated: str = None,
    name: str = None,
) -> str:
    """List all secrets in the ZenML workspace."""
    secrets = zenml_client.list_secrets(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
    )
    return f"""# Secrets: {[secret.model_dump_json(indent=2) for secret in secrets]}"""


@mcp.tool()
@handle_exceptions
def get_service_connector(name_id_or_prefix: str) -> str:
    """Get a service connector by name, ID, or prefix."""
    service_connector = zenml_client.get_service_connector(name_id_or_prefix)
    return f"""# Service Connector: {service_connector.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_exceptions
def list_service_connectors(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str = None,
    updated: str = None,
    name: str = None,
    connector_type: str = None,
) -> str:
    """List all service connectors in the ZenML workspace."""
    service_connectors = zenml_client.list_service_connectors(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        connector_type=connector_type,
    )
    return f"""# Service Connectors: {[service_connector.model_dump_json(indent=2) for service_connector in service_connectors]}"""


@mcp.tool()
@handle_exceptions
def get_model(name_id_or_prefix: str) -> str:
    """Get a model by name, ID, or prefix."""
    model = zenml_client.get_model(name_id_or_prefix)
    return f"""# Model: {model.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_exceptions
def list_models(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str = None,
    updated: str = None,
    name: str = None,
    tag: str = None,
) -> str:
    """List all models in the ZenML workspace."""
    models = zenml_client.list_models(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        tag=tag,
    )
    return f"""# Models: {[model.model_dump_json(indent=2) for model in models]}"""


@mcp.tool()
@handle_exceptions
def get_model_version(
    model_name_or_id: str,
    model_version_name_or_number_or_id: str,
) -> str:
    """Get a model version by name, ID, or prefix."""
    model_version = zenml_client.get_model_version(
        model_name_or_id,
        model_version_name_or_number_or_id,
    )
    return f"""# Model Version: {model_version.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_exceptions
def list_model_versions(
    model_name_or_id: str,
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str = None,
    updated: str = None,
    name: str = None,
    number: int = None,
    stage: str = None,
    tag: str = None,
) -> str:
    """List all model versions for a model."""
    model_versions = zenml_client.list_model_versions(
        model_name_or_id,
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        number=number,
        stage=stage,
        tag=tag,
    )
    return f"""# Model Versions: {[model_version.model_dump_json(indent=2) for model_version in model_versions]}"""


@mcp.tool()
@handle_exceptions
def get_step_code(
    step_run_id: str,
) -> str:
    """Get the code for a step."""
    step_code = zenml_client.get_run_step(step_run_id).source_code
    return f"""# Step Code: {step_code}"""


if __name__ == "__main__":
    try:
        mcp.run(transport="stdio")
    except Exception as e:
        print(f"Error running server: {str(e)}", file=sys.stderr)
        raise
