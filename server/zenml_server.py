# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "mcp[cli]",
#     "zenml",
#     "setuptools",
#     "requests>=2.32.0",
# ]
# ///

# Ensure setuptools is imported first to provide distutils compatibility
try:
    import setuptools  # noqa
except ImportError:
    pass

import functools
import json
import logging
import os
import sys
import warnings
from threading import Lock
from typing import Any, Dict, ParamSpec, TypeVar, cast

import requests
import zenml_mcp_analytics as analytics

# Suppress ZenML warnings that print to stdout (breaks JSON-RPC protocol)
# E.g., "Setting the global active stack to default"
warnings.filterwarnings("ignore", module=r"^zenml(\.|$)")

logger = logging.getLogger(__name__)

# Configure minimal logging to stderr
log_level_name = os.environ.get("LOGLEVEL", "WARNING").upper()
log_level = max(getattr(logging, log_level_name, logging.WARNING), logging.WARNING)

# Simple stderr logging configuration - explicitly use stderr to avoid JSON protocol issues
logging.basicConfig(
    level=log_level,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,
)

# Never log below WARNING to prevent JSON protocol interference

# Suppress ZenML's internal logging to prevent JSON protocol issues
# Must use ERROR level (not WARNING) to suppress "Setting the global active stack" message
# Also clear any handlers ZenML may have added that write to stdout
zenml_logger = logging.getLogger("zenml")
zenml_logger.handlers.clear()  # Remove any stdout handlers ZenML may have added
zenml_logger.setLevel(logging.ERROR)  # Only show errors, not warnings
logging.getLogger("zenml.client").setLevel(logging.ERROR)

# Suppress MCP/FastMCP logging to prevent stdout pollution (breaks JSON-RPC protocol)
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("mcp.server").setLevel(logging.WARNING)
logging.getLogger("mcp.server.fastmcp").setLevel(logging.WARNING)

# Type variables for decorator signatures
P = ParamSpec("P")  # Captures function parameters
T = TypeVar("T")  # Captures return type

# Type alias for functions (callables with __name__ attribute)
# Using ParamSpec preserves the original function's parameter types
from collections.abc import Callable


# Decorator for handling exceptions in tool functions (with analytics tracking)
def handle_tool_exceptions(func: Callable[P, T]) -> Callable[P, T]:
    """Decorator for MCP tools - handles exceptions and tracks analytics.

    Use this decorator for @mcp.tool() functions. It:
    - Catches exceptions and returns friendly error messages
    - Tracks tool usage via analytics (timing, success/failure, size param)
    """
    # Capture function name at decoration time (avoids type checker issues with __name__)
    func_name = func.__name__  # type: ignore[attr-defined]

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        import time

        start_time = time.perf_counter()
        success = True
        error_type: str | None = None
        http_status_code: int | None = None

        try:
            return func(*args, **kwargs)
        except requests.HTTPError as e:
            success = False
            error_type = type(e).__name__
            http_status_code = (
                e.response.status_code
                if getattr(e, "response", None) is not None
                else None
            )

            if http_status_code == 401:
                message = "Authentication failed. Please check your API key."
            elif http_status_code == 404 and func_name == "get_step_logs":
                message = (
                    "Logs not found. Please check the step ID. "
                    "Also note that if the step was run on a stack with a local "
                    "or non-cloud-based artifact store then no logs will have been "
                    "stored by ZenML."
                )
            elif http_status_code == 404 and func_name == "get_deployment_logs":
                message = (
                    "Deployment not found or logs unavailable. Please check the deployment "
                    "name/ID. Note that log availability depends on the deployer type and "
                    "infrastructure configuration."
                )
            elif http_status_code is not None:
                message = f"Request failed (HTTP {http_status_code})."
            else:
                message = "Request failed."

            if analytics.DEV_MODE:
                message = f"{message} ({e})"

            err_log = f"Error in {func_name}: {error_type}"
            if http_status_code is not None:
                err_log = f"{err_log} (HTTP {http_status_code})"
            if analytics.DEV_MODE:
                err_log = f"{err_log} - {e}"
            print(err_log, file=sys.stderr)
            return cast(T, message)
        except Exception as e:
            success = False
            error_type = type(e).__name__

            # Always show details for ImportError/RuntimeError since they indicate setup/config issues
            if analytics.DEV_MODE or isinstance(e, (ImportError, RuntimeError)):
                error_detail = str(e)
            else:
                error_detail = error_type

            message = f"Error in {func_name}: {error_detail}"
            print(message, file=sys.stderr)
            return cast(T, message)
        finally:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            try:
                size = analytics.extract_size_from_call(func_name, args, kwargs)
                analytics.track_tool_call(
                    tool_name=func_name,
                    success=success,
                    duration_ms=duration_ms,
                    error_type=error_type,
                    size=size,
                    http_status_code=http_status_code,
                )
            except Exception:
                pass

    return wrapper


# Decorator for handling exceptions in prompts/resources (no analytics)
def handle_exceptions(func: Callable[P, T]) -> Callable[P, T]:
    """Decorator for prompts/resources - handles exceptions without analytics.

    Use this decorator for @mcp.prompt() and @mcp.resource() functions.
    It catches exceptions but does NOT track analytics (to avoid noise from
    non-tool endpoints).
    """
    # Capture function name at decoration time (avoids type checker issues with __name__)
    func_name = func.__name__  # type: ignore[attr-defined]

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_type = type(e).__name__
            error_detail = str(e) if analytics.DEV_MODE else error_type
            message = f"Error in {func_name}: {error_detail}"
            print(message, file=sys.stderr)
            return cast(T, message)

    return wrapper


INSTRUCTIONS = """
You are a helpful assistant that can answer questions about a user's ZenML
server.

You might want to use custom arguments passed into the tool functions to filter
and sort the results you're getting back. (By default, you generally will just
get a handful of recent results back, but you might want to get more, iterate
through the pages and so on.)

Since a lot of the data comes back in JSON format, you might want to present
this data to the user in a more readable format (e.g. a table).
"""

try:
    logger.debug("Importing MCP dependencies...")
    from mcp.server.fastmcp import FastMCP

    # Initialize FastMCP server
    logger.debug("Initializing FastMCP server...")
    mcp = FastMCP(name="zenml", instructions=INSTRUCTIONS)
    logger.debug("FastMCP server initialized successfully")

    # ZenML client will be initialized lazily
    zenml_client = None

except Exception as e:
    logger.error(f"Error during initialization: {str(e)}")
    raise


# Track if we've already reported client init failure (avoid spam)
_client_init_failure_reported = False
_zenml_client_init_lock = Lock()


def get_zenml_client():
    """Get or initialize the ZenML client lazily."""
    global zenml_client, _client_init_failure_reported
    if zenml_client is not None:
        return zenml_client

    with _zenml_client_init_lock:
        if zenml_client is not None:
            return zenml_client

        logger.debug("Lazy importing ZenML...")
        from zenml.client import Client

        logger.debug("Initializing ZenML client...")
        try:
            zenml_client = Client()
            logger.debug("ZenML client initialized successfully")
        except Exception as e:
            logger.error(f"ZenML client initialization failed: {e}")
            # Track client init failure (only report once per session)
            if not _client_init_failure_reported:
                _client_init_failure_reported = True
                analytics.track_event(
                    "Client Init Failed",
                    {
                        "error_type": type(e).__name__,
                    },
                )
            raise

    return zenml_client


def get_access_token(server_url: str, api_key: str) -> str:
    """
    Generate a short-lived access token using the ZenML API key.

    Args:
        server_url: The base URL of the ZenML server
        api_key: The ZenML API key

    Returns:
        The access token as a string

    Raises:
        requests.HTTPError: If the request fails
        ValueError: If the response doesn't contain an access token
    """
    # Ensure the server URL doesn't end with a slash
    server_url = server_url.rstrip("/")

    # Construct the login URL
    url = f"{server_url}/api/v1/login"

    logger.debug("Generating access token")

    # Make the request to get an access token
    response = requests.post(
        url,
        data={"password": api_key},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=(3.05, 30),
    )
    response.raise_for_status()

    # Parse the response
    token_data = response.json()

    # Check if the access token is in the response
    if "access_token" not in token_data:
        raise ValueError("No access token in response")

    return token_data["access_token"]


def make_step_logs_request(
    server_url: str, step_id: str, access_token: str
) -> Dict[str, Any]:
    """Get logs for a specific step from the ZenML API.

    Args:
        server_url: The base URL of the ZenML server
        step_id: The ID of the step to get logs for
        access_token: The access token for authentication

    Returns:
        The logs data as a dictionary

    Raises:
        requests.HTTPError: If the request fails
    """
    # Ensure the server URL doesn't end with a slash
    server_url = server_url.rstrip("/")

    # Construct the full URL
    url = f"{server_url}/api/v1/steps/{step_id}/logs"

    # Prepare headers with the access token
    headers = {"Authorization": f"Bearer {access_token}"}

    logger.debug(f"Fetching logs for step {step_id}")

    # Make the request
    response = requests.get(url, headers=headers, timeout=(3.05, 30))
    response.raise_for_status()  # Raise an exception for HTTP errors

    return response.json()


@mcp.tool()
@handle_tool_exceptions
def get_step_logs(step_run_id: str) -> str:
    """Get the logs for a specific step run.

    Args:
        step_run_id: The ID of the step run to get logs for
    """
    # Get server URL and API key from environment variables
    server_url = os.environ.get("ZENML_STORE_URL")
    api_key = os.environ.get("ZENML_STORE_API_KEY")

    if not server_url:
        raise ValueError("ZENML_STORE_URL environment variable not set")

    if not api_key:
        raise ValueError("ZENML_STORE_API_KEY environment variable not set")

    # Generate a short-lived access token
    access_token = get_access_token(server_url, api_key)

    # Get the logs using the access token
    logs = make_step_logs_request(server_url, step_run_id, access_token)
    return json.dumps(logs)


@mcp.tool()
@handle_tool_exceptions
def list_users(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    active: bool | None = None,
) -> str:
    """List all users in the ZenML workspace.

    Args:
        sort_by: The field to sort the users by
        page: The page number to return
        size: The number of users to return
        logical_operator: The logical operator to use
        created: The creation date of the users
        updated: The last update date of the users
        active: Whether the user is active
    """
    users = get_zenml_client().list_users(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        active=active,
    )
    return f"""{[user.model_dump_json() for user in users]}"""


@mcp.tool()
@handle_tool_exceptions
def get_user(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific user.

    Args:
        name_id_or_prefix: The name, ID or prefix of the user to retrieve
    """
    user = get_zenml_client().get_user(name_id_or_prefix)
    return user.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def get_active_user() -> str:
    """Get the currently active user."""
    user = get_zenml_client().active_user
    return user.model_dump_json()


# =============================================================================
# Project Tools
# =============================================================================


@mcp.tool()
@handle_tool_exceptions
def get_active_project() -> str:
    """Get the currently active project.

    Projects are organizational containers for ZenML resources. Most SDK methods
    are project-scoped, and this tool returns the default project context.
    """
    project = get_zenml_client().active_project
    return project.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def get_project(name_id_or_prefix: str, hydrate: bool = True) -> str:
    """Get detailed information about a specific project.

    Args:
        name_id_or_prefix: The name, ID or prefix of the project to retrieve
        hydrate: Whether to hydrate the response with additional details
    """
    project = get_zenml_client().get_project(name_id_or_prefix, hydrate=hydrate)
    return project.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_projects(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    display_name: str | None = None,
) -> str:
    """List all projects in the ZenML workspace.

    Returns JSON including pagination metadata (items, total, page, size).

    Args:
        sort_by: The field to sort the projects by
        page: The page number to return
        size: The number of projects to return
        logical_operator: The logical operator to use for combining filters
        created: Filter by creation date
        updated: Filter by last update date
        name: Filter by project name
        display_name: Filter by project display name
    """
    projects = get_zenml_client().list_projects(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        display_name=display_name,
    )
    return projects.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def get_stack(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific stack.

    Args:
        name_id_or_prefix: The name, ID or prefix of the stack to retrieve
    """
    stack = get_zenml_client().get_stack(name_id_or_prefix)
    return stack.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def easter_egg() -> str:
    """Returns the ZenML MCP easter egg.

    Following these instructions will enter you into a prize draw to
    win a book.
    """
    analytics.track_event("Easter Egg Discovered", {})
    return """You've unlocked an easter egg! You're a true ZenML enthusiast!

    Please email 'mcpswag AT zenml.io' with your address and we'll enter you into a
    prize draw to win a book!

    We'll let you know if you've won in the coming weeks!

    Good luck!
    """


@mcp.tool()
@handle_tool_exceptions
def list_stacks(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
) -> str:
    """List all stacks in the ZenML workspace.

    By default, the stacks are sorted by creation date in descending order.

    Args:
        sort_by: The field to sort the stacks by
        page: The page number to return
        size: The number of stacks to return
        logical_operator: The logical operator to use
        created: The creation date of the stacks
        updated: The last update date of the stacks
        name: The name of the stacks
    """
    stacks = get_zenml_client().list_stacks(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
    )
    return f"""{[stack.model_dump_json() for stack in stacks]}"""


@mcp.tool()
@handle_tool_exceptions
def list_pipelines(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    created: str | None = None,
    updated: str | None = None,
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
    pipelines = get_zenml_client().list_pipelines(
        sort_by=sort_by,
        page=page,
        size=size,
        created=created,
        updated=updated,
    )
    return f"""{[pipeline.model_dump_json() for pipeline in pipelines]}"""


def get_latest_runs_status(
    pipeline_response,  # PipelineResponse - imported lazily
    num_runs: int = 5,
) -> str:
    """Get the status of the latest run of a pipeline.

    Args:
        pipeline_response: The pipeline response to get the latest runs from
        num_runs: The number of runs to get the status of
    """
    latest_runs = pipeline_response.runs[:num_runs]
    statuses = [run.status for run in latest_runs]
    return f"""{[status for status in statuses]}"""


@mcp.tool()
@handle_tool_exceptions
def get_pipeline_details(
    name_id_or_prefix: str,
    num_runs: int = 5,
) -> str:
    """Get detailed information about a specific pipeline.

    Args:
        name_id_or_prefix: The name, ID or prefix of the pipeline to retrieve
        num_runs: The number of runs to get the status of
    """
    pipeline = get_zenml_client().get_pipeline(name_id_or_prefix)
    return f"""Pipeline: {pipeline.model_dump_json()}\n\nStatus of latest {num_runs} runs: {get_latest_runs_status(pipeline, num_runs)}"""


@mcp.tool()
@handle_tool_exceptions
def get_service(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific service.

    Args:
        name_id_or_prefix: The name, ID or prefix of the service to retrieve
    """
    service = get_zenml_client().get_service(name_id_or_prefix)
    return service.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_services(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    id: str | None = None,
    created: str | None = None,
    updated: str | None = None,
    running: bool | None = None,
    service_name: str | None = None,
    pipeline_name: str | None = None,
    pipeline_run_id: str | None = None,
    pipeline_step_name: str | None = None,
    model_version_id: str | None = None,
) -> str:
    """List all services in the ZenML workspace.

    Args:
        sort_by: The field to sort the services by
        page: The page number to return
        size: The number of services to return
        logical_operator: The logical operator to use
        id: The ID of the services
        created: The creation date of the services
        updated: The last update date of the services
        running: Whether the service is running
        service_name: The name of the service
        pipeline_name: The name of the pipeline
        pipeline_run_id: The ID of the pipeline run
        pipeline_step_name: The name of the pipeline step
        model_version_id: The ID of the model version
    """
    services = get_zenml_client().list_services(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        id=id,
        created=created,
        updated=updated,
        running=running,
        service_name=service_name,
        pipeline_name=pipeline_name,
        pipeline_run_id=pipeline_run_id,
        pipeline_step_name=pipeline_step_name,
        model_version_id=model_version_id,
    )
    return f"""{[service.model_dump_json() for service in services]}"""


@mcp.tool()
@handle_tool_exceptions
def get_stack_component(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific stack component.

    Args:
        name_id_or_prefix: The name, ID or prefix of the stack component to retrieve
    """
    stack_component = get_zenml_client().get_stack_component(name_id_or_prefix)
    return stack_component.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_stack_components(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    flavor: str | None = None,
    stack_id: str | None = None,
) -> str:
    """List all stack components in the ZenML workspace.

    Args:
        sort_by: The field to sort the stack components by
        page: The page number to return
        size: The number of stack components to return
        logical_operator: The logical operator to use
        created: The creation date of the stack components
        updated: The last update date of the stack components
        name: The name of the stack components
        flavor: The flavor of the stack components
        stack_id: The ID of the stack
    """
    stack_components = get_zenml_client().list_stack_components(
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
    return f"""{[component.model_dump_json() for component in stack_components]}"""


@mcp.tool()
@handle_tool_exceptions
def get_flavor(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific flavor.

    Args:
        name_id_or_prefix: The name, ID or prefix of the flavor to retrieve
    """
    flavor = get_zenml_client().get_flavor(name_id_or_prefix)
    return flavor.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_flavors(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    id: str | None = None,
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    integration: str | None = None,
) -> str:
    """List all flavors in the ZenML workspace.

    Args:
        sort_by: The field to sort the flavors by
        page: The page number to return
        size: The number of flavors to return
        logical_operator: The logical operator to use
        id: The ID of the flavors
        created: The creation date of the flavors
        updated: The last update date of the flavors
    """
    flavors = get_zenml_client().list_flavors(
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
    return f"""{[flavor.model_dump_json() for flavor in flavors]}"""


@mcp.tool()
@handle_tool_exceptions
def trigger_pipeline(
    pipeline_name_or_id: str,
    snapshot_name_or_id: str | None = None,
    stack_name_or_id: str | None = None,
    template_id: str | None = None,
) -> str:
    """Trigger a pipeline to run from the server.

    Args:
        pipeline_name_or_id: The name or ID of the pipeline to trigger
        snapshot_name_or_id: The name or ID of a specific snapshot to run (preferred)
        stack_name_or_id: Optional stack override for the run
        template_id: ⚠️ DEPRECATED - Use `snapshot_name_or_id` instead.
            The ID of a run template to use. Run Templates are deprecated
            and will be removed in a future version.

    Usage examples:
        * Run the latest runnable snapshot for a pipeline:
        ```python
        trigger_pipeline(pipeline_name_or_id=<NAME>)
        ```
        * Run the latest runnable snapshot for a pipeline on a specific stack:
        ```python
        trigger_pipeline(
            pipeline_name_or_id=<NAME>,
            stack_name_or_id=<STACK_NAME_OR_ID>
        )
        ```
        * Run a specific snapshot (RECOMMENDED):
        ```python
        trigger_pipeline(
            pipeline_name_or_id=<NAME>,
            snapshot_name_or_id=<SNAPSHOT_NAME_OR_ID>
        )
        ```
        * Run a specific template (DEPRECATED - use snapshot_name_or_id instead):
        ```python
        trigger_pipeline(pipeline_name_or_id=<NAME>, template_id=<ID>)
        ```
    """
    # Build kwargs for SDK call, preferring snapshot_name_or_id over deprecated template_id
    trigger_kwargs: Dict[str, Any] = {
        "pipeline_name_or_id": pipeline_name_or_id,
        "stack_name_or_id": stack_name_or_id,
    }

    deprecation_warning = ""
    if snapshot_name_or_id is not None:
        trigger_kwargs["snapshot_name_or_id"] = snapshot_name_or_id
    elif template_id is not None:
        # Fall back to template_id for backward compatibility, but warn
        trigger_kwargs["template_id"] = template_id
        deprecation_warning = (
            "⚠️ DEPRECATION WARNING: The `template_id` parameter is deprecated. "
            "Please use `snapshot_name_or_id` instead. Run Templates are being "
            "phased out in favor of Snapshots.\n\n"
        )

    pipeline_run = get_zenml_client().trigger_pipeline(**trigger_kwargs)
    analytics.track_event(
        "Pipeline Triggered",
        {
            "has_snapshot_id": snapshot_name_or_id is not None,
            "has_template_id": template_id is not None,
            "has_stack_override": stack_name_or_id is not None,
            "used_deprecated_template": template_id is not None
            and snapshot_name_or_id is None,
            "success": True,
        },
    )
    return f"""{deprecation_warning}# Pipeline Run Response: {pipeline_run.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_tool_exceptions
def get_run_template(name_id_or_prefix: str) -> str:
    """Get a run template for a pipeline.

    ⚠️ DEPRECATED: Run Templates are deprecated in ZenML. Use `get_snapshot` instead.
    Snapshots are the modern replacement for run templates and provide the same
    functionality with better integration into the ZenML ecosystem.

    Args:
        name_id_or_prefix: The name, ID or prefix of the run template to retrieve
    """
    run_template = get_zenml_client().get_run_template(name_id_or_prefix)
    deprecation_notice = (
        "⚠️ DEPRECATION NOTICE: Run Templates are deprecated in ZenML. "
        "Please use `get_snapshot` instead. Run Templates internally reference "
        "Snapshots via `source_snapshot_id` and will be removed in a future version."
    )
    return f"{deprecation_notice}\n\n{run_template.model_dump_json()}"


@mcp.tool()
@handle_tool_exceptions
def list_run_templates(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    tag: str | None = None,
) -> str:
    """List all run templates in the ZenML workspace.

    ⚠️ DEPRECATED: Run Templates are deprecated in ZenML. Use `list_snapshots` instead.
    Snapshots are the modern replacement for run templates. To find runnable
    snapshots, use `list_snapshots(runnable=True)`.

    Args:
        sort_by: The field to sort the run templates by
        page: The page number to return
        size: The number of run templates to return
        created: The creation date of the run templates
        updated: The last update date of the run templates
        name: The name of the run templates
        tag: The tag of the run templates
    """
    run_templates = get_zenml_client().list_run_templates(
        sort_by=sort_by,
        page=page,
        size=size,
        created=created,
        updated=updated,
        name=name,
        tag=tag,
    )
    deprecation_notice = (
        "⚠️ DEPRECATION NOTICE: Run Templates are deprecated in ZenML. "
        "Please use `list_snapshots` instead. For runnable configurations, "
        "use `list_snapshots(runnable=True)`. Run Templates will be removed in a future version."
    )
    templates_json = [run_template.model_dump_json() for run_template in run_templates]
    return f"{deprecation_notice}\n\n{templates_json}"


# =============================================================================
# Snapshot Tools (Modern replacement for Run Templates)
# =============================================================================


@mcp.tool()
@handle_tool_exceptions
def get_snapshot(
    name_id_or_prefix: str,
    pipeline_name_or_id: str | None = None,
    project: str | None = None,
    include_config_schema: bool | None = None,
    hydrate: bool = True,
) -> str:
    """Get detailed information about a specific snapshot.

    Snapshots are frozen pipeline configurations that link pipeline + stack + build
    + schedule + tags together. They represent "what exactly ran/is deployed" and
    are the modern replacement for Run Templates.

    Args:
        name_id_or_prefix: The name, ID or prefix of the snapshot to retrieve
        pipeline_name_or_id: Optional pipeline context to narrow the search
        project: Optional project scope (defaults to active project)
        include_config_schema: Whether to include the config schema in the response
            (can produce large payloads)
        hydrate: Whether to hydrate the response with additional details
    """
    snapshot = get_zenml_client().get_snapshot(
        name_id_or_prefix,
        pipeline_name_or_id=pipeline_name_or_id,
        project=project,
        include_config_schema=include_config_schema,
        hydrate=hydrate,
    )
    return snapshot.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_snapshots(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    pipeline: str | None = None,
    runnable: bool | None = None,
    deployable: bool | None = None,
    deployed: bool | None = None,
    tag: str | None = None,
    project: str | None = None,
    named_only: bool | None = True,
) -> str:
    """List all snapshots in the ZenML workspace.

    Snapshots are frozen pipeline configurations that replace the deprecated
    Run Templates. Use `runnable=True` to find snapshots that can be triggered.

    Returns JSON including pagination metadata (items, total, page, size).

    Args:
        sort_by: The field to sort the snapshots by
        page: The page number to return
        size: The number of snapshots to return
        logical_operator: The logical operator to use for combining filters
        created: Filter by creation date
        updated: Filter by last update date
        name: Filter by snapshot name
        pipeline: Filter by pipeline name or ID
        runnable: Filter to only runnable snapshots (can be triggered)
        deployable: Filter to only deployable snapshots
        deployed: Filter to only currently deployed snapshots
        tag: Filter by tag
        project: Optional project scope (defaults to active project)
        named_only: Only return named snapshots (default True to avoid internal ones)
    """
    snapshots = get_zenml_client().list_snapshots(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        pipeline=pipeline,
        runnable=runnable,
        deployable=deployable,
        deployed=deployed,
        tag=tag,
        project=project,
        named_only=named_only,
    )
    return snapshots.model_dump_json()


# =============================================================================
# Deployment Tools
# =============================================================================


@mcp.tool()
@handle_tool_exceptions
def get_deployment(
    name_id_or_prefix: str,
    project: str | None = None,
    hydrate: bool = True,
) -> str:
    """Get detailed information about a specific deployment.

    Deployments represent the runtime state of what's currently serving/provisioned,
    including status, URL, and metadata. They tie back to snapshots.

    Args:
        name_id_or_prefix: The name, ID or prefix of the deployment to retrieve
        project: Optional project scope (defaults to active project)
        hydrate: Whether to hydrate the response with additional details
    """
    deployment = get_zenml_client().get_deployment(
        name_id_or_prefix,
        project=project,
        hydrate=hydrate,
    )
    return deployment.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_deployments(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    status: str | None = None,
    url: str | None = None,
    pipeline: str | None = None,
    snapshot_id: str | None = None,
    tag: str | None = None,
    project: str | None = None,
) -> str:
    """List all deployments in the ZenML workspace.

    Deployments show what's currently serving/provisioned with runtime status.

    Returns JSON including pagination metadata (items, total, page, size).

    Args:
        sort_by: The field to sort the deployments by
        page: The page number to return
        size: The number of deployments to return
        logical_operator: The logical operator to use for combining filters
        created: Filter by creation date
        updated: Filter by last update date
        name: Filter by deployment name
        status: Filter by deployment status (e.g., "running", "error")
        url: Filter by deployment URL
        pipeline: Filter by pipeline name or ID
        snapshot_id: Filter by source snapshot ID
        tag: Filter by tag
        project: Optional project scope (defaults to active project)
    """
    deployments = get_zenml_client().list_deployments(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        status=status,
        url=url,
        pipeline=pipeline,
        snapshot_id=snapshot_id,
        tag=tag,
        project=project,
    )
    return deployments.model_dump_json()


# Maximum size for deployment logs output (100KB)
MAX_DEPLOYMENT_LOGS_SIZE = 100 * 1024


@mcp.tool()
@handle_tool_exceptions
def get_deployment_logs(
    name_id_or_prefix: str,
    project: str | None = None,
    tail: int = 100,
) -> str:
    """Get logs for a specific deployment.

    Retrieves logs from the deployment's underlying infrastructure. This is useful
    for debugging deployment issues or monitoring deployment behavior.

    Note: Log availability depends on the deployer plugin being installed and
    the deployment infrastructure supporting log retrieval.

    Args:
        name_id_or_prefix: The name, ID or prefix of the deployment
        project: Optional project scope (defaults to active project)
        tail: Number of recent log lines to retrieve (default: 100, max recommended: 500)

    Returns:
        JSON object with 'logs' (string) and metadata about truncation if applicable
    """
    # Cap tail at a reasonable maximum to prevent excessive output
    effective_tail = min(tail, 1000)

    try:
        # Get the log generator - ALWAYS use follow=False to prevent hanging
        log_generator = get_zenml_client().get_deployment_logs(
            name_id_or_prefix,
            project=project,
            follow=False,  # Critical: Never follow to avoid infinite stream
            tail=effective_tail,
        )

        # Collect logs from generator with size limit
        log_lines = []
        total_size = 0
        truncated = False

        for line in log_generator:
            line_size = len(line.encode("utf-8"))
            if total_size + line_size > MAX_DEPLOYMENT_LOGS_SIZE:
                truncated = True
                break
            log_lines.append(line)
            total_size += line_size

        logs_text = "\n".join(log_lines)

        result = {
            "logs": logs_text,
            "line_count": len(log_lines),
            "truncated": truncated,
            "tail_requested": tail,
            "tail_effective": effective_tail,
        }

        if truncated:
            result["truncation_message"] = (
                f"Output truncated at {MAX_DEPLOYMENT_LOGS_SIZE // 1024}KB. "
                f"Use a smaller 'tail' value to see complete recent logs."
            )

        return json.dumps(result)

    except ImportError as e:
        # Handle missing deployer plugin (direct import failure)
        return json.dumps(
            {
                "error": "deployer_plugin_not_installed",
                "message": (
                    f"The deployer plugin required to fetch logs is not installed: {e}. "
                    "Please install the appropriate ZenML integration for your stack "
                    "(e.g., `zenml integration install gcp` for GCP deployments), "
                    "then restart the MCP server."
                ),
                "logs": None,
            }
        )
    except Exception as e:
        # Check if this is a deployer instantiation error (missing dependencies)
        error_str = str(e)
        if (
            "could not be instantiated" in error_str
            or "dependencies are not installed" in error_str
        ):
            return json.dumps(
                {
                    "error": "deployer_dependencies_missing",
                    "message": (
                        f"The deployer's dependencies are not installed: {error_str}\n\n"
                        "To fix this:\n"
                        "1. Check which stack/deployer was used for this deployment\n"
                        "2. Install the required ZenML integration for that deployer:\n"
                        "   `zenml integration install <integration-name>`\n"
                        "3. Restart the MCP server\n\n"
                        "Common deployer integrations: gcp, aws, azure, kubernetes, huggingface"
                    ),
                    "logs": None,
                }
            )
        # Re-raise other exceptions to be handled by the decorator
        raise


@mcp.tool()
@handle_tool_exceptions
def get_schedule(name_id_or_prefix: str) -> str:
    """Get a schedule for a pipeline.

    Args:
        name_id_or_prefix: The name, ID or prefix of the schedule to retrieve
    """
    schedule = get_zenml_client().get_schedule(name_id_or_prefix)
    return schedule.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_schedules(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    pipeline_id: str | None = None,
    orchestrator_id: str | None = None,
    active: bool | None = None,
) -> str:
    """List all schedules in the ZenML workspace.

    Args:
        sort_by: The field to sort the schedules by
        page: The page number to return
        size: The number of schedules to return
        created: The creation date of the schedules
        updated: The last update date of the schedules
        name: The name of the schedules
        pipeline_id: The ID of the pipeline
        orchestrator_id: The ID of the orchestrator
        active: Whether the schedule is active
    """
    schedules = get_zenml_client().list_schedules(
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
    return f"""{[schedule.model_dump_json() for schedule in schedules]}"""


@mcp.tool()
@handle_tool_exceptions
def get_pipeline_run(name_id_or_prefix: str) -> str:
    """Get a pipeline run by name, ID, or prefix.

    Args:
        name_id_or_prefix: The name, ID or prefix of the pipeline run to retrieve
    """
    pipeline_run = get_zenml_client().get_pipeline_run(name_id_or_prefix)
    return pipeline_run.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_pipeline_runs(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    pipeline_id: str | None = None,
    pipeline_name: str | None = None,
    stack_id: str | None = None,
    status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    stack: str | None = None,
    stack_component: str | None = None,
) -> str:
    """List all pipeline runs in the ZenML workspace.

    Args:
        sort_by: The field to sort the pipeline runs by
        page: The page number to return
        size: The number of pipeline runs to return
        logical_operator: The logical operator to use
        created: The creation date of the pipeline runs
        updated: The last update date of the pipeline runs
        name: The name of the pipeline runs
        pipeline_id: The ID of the pipeline
        pipeline_name: The name of the pipeline
        stack_id: The ID of the stack
        status: The status of the pipeline runs
        start_time: The start time of the pipeline runs
        end_time: The end time of the pipeline runs
        stack: The stack of the pipeline runs
        stack_component: The stack component of the pipeline runs
    """
    pipeline_runs = get_zenml_client().list_pipeline_runs(
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
        stack=stack,
        stack_component=stack_component,
    )
    return f"""{[pipeline_run.model_dump_json() for pipeline_run in pipeline_runs]}"""


@mcp.tool()
@handle_tool_exceptions
def get_run_step(step_run_id: str) -> str:
    """Get a run step by name, ID, or prefix.

    Args:
        step_run_id: The ID of the run step to retrieve
    """
    run_step = get_zenml_client().get_run_step(step_run_id)
    return run_step.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_run_steps(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    pipeline_run_id: str | None = None,
) -> str:
    """List all run steps in the ZenML workspace.

    Args:
        sort_by: The field to sort the run steps by
        page: The page number to return
        size: The number of run steps to return
        logical_operator: The logical operator to use
        created: The creation date of the run steps
        updated: The last update date of the run steps
        name: The name of the run steps
        status: The status of the run steps
        start_time: The start time of the run steps
        end_time: The end time of the run steps
        pipeline_run_id: The ID of the pipeline run
    """
    run_steps = get_zenml_client().list_run_steps(
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
    return f"""{[run_step.model_dump_json() for run_step in run_steps]}"""


@mcp.tool()
@handle_tool_exceptions
def list_artifacts(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    tag: str | None = None,
) -> str:
    """List all artifacts in the ZenML workspace.

    Args:
        sort_by: The field to sort the artifacts by
        page: The page number to return
        size: The number of artifacts to return
        logical_operator: The logical operator to use
        created: The creation date of the artifacts
        updated: The last update date of the artifacts
        name: The name of the artifacts
    """
    artifacts = get_zenml_client().list_artifacts(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        tag=tag,
    )
    return f"""{[artifact.model_dump_json() for artifact in artifacts]}"""


@mcp.tool()
@handle_tool_exceptions
def get_artifact_version(
    name_id_or_prefix: str,
    version: str | None = None,
) -> str:
    """Get detailed information about a specific artifact version.

    Args:
        name_id_or_prefix: The name, ID or prefix of the artifact
        version: Optional specific version (defaults to latest)
    """
    artifact = get_zenml_client().get_artifact_version(
        name_id_or_prefix=name_id_or_prefix,
        version=version,
    )

    return artifact.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_artifact_versions(
    artifact_name_or_id: str,
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    tag: str | None = None,
) -> str:
    """List all versions of a specific artifact.

    Args:
        artifact_name_or_id: The name or ID of the artifact
        sort_by: The field to sort the versions by
        page: The page number to return
        size: The number of versions to return
        logical_operator: The logical operator to use
        created: The creation date filter
        updated: The last update date filter
        tag: The tag filter
    """

    versions = get_zenml_client().list_artifact_versions(
        artifact=artifact_name_or_id,
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        tag=tag,
    )
    return f"""{[version.model_dump_json() for version in versions]}"""


@mcp.tool()
@handle_tool_exceptions
def list_secrets(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
) -> str:
    """List all secrets in the ZenML workspace.

    Args:
        sort_by: The field to sort the secrets by
        page: The page number to return
        size: The number of secrets to return
        logical_operator: The logical operator to use
        created: The creation date of the secrets
        updated: The last update date of the secrets
        name: The name of the secrets
    """
    secrets = get_zenml_client().list_secrets(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
    )
    return f"""{[secret.model_dump_json() for secret in secrets]}"""


@mcp.tool()
@handle_tool_exceptions
def get_service_connector(name_id_or_prefix: str) -> str:
    """Get a service connector by name, ID, or prefix.

    Args:
        name_id_or_prefix: The name, ID or prefix of the service connector to retrieve
    """
    service_connector = get_zenml_client().get_service_connector(name_id_or_prefix)
    return service_connector.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_service_connectors(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    connector_type: str | None = None,
) -> str:
    """List all service connectors in the ZenML workspace.

    Args:
        sort_by: The field to sort the service connectors by
        page: The page number to return
        size: The number of service connectors to return
        logical_operator: The logical operator to use
        created: The creation date of the service connectors
        updated: The last update date of the service connectors
        name: The name of the service connectors
        connector_type: The type of the service connectors
    """
    service_connectors = get_zenml_client().list_service_connectors(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        connector_type=connector_type,
    )
    return f"""{[service_connector.model_dump_json() for service_connector in service_connectors]}"""


@mcp.tool()
@handle_tool_exceptions
def get_model(name_id_or_prefix: str) -> str:
    """Get a model by name, ID, or prefix.

    Args:
        name_id_or_prefix: The name, ID or prefix of the model to retrieve
    """
    model = get_zenml_client().get_model(name_id_or_prefix)
    return model.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_models(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    tag: str | None = None,
) -> str:
    """List all models in the ZenML workspace.

    Args:
        sort_by: The field to sort the models by
        page: The page number to return
        size: The number of models to return
        logical_operator: The logical operator to use
        created: The creation date of the models
        updated: The last update date of the models
        name: The name of the models
        tag: The tag of the models
    """
    models = get_zenml_client().list_models(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        tag=tag,
    )
    return f"""{[model.model_dump_json() for model in models]}"""


@mcp.tool()
@handle_tool_exceptions
def get_model_version(
    model_name_or_id: str,
    model_version_name_or_number_or_id: str,
) -> str:
    """Get a model version by name, ID, or prefix.

    Args:
        model_name_or_id: The name, ID or prefix of the model to retrieve
        model_version_name_or_number_or_id: The name, ID or prefix of the model version to retrieve
    """
    model_version = get_zenml_client().get_model_version(
        model_name_or_id,
        model_version_name_or_number_or_id,
    )
    return model_version.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_model_versions(
    model_name_or_id: str,
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    number: int | None = None,
    stage: str | None = None,
    tag: str | None = None,
) -> str:
    """List all model versions for a model.

    Args:
        model_name_or_id: The name, ID or prefix of the model to retrieve
        sort_by: The field to sort the model versions by
        page: The page number to return
        size: The number of model versions to return
        logical_operator: The logical operator to use
        created: The creation date of the model versions
        updated: The last update date of the model versions
        name: The name of the model versions
        number: The number of the model versions
        stage: The stage of the model versions
        tag: The tag of the model versions
    """
    model_versions = get_zenml_client().list_model_versions(
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
    return (
        f"""{[model_version.model_dump_json() for model_version in model_versions]}"""
    )


@mcp.tool()
@handle_tool_exceptions
def get_step_code(
    step_run_id: str,
) -> str:
    """Get the code for a step.

    Args:
        step_run_id: The ID of the step to retrieve
    """
    step_code = get_zenml_client().get_run_step(step_run_id).source_code
    return f"""{step_code}"""


# =============================================================================
# Tag Tools
# =============================================================================


@mcp.tool()
@handle_tool_exceptions
def get_tag(tag_name_or_id: str, hydrate: bool = True) -> str:
    """Get detailed information about a specific tag.

    Tags are cross-cutting metadata labels for discovery (prod, staging, latest,
    candidate, etc.). Many ZenML entities can be tagged.

    Args:
        tag_name_or_id: The name or ID of the tag to retrieve
        hydrate: Whether to hydrate the response with additional details
    """
    tag = get_zenml_client().get_tag(tag_name_or_id, hydrate=hydrate)
    return tag.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_tags(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    exclusive: bool | None = None,
    resource_type: str | None = None,
) -> str:
    """List all tags in the ZenML workspace.

    Tags enable queries like "show me all prod deployments" and help organize
    resources. Exclusive tags can only be applied once per entity.

    Returns JSON including pagination metadata (items, total, page, size).

    Args:
        sort_by: The field to sort the tags by
        page: The page number to return
        size: The number of tags to return
        logical_operator: The logical operator to use for combining filters
        created: Filter by creation date
        updated: Filter by last update date
        name: Filter by tag name
        exclusive: Filter by exclusive tags (can only be applied once per entity)
        resource_type: Filter by resource type the tag applies to
    """
    tags = get_zenml_client().list_tags(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        exclusive=exclusive,
        resource_type=resource_type,
    )
    return tags.model_dump_json()


# =============================================================================
# Build Tools
# =============================================================================


@mcp.tool()
@handle_tool_exceptions
def get_build(
    id_or_prefix: str,
    project: str | None = None,
    hydrate: bool = True,
) -> str:
    """Get detailed information about a specific pipeline build.

    Builds contain image info, code embedding, and stack checksums that explain
    reproducibility and infrastructure setup for pipeline runs.

    Args:
        id_or_prefix: The ID or prefix of the build to retrieve
        project: Optional project scope (defaults to active project)
        hydrate: Whether to hydrate the response with additional details
    """
    build = get_zenml_client().get_build(
        id_or_prefix,
        project=project,
        hydrate=hydrate,
    )
    return build.model_dump_json()


@mcp.tool()
@handle_tool_exceptions
def list_builds(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    pipeline_id: str | None = None,
    stack_id: str | None = None,
    is_local: bool | None = None,
    contains_code: bool | None = None,
    project: str | None = None,
) -> str:
    """List all pipeline builds in the ZenML workspace.

    Builds explain reproducibility (container image/code) and can help debug
    infrastructure issues.

    Returns JSON including pagination metadata (items, total, page, size).

    Args:
        sort_by: The field to sort the builds by
        page: The page number to return
        size: The number of builds to return
        logical_operator: The logical operator to use for combining filters
        created: Filter by creation date
        updated: Filter by last update date
        pipeline_id: Filter by pipeline ID
        stack_id: Filter by stack ID
        is_local: Filter by local builds (not runnable from server)
        contains_code: Filter by builds that contain embedded code
        project: Optional project scope (defaults to active project)
    """
    builds = get_zenml_client().list_builds(
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        pipeline_id=pipeline_id,
        stack_id=stack_id,
        is_local=is_local,
        contains_code=contains_code,
        project=project,
    )
    return builds.model_dump_json()


@mcp.prompt()
@handle_exceptions
def stack_components_analysis() -> str:
    """Analyze the stacks in the ZenML workspace."""
    return (
        "Please generate a comprehensive report or dashboard on our ZenML stack components, "
        "showing which ones are most frequently used across our pipelines. "
        "Include information about version compatibility issues and performance variations."
    )


@mcp.prompt()
@handle_exceptions
def recent_runs_analysis() -> str:
    """Analyze the recent runs in the ZenML workspace."""
    return (
        "Please generate a comprehensive report or dashboard on our recent runs, "
        "showing which pipelines are most frequently run and which ones are most frequently failed."
        " Include information about the status of the runs, the duration, and the stack components used."
    )


@mcp.resource(uri="resource://zenml_server/most_recent_runs?run_count={run_count}")
@handle_exceptions
def most_recent_runs(run_count: int = 10) -> str:
    """Returns the ten most recent runs in the ZenML workspace.

    Args:
        run_count: The number of runs to return
    """
    return (
        get_zenml_client()
        .list_pipeline_runs(
            sort_by="desc:created",
            page=1,
            size=run_count,
        )
        .model_dump_json()
    )


if __name__ == "__main__":
    try:
        analytics.init_analytics()
        analytics.track_server_started()
        mcp.run(transport="stdio")
    except Exception as e:
        logger.error(f"Error running MCP server: {e}")
