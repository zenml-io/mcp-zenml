# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "mcp[cli]",
#     "zenml",
#     "setuptools",
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
from typing import Any, Callable, Dict, TypeVar, cast

import requests

logger = logging.getLogger(__name__)

# Configure minimal logging to stderr
log_level_name = os.environ.get("LOGLEVEL", "WARNING").upper()
log_level = getattr(logging, log_level_name, logging.WARNING)

# Simple stderr logging configuration - explicitly use stderr to avoid JSON protocol issues
logging.basicConfig(
    level=log_level,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,
)

# Suppress all INFO level logging from all modules to prevent JSON protocol interference
logging.getLogger().setLevel(logging.WARNING)

# Specifically suppress ZenML's internal logging to prevent JSON protocol issues
logging.getLogger("zenml").setLevel(logging.WARNING)
logging.getLogger("zenml.client").setLevel(logging.WARNING)

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
    logger.info("Importing MCP dependencies...")
    from mcp.server.fastmcp import FastMCP

    # Initialize FastMCP server
    logger.info("Initializing FastMCP server...")
    mcp = FastMCP(name="zenml", instructions=INSTRUCTIONS)
    logger.info("FastMCP server initialized successfully")

    # ZenML client will be initialized lazily
    zenml_client = None

except Exception as e:
    logger.error(f"Error during initialization: {str(e)}")
    raise


def get_zenml_client():
    """Get or initialize the ZenML client lazily."""
    global zenml_client
    if zenml_client is None:
        logger.info("Lazy importing ZenML...")
        from zenml.client import Client

        logger.info("Initializing ZenML client...")
        try:
            zenml_client = Client()
            logger.info("ZenML client initialized successfully")
        except Exception as e:
            logger.error(f"ZenML client initialization failed: {e}")
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

    logger.info("Generating access token")

    # Make the request to get an access token
    response = requests.post(
        url,
        data={"password": api_key},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
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

    logger.info(f"Fetching logs for step {step_id}")

    # Make the request
    response = requests.get(url, headers=headers)
    response.raise_for_status()  # Raise an exception for HTTP errors

    return response.json()


@mcp.tool()
@handle_exceptions
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

    try:
        # Generate a short-lived access token
        access_token = get_access_token(server_url, api_key)

        # Get the logs using the access token
        logs = make_step_logs_request(server_url, step_run_id, access_token)
        return json.dumps(logs)

    except requests.HTTPError as e:
        if e.response.status_code == 401:
            return "Authentication failed. Please check your API key."
        elif e.response.status_code == 404:
            return (
                "Logs not found. Please check the step ID. "
                "Also note that if the step was run on a stack with a local "
                "or non-cloud-based artifact store then no logs will have been "
                "stored by ZenML."
            )
        else:
            return f"Failed to fetch logs: {e}"
    except ValueError as e:
        return f"Value error: {e}"
    except Exception as e:
        return f"An error occurred: {e}"


@mcp.tool()
@handle_exceptions
def list_users(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 10,
    logical_operator: str = "and",
    created: str = None,
    updated: str = None,
    active: bool = None,
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
@handle_exceptions
def get_user(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific user.

    Args:
        name_id_or_prefix: The name, ID or prefix of the user to retrieve
    """
    user = get_zenml_client().get_user(name_id_or_prefix)
    return user.model_dump_json()


@mcp.tool()
@handle_exceptions
def get_active_user() -> str:
    """Get the currently active user."""
    user = get_zenml_client().active_user
    return user.model_dump_json()


@mcp.tool()
@handle_exceptions
def get_stack(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific stack.

    Args:
        name_id_or_prefix: The name, ID or prefix of the stack to retrieve
    """
    stack = get_zenml_client().get_stack(name_id_or_prefix)
    return stack.model_dump_json()


@mcp.tool()
@handle_exceptions
def easter_egg() -> str:
    """Returns the ZenML MCP easter egg.

    Following these instructions will enter you into a prize draw to
    win a book.
    """
    return """You've unlocked an easter egg! You're a true ZenML enthusiast!

    Please email 'mcpswag AT zenml.io' with your address and we'll enter you into a
    prize draw to win a book!

    We'll let you know if you've won in the coming weeks!

    Good luck!
    """


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
@handle_exceptions
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
@handle_exceptions
def get_service(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific service.

    Args:
        name_id_or_prefix: The name, ID or prefix of the service to retrieve
    """
    service = get_zenml_client().get_service(name_id_or_prefix)
    return service.model_dump_json()


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
    running: bool = None,
    service_name: str = None,
    pipeline_name: str = None,
    pipeline_run_id: str = None,
    pipeline_step_name: str = None,
    model_version_id: str = None,
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
@handle_exceptions
def get_stack_component(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific stack component.

    Args:
        name_id_or_prefix: The name, ID or prefix of the stack component to retrieve
    """
    stack_component = get_zenml_client().get_stack_component(name_id_or_prefix)
    return stack_component.model_dump_json()


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
@handle_exceptions
def get_flavor(name_id_or_prefix: str) -> str:
    """Get detailed information about a specific flavor.

    Args:
        name_id_or_prefix: The name, ID or prefix of the flavor to retrieve
    """
    flavor = get_zenml_client().get_flavor(name_id_or_prefix)
    return flavor.model_dump_json()


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
    pipeline_run = get_zenml_client().trigger_pipeline(
        pipeline_name_or_id=pipeline_name_or_id,
        template_id=template_id,
        stack_name_or_id=stack_name_or_id,
    )
    return f"""# Pipeline Run Response: {pipeline_run.model_dump_json(indent=2)}"""


@mcp.tool()
@handle_exceptions
def get_run_template(name_id_or_prefix: str) -> str:
    """Get a run template for a pipeline.

    Args:
        name_id_or_prefix: The name, ID or prefix of the run template to retrieve
    """
    run_template = get_zenml_client().get_run_template(name_id_or_prefix)
    return run_template.model_dump_json()


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
    """List all run templates in the ZenML workspace.

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
    return f"""{[run_template.model_dump_json() for run_template in run_templates]}"""


@mcp.tool()
@handle_exceptions
def get_schedule(name_id_or_prefix: str) -> str:
    """Get a schedule for a pipeline.

    Args:
        name_id_or_prefix: The name, ID or prefix of the schedule to retrieve
    """
    schedule = get_zenml_client().get_schedule(name_id_or_prefix)
    return schedule.model_dump_json()


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
@handle_exceptions
def get_pipeline_run(name_id_or_prefix: str) -> str:
    """Get a pipeline run by name, ID, or prefix.

    Args:
        name_id_or_prefix: The name, ID or prefix of the pipeline run to retrieve
    """
    pipeline_run = get_zenml_client().get_pipeline_run(name_id_or_prefix)
    return pipeline_run.model_dump_json()


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
    stack: str = None,
    stack_component: str = None,
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
@handle_exceptions
def get_run_step(step_run_id: str) -> str:
    """Get a run step by name, ID, or prefix.

    Args:
        step_run_id: The ID of the run step to retrieve
    """
    run_step = get_zenml_client().get_run_step(step_run_id)
    return run_step.model_dump_json()


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
@handle_exceptions
def get_service_connector(name_id_or_prefix: str) -> str:
    """Get a service connector by name, ID, or prefix.

    Args:
        name_id_or_prefix: The name, ID or prefix of the service connector to retrieve
    """
    service_connector = get_zenml_client().get_service_connector(name_id_or_prefix)
    return service_connector.model_dump_json()


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
@handle_exceptions
def get_model(name_id_or_prefix: str) -> str:
    """Get a model by name, ID, or prefix.

    Args:
        name_id_or_prefix: The name, ID or prefix of the model to retrieve
    """
    model = get_zenml_client().get_model(name_id_or_prefix)
    return model.model_dump_json()


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
@handle_exceptions
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
@handle_exceptions
def get_step_code(
    step_run_id: str,
) -> str:
    """Get the code for a step.

    Args:
        step_run_id: The ID of the step to retrieve
    """
    step_code = get_zenml_client().get_run_step(step_run_id).source_code
    return f"""{step_code}"""


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
        "Include information about the status of the runs, the duration, and the stack components used."
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
        mcp.run(transport="stdio")
    except Exception as e:
        logger.error(f"Error running MCP server: {e}")
