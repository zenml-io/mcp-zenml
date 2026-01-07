"""
Analytics module for ZenML MCP Server.

Provides anonymous usage tracking via Segment to help improve the product.
All analytics calls are non-blocking and exception-safe - they will never
affect MCP server functionality.
"""

import atexit
import hashlib
import json
import os
import platform
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# =============================================================================
# Configuration
# =============================================================================

# Check opt-out environment variables
_enabled_env = os.getenv("ZENML_MCP_ANALYTICS_ENABLED", "true").lower()
_disable_env = os.getenv("ZENML_MCP_DISABLE_ANALYTICS", "").lower()

ANALYTICS_ENABLED = _enabled_env in ("true", "1", "yes") and _disable_env not in (
    "true",
    "1",
    "yes",
)

DEV_MODE = os.getenv("ZENML_MCP_ANALYTICS_DEV", "").lower() in ("true", "1", "yes")

# Segment write key - hardcoded default with env override
SEGMENT_WRITE_KEY = os.getenv(
    "ZENML_MCP_SEGMENT_WRITE_KEY", "iZAi5eWiCRTua7016fA1W9hu1b82bj24"
)

# =============================================================================
# Tool allowlist - only track actual MCP tools, not prompts/resources
# =============================================================================

TRACKED_TOOL_NAMES: set[str] = {
    # User tools
    "list_users",
    "get_user",
    "get_active_user",
    # Stack tools
    "list_stacks",
    "get_stack",
    # Pipeline tools
    "list_pipelines",
    "get_pipeline_details",
    "trigger_pipeline",
    # Run tools
    "list_pipeline_runs",
    "get_pipeline_run",
    "list_run_steps",
    "get_run_step",
    # Step tools
    "get_step_logs",
    "get_step_code",
    # Service tools
    "list_services",
    "get_service",
    # Component tools
    "list_stack_components",
    "get_stack_component",
    # Flavor tools
    "list_flavors",
    "get_flavor",
    # Template tools
    "list_run_templates",
    "get_run_template",
    # Schedule tools
    "list_schedules",
    "get_schedule",
    # Artifact tools
    "list_artifacts",
    # Secret tools
    "list_secrets",
    # Connector tools
    "list_service_connectors",
    "get_service_connector",
    # Model tools
    "list_models",
    "get_model",
    "list_model_versions",
    "get_model_version",
    # Easter egg
    "easter_egg",
}

# =============================================================================
# Module state
# =============================================================================

_analytics: Any = None
_session_id: str | None = None
_session_start_time: float | None = None
_tool_call_count = 0
_tools_used: set[str] = set()
_user_id: str | None = None
_init_attempted = False
_init_failed = False


# =============================================================================
# Helper functions
# =============================================================================


def get_or_create_user_id() -> str:
    """Get or create a stable anonymous user ID.

    Priority:
    1. Environment variable ZENML_MCP_ANALYTICS_ID (useful for Docker)
    2. Persistent file in config directory
    3. Fallback to session-only UUID if file storage fails
    """
    global _user_id
    if _user_id:
        return _user_id

    # Check environment variable first (for Docker consistency)
    env_id = os.getenv("ZENML_MCP_ANALYTICS_ID")
    if env_id:
        _user_id = env_id
        return _user_id

    # Determine config directory based on platform
    if platform.system() == "Windows":
        config_dir = Path(os.getenv("APPDATA", "")) / "zenml-mcp"
    else:
        config_dir = Path.home() / ".config" / "zenml-mcp"

    id_file = config_dir / "anonymous_id"

    try:
        if id_file.exists():
            _user_id = id_file.read_text().strip()
            if _user_id:  # Ensure file wasn't empty
                return _user_id

        # Generate new ID
        _user_id = str(uuid.uuid4())
        config_dir.mkdir(parents=True, exist_ok=True)
        id_file.write_text(_user_id)
        return _user_id
    except (OSError, PermissionError):
        # Fallback: session-only ID
        _user_id = str(uuid.uuid4())
        return _user_id


def get_server_domain_hash() -> str:
    """Hash the ZenML server domain for anonymous tracking.

    Only hashes the domain portion of the URL, not the full path.
    Returns first 16 characters of SHA256 hash.
    """
    url = os.getenv("ZENML_STORE_URL", "")
    if url:
        try:
            from urllib.parse import urlparse

            domain = urlparse(url).netloc
            if domain:
                return hashlib.sha256(domain.encode()).hexdigest()[:16]
        except Exception:
            pass
    return "unknown"


def get_server_version() -> str:
    """Get the server version from VERSION file."""
    try:
        version_file = Path(__file__).parent.parent / "VERSION"
        if version_file.exists():
            return version_file.read_text().strip()
    except Exception:
        pass
    return "unknown"


def is_running_in_docker() -> bool:
    """Check if running inside a Docker container."""
    # Check for .dockerenv file
    if os.path.exists("/.dockerenv"):
        return True
    # Check cgroup for docker
    try:
        with open("/proc/1/cgroup", "r") as f:
            return "docker" in f.read()
    except (FileNotFoundError, PermissionError):
        return False


def is_ci_environment() -> bool:
    """Check if running in a CI/CD environment."""
    ci_env_vars = [
        "CI",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "CIRCLECI",
        "TRAVIS",
        "JENKINS_URL",
        "BUILDKITE",
        "AZURE_PIPELINES",
    ]
    return any(os.getenv(var) for var in ci_env_vars)


def should_track_function(func_name: str) -> bool:
    """Check if a function should be tracked (is it an MCP tool?)."""
    return func_name in TRACKED_TOOL_NAMES


# =============================================================================
# Traits management
# =============================================================================


def _get_traits() -> dict[str, Any]:
    """Get current user traits for identify call."""
    return {
        "server_version": get_server_version(),
        "python_version": platform.python_version(),
        "os": platform.system(),
        "is_docker": is_running_in_docker(),
        "zenml_server_domain_hash": get_server_domain_hash(),
    }


def _get_traits_hash() -> str:
    """Get hash of current traits to detect changes."""
    traits = _get_traits()
    return hashlib.sha256(json.dumps(traits, sort_keys=True).encode()).hexdigest()[:16]


def _should_identify() -> bool:
    """Check if we should send identify call (traits changed since last time)."""
    if platform.system() == "Windows":
        config_dir = Path(os.getenv("APPDATA", "")) / "zenml-mcp"
    else:
        config_dir = Path.home() / ".config" / "zenml-mcp"

    hash_file = config_dir / "traits_hash"
    current_hash = _get_traits_hash()

    try:
        if hash_file.exists():
            stored_hash = hash_file.read_text().strip()
            if stored_hash == current_hash:
                return False

        # Store new hash
        config_dir.mkdir(parents=True, exist_ok=True)
        hash_file.write_text(current_hash)
        return True
    except (OSError, PermissionError):
        return True  # Identify if we can't track


# =============================================================================
# Initialization
# =============================================================================


def _do_init_analytics() -> None:
    """Actually initialize the Segment analytics client."""
    global _analytics, _session_id, _session_start_time

    # Session tracking (always set, even in dev mode)
    _session_id = str(uuid.uuid4())
    _session_start_time = time.time()

    if DEV_MODE:
        print("Analytics: dev mode (events logged, not sent)", file=sys.stderr)
        return

    # Import Segment SDK
    import segment.analytics as seg_analytics

    _analytics = seg_analytics
    _analytics.write_key = SEGMENT_WRITE_KEY
    _analytics.send = True
    _analytics.debug = os.getenv("LOGLEVEL", "").upper() == "DEBUG"
    _analytics.on_error = lambda error, items: None  # Silent errors

    # Register shutdown handlers
    atexit.register(_on_shutdown)

    def signal_handler(sig: int, frame: Any) -> None:
        _on_shutdown()
        sys.exit(0)

    # Register signal handlers (best effort)
    try:
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    except (ValueError, OSError):
        # Signal handling may not work in all contexts (e.g., non-main thread)
        pass

    # Identify user if traits changed
    user_id = get_or_create_user_id()
    if _should_identify():
        try:
            _analytics.identify(user_id, _get_traits())
        except Exception:
            pass  # Never let analytics affect the server


def init_analytics() -> None:
    """Initialize Segment analytics.

    Prints status to stderr and sets up session tracking.
    Safe to call multiple times - will only initialize once.
    """
    global _init_attempted, _init_failed

    if _init_attempted:
        return

    _init_attempted = True

    if not ANALYTICS_ENABLED:
        print(
            "Analytics: disabled (ZENML_MCP_ANALYTICS_ENABLED=false)", file=sys.stderr
        )
        return

    try:
        _do_init_analytics()
        if not DEV_MODE:
            print("Analytics: enabled", file=sys.stderr)
    except ImportError as e:
        print(f"Analytics: disabled (import error: {e})", file=sys.stderr)
        _init_failed = True
    except Exception as e:
        print(f"Analytics: disabled (init error: {e})", file=sys.stderr)
        _init_failed = True


def _ensure_initialized() -> bool:
    """Ensure analytics is initialized, with lazy retry support.

    Returns True if analytics is ready to use.
    """
    global _init_attempted, _init_failed

    if not ANALYTICS_ENABLED:
        return False
    if _init_failed:
        return False
    if _analytics is not None or DEV_MODE:
        return True

    # Retry init if not attempted
    if not _init_attempted:
        init_analytics()

    return (_analytics is not None or DEV_MODE) and not _init_failed


def is_analytics_enabled() -> bool:
    """Check if analytics is currently enabled and initialized."""
    return ANALYTICS_ENABLED and not _init_failed


# =============================================================================
# Event tracking
# =============================================================================


def track_event(event_name: str, properties: dict[str, Any] | None = None) -> None:
    """Track an analytics event.

    Args:
        event_name: Name of the event (e.g., "Tool Called", "MCP Server Started")
        properties: Optional dictionary of event properties

    This function is safe to call at any time - it will never raise exceptions
    or affect server functionality.
    """
    if not _ensure_initialized():
        return

    props = properties.copy() if properties else {}
    props["session_id"] = _session_id
    props["is_ci"] = is_ci_environment()

    user_id = get_or_create_user_id()

    if DEV_MODE:
        print(f"[Analytics DEV] {event_name}: {props}", file=sys.stderr)
        return

    try:
        _analytics.track(user_id, event_name, props)
    except Exception:
        pass  # Never let analytics affect the server


def track_tool_call(
    tool_name: str,
    success: bool,
    duration_ms: int,
    error_type: str | None = None,
    size: int | None = None,
) -> None:
    """Track a tool call with session stats.

    Args:
        tool_name: Name of the tool that was called
        success: Whether the call succeeded
        duration_ms: Duration of the call in milliseconds
        error_type: Type of error if failed (e.g., "HTTPError")
        size: Size parameter if this was a list operation
    """
    global _tool_call_count
    _tool_call_count += 1
    _tools_used.add(tool_name)

    properties: dict[str, Any] = {
        "tool_name": tool_name,
        "success": success,
        "duration_ms": duration_ms,
    }
    if error_type:
        properties["error_type"] = error_type
    if size is not None:
        properties["size"] = size

    track_event("Tool Called", properties)


def track_server_started() -> None:
    """Track server startup event with environment information."""
    track_event(
        "MCP Server Started",
        {
            "server_version": get_server_version(),
            "python_version": platform.python_version(),
            "os": platform.system(),
            "is_docker": is_running_in_docker(),
        },
    )


def _on_shutdown() -> None:
    """Handle server shutdown - send summary event and flush.

    Called via atexit or signal handlers.
    """
    if not _ensure_initialized():
        return

    if _session_start_time:
        uptime = int(time.time() - _session_start_time)
        track_event(
            "MCP Server Shutdown",
            {
                "uptime_seconds": uptime,
                "total_tool_calls": _tool_call_count,
                "unique_tools_used": len(_tools_used),
            },
        )

    # Flush any pending events
    if _analytics and not DEV_MODE:
        try:
            _analytics.flush()
        except Exception:
            pass  # Best effort


# =============================================================================
# Utility for extracting size argument from tool calls
# =============================================================================


def extract_size_from_call(
    func_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> int | None:
    """Extract the 'size' argument from a tool call if present.

    Args:
        func_name: Name of the function (unused but available for future logic)
        args: Positional arguments to the function
        kwargs: Keyword arguments to the function

    Returns:
        The size value if found in kwargs, otherwise None
    """
    # For simplicity, we only check kwargs since most callers use keyword args
    # The MCP protocol typically passes arguments as kwargs
    size = kwargs.get("size")
    if isinstance(size, int):
        return size
    return None
