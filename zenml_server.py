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
import traceback
import functools
import os
from typing import Callable, Any, TypeVar, cast
from pathlib import Path

# Get log level from environment variable or default to WARNING
log_level_name = os.environ.get("LOGLEVEL", "WARNING").upper()
log_level = getattr(logging, log_level_name, logging.WARNING)

# Create a logs directory in the user's home directory
home_dir = Path.home()
log_dir = home_dir / ".zenml-mcp" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "zenml_server.log"

# Configure logging with a single file handler
logging.basicConfig(
    filename=str(log_file),
    level=log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Get the logger for this module
logger = logging.getLogger("zenml_server")
logger.info(f"Logging to {log_file}")

# Type variable for function return type
T = TypeVar("T")


# Decorator for handling exceptions in tool functions
def handle_exceptions(func: Callable[..., T]) -> Callable[..., T]:
    """Decorator to handle exceptions in tool functions and log them."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {str(e)}", exc_info=True)
            return cast(T, f"Error in {func.__name__}: {str(e)}")

    return wrapper


try:
    from mcp.server.fastmcp import FastMCP
    from zenml.client import Client

    # Initialize FastMCP server
    mcp = FastMCP("zenml")

    # Initialize ZenML client
    zenml_client = Client()

    logger.info("Successfully initialized FastMCP and ZenML client")
except Exception as e:
    logger.error(f"Error during initialization: {str(e)}", exc_info=True)
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
    sort_by: str = "created",
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
    sort_by: str = "created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    id: str = None,
    created: str = None,
    updated: str = None,
    name: str = None,
    description: str = None,
    workspace_id: str = None,
    user_id: str = None,
) -> str:
    """List all stacks in the ZenML workspace."""
    stacks = zenml_client.list_stacks(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        id=id,
        created=created,
        updated=updated,
        name=name,
        description=description,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    return f"""# Stacks: {stacks}"""


@mcp.tool()
@handle_exceptions
def get_active_stack() -> str:
    """Get the currently active stack."""
    stack = zenml_client.active_stack
    return f"""# Active Stack: {stack}"""


@mcp.tool()
@handle_exceptions
def activate_stack(name_id_or_prefix: str) -> str:
    """Activate a specific stack.

    Sets the stack as active.
    """
    zenml_client.activate_stack(name_id_or_prefix)
    return f"Stack activated: {name_id_or_prefix}"


@mcp.tool()
@handle_exceptions
def list_pipelines() -> str:
    """List all pipelines in the ZenML workspace."""
    pipelines = zenml_client.list_pipelines()

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


@mcp.tool()
@handle_exceptions
def get_pipeline_details(name_id_or_prefix: str) -> str:
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
Steps: {', '.join(step.name for step in pipeline.steps)}
"""


if __name__ == "__main__":
    try:
        logger.info("Starting server...")
        # Initialize and run the server
        mcp.run(transport="stdio")
    except Exception as e:
        logger.error(f"Error running server: {str(e)}", exc_info=True)
        raise
