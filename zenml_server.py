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


if __name__ == "__main__":
    try:
        mcp.run(transport="stdio")
    except Exception as e:
        print(f"Error running server: {str(e)}", file=sys.stderr)
        raise
