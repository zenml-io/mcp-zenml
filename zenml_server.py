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


class NullHandler(logging.Handler):
    def emit(self, record):
        pass


class DebugHandler(logging.Handler):
    def emit(self, record):
        # Write to our debug log file
        with open("zenml_debug.log", "a") as f:
            f.write(f"{record.levelname}: {record.getMessage()}\n")
            if record.exc_info:
                f.write(
                    f"Exception:\n{''.join(traceback.format_exception(*record.exc_info))}\n"
                )


# Configure debug logging
debug_logger = logging.getLogger("debug")
debug_logger.setLevel(logging.DEBUG)
debug_logger.addHandler(DebugHandler())
debug_logger.propagate = False

# Configure root logger before anything else
root = logging.getLogger()
root.addHandler(NullHandler())
root.setLevel(logging.WARNING)

# Disable all logging output
logging.basicConfig(handlers=[NullHandler()], level=logging.WARNING, force=True)

# Ensure no other loggers can output anything
for logger_name in ("zenml", "mcp", "urllib3", "requests"):
    logger = logging.getLogger(logger_name)
    logger.addHandler(NullHandler())
    logger.setLevel(logging.WARNING)
    logger.propagate = False

# Redirect stderr to a file to prevent it mixing with stdout JSON
sys.stderr = open("zenml_server.log", "w")

try:
    from mcp.server.fastmcp import FastMCP
    from zenml.client import Client

    # Initialize FastMCP server
    mcp = FastMCP("zenml")

    # Initialize ZenML client with minimal logging
    zenml_client = Client()

    debug_logger.info("Successfully initialized FastMCP and ZenML client")
except Exception as e:
    debug_logger.error(f"Error during initialization: {str(e)}", exc_info=True)
    raise


@mcp.tool()
def get_settings() -> str:
    """Get the current settings for the ZenML server."""
    try:
        settings = zenml_client.get_settings()
        return f"Settings: {settings}"
    except Exception as e:
        debug_logger.error(f"Error in get_settings: {str(e)}", exc_info=True)
        return f"Error retrieving settings: {str(e)}"


@mcp.tool()
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
    try:
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
    except Exception as e:
        debug_logger.error(f"Error in list_users: {str(e)}", exc_info=True)
        return f"Error listing users: {str(e)}"


@mcp.tool()
def get_user(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific user."""
    try:
        user = zenml_client.get_user(name_id_or_prefix)
        return f"""# User: {user}"""
    except Exception as e:
        debug_logger.error(f"Error in get_user: {str(e)}", exc_info=True)
        return f"Error retrieving user: {str(e)}"


@mcp.tool()
def get_active_user() -> str:
    """Get the currently active user."""
    try:
        user = zenml_client.active_user
        return f"""# Active User: {user}"""
    except Exception as e:
        debug_logger.error(f"Error in get_active_user: {str(e)}", exc_info=True)
        return f"Error retrieving active user: {str(e)}"


@mcp.tool()
def get_stack(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific stack."""
    try:
        stack = zenml_client.get_stack(name_id_or_prefix)
        return f"""# Stack: {stack}"""
    except Exception as e:
        debug_logger.error(f"Error in get_stack: {str(e)}", exc_info=True)
        return f"Error retrieving stack: {str(e)}"


@mcp.tool()
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
    try:
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
    except Exception as e:
        debug_logger.error(f"Error in list_stacks: {str(e)}", exc_info=True)
        return f"Error listing stacks: {str(e)}"


@mcp.tool()
def get_active_stack() -> str:
    """Get the currently active stack."""
    try:
        stack = zenml_client.active_stack
        return f"""# Active Stack: {stack}"""
    except Exception as e:
        debug_logger.error(f"Error in get_active_stack: {str(e)}", exc_info=True)
        return f"Error retrieving active stack: {str(e)}"


@mcp.tool()
def activate_stack(name_id_or_prefix: str) -> str:
    """Activate a specific stack.

    Sets the stack as active.
    """
    try:
        zenml_client.activate_stack(name_id_or_prefix)
        return f"Stack activated: {name_id_or_prefix}"
    except Exception as e:
        debug_logger.error(f"Error in activate_stack: {str(e)}", exc_info=True)
        return f"Error activating stack: {str(e)}"


@mcp.tool()
def list_pipelines() -> str:
    """List all pipelines in the ZenML workspace."""
    try:
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
            "\n".join(formatted_pipelines)
            if formatted_pipelines
            else "No pipelines found."
        )
    except Exception as e:
        debug_logger.error(f"Error in list_pipelines: {str(e)}", exc_info=True)
        return f"Error listing pipelines: {str(e)}"


@mcp.tool()
def get_pipeline_details(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific pipeline.

    Args:
        name_id_or_prefix: The name, ID or prefix of the pipeline to retrieve
    """
    try:
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
    except Exception as e:
        debug_logger.error(f"Error in get_pipeline_details: {str(e)}", exc_info=True)
        return f"Error retrieving pipeline details: {str(e)}"


if __name__ == "__main__":
    try:
        debug_logger.info("Starting server...")
        # Initialize and run the server
        mcp.run(transport="stdio")
    except Exception as e:
        debug_logger.error(f"Error running server: {str(e)}", exc_info=True)
        raise
