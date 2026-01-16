"""
Analytics module for ZenML MCP Server.

Provides anonymous usage tracking via the ZenML Analytics Server to help improve
the product. All analytics calls are best-effort and exception-safe - they will
never affect MCP server functionality.
"""

import atexit
import hashlib
import json
import logging
import os
import platform
import signal
import sys
import time
import uuid
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Any

# =============================================================================
# Configuration
# =============================================================================

logging.getLogger("httpx").setLevel(logging.WARNING)

# Check opt-out environment variables
_enabled_env = os.getenv("ZENML_MCP_ANALYTICS_ENABLED", "true").lower()
_disable_env = os.getenv("ZENML_MCP_DISABLE_ANALYTICS", "").lower()

# Track why analytics was disabled (for accurate status message)
_disabled_reason: str | None = None
if _disable_env in ("true", "1", "yes"):
    _disabled_reason = f"ZENML_MCP_DISABLE_ANALYTICS={_disable_env}"
elif _enabled_env not in ("true", "1", "yes"):
    _disabled_reason = f"ZENML_MCP_ANALYTICS_ENABLED={_enabled_env}"

ANALYTICS_ENABLED = _disabled_reason is None

DEV_MODE = os.getenv("ZENML_MCP_ANALYTICS_DEV", "").lower() in ("true", "1", "yes")

# Analytics server endpoint (hardcoded; no env var override)
ANALYTICS_ENDPOINT = "https://analytics.zenml.io/batch"
ANALYTICS_SOURCE_CONTEXT = "mcp-zenml"
ANALYTICS_TIMEOUT_S = float(os.getenv("ZENML_MCP_ANALYTICS_TIMEOUT_S", "2.0"))

# Debug flag for events (routes to dev vs prod Segment on server side)
ANALYTICS_DEBUG = os.getenv("LOGLEVEL", "").upper() == "DEBUG" or DEV_MODE

# Fire-and-forget queue config
ANALYTICS_QUEUE_MAXSIZE = 100

# =============================================================================
# Module state
# =============================================================================

_session_id: str | None = None
_session_start_time: float | None = None
_tool_call_count = 0
_tools_used: set[str] = set()
_user_id: str | None = None
_init_attempted = False
_init_failed = False
_shutdown_registered = False
_http_client: Any = None

_event_queue: Queue[list[dict[str, Any]] | None] = Queue(
    maxsize=ANALYTICS_QUEUE_MAXSIZE
)
_sender_thread: Thread | None = None
_sender_stop_event = Event()
_sender_start_lock = Lock()

_tool_stats_lock = Lock()
_shutdown_lock = Lock()
_shutdown_once = Event()


# =============================================================================
# Helper functions
# =============================================================================


def _get_config_dir() -> Path:
    if platform.system() == "Windows":
        appdata = os.getenv("APPDATA")
        base_dir = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
        return base_dir / "zenml-mcp"

    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "zenml-mcp"

    xdg_config_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "zenml-mcp"

    return Path.home() / ".config" / "zenml-mcp"


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

    config_dir = _get_config_dir()

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


def is_test_run_environment() -> bool:
    """Check if this is a test run (events should be filterable in Segment).

    Set ZENML_MCP_ANALYTICS_TEST_RUN=true in CI to mark module-emitted
    events as test events. This allows filtering them out in analytics.
    """
    value = os.getenv("ZENML_MCP_ANALYTICS_TEST_RUN", "").lower()
    return value in ("true", "1", "yes")


def _close_http_client() -> None:
    global _http_client
    if _http_client is None:
        return
    try:
        _http_client.close()
    except Exception:
        pass
    finally:
        _http_client = None


def _send_events_sync(events: list[dict[str, Any]]) -> None:
    """Send events to the analytics server synchronously (best-effort).

    This function never raises - all errors are silently ignored
    to ensure analytics never affects server functionality.
    """
    global _http_client

    if not events:
        return

    try:
        import httpx

        if _http_client is None:
            _http_client = httpx.Client(timeout=ANALYTICS_TIMEOUT_S)

        _http_client.post(
            ANALYTICS_ENDPOINT,
            json=events,
            headers={
                "Content-Type": "application/json",
                "Source-Context": ANALYTICS_SOURCE_CONTEXT,
            },
        )
    except Exception:
        pass  # Best effort - never affect the server


def _sender_worker() -> None:
    while not _sender_stop_event.is_set():
        try:
            batch = _event_queue.get(timeout=0.5)
        except Empty:
            continue

        if batch is None:
            break

        _send_events_sync(batch)

    _close_http_client()


def _ensure_sender_thread_started() -> None:
    global _sender_thread

    if _sender_thread is not None and _sender_thread.is_alive():
        return

    with _sender_start_lock:
        if _sender_thread is not None and _sender_thread.is_alive():
            return

        _sender_stop_event.clear()
        _sender_thread = Thread(
            target=_sender_worker,
            name="zenml-mcp-analytics",
            daemon=True,
        )
        _sender_thread.start()


def _stop_sender_thread() -> None:
    _sender_stop_event.set()
    try:
        _event_queue.put_nowait(None)
    except Full:
        pass
    except Exception:
        pass


def _send_events(events: list[dict[str, Any]]) -> None:
    """Enqueue events for async delivery (best-effort, non-blocking)."""
    if not events:
        return

    try:
        _ensure_sender_thread_started()
        _event_queue.put_nowait(list(events))
    except Full:
        pass
    except Exception:
        pass


def _build_track_event(event_name: str, properties: dict[str, Any]) -> dict[str, Any]:
    """Build a track event in analytics server format."""
    return {
        "type": "track",
        "user_id": get_or_create_user_id(),
        "event": event_name,
        "properties": properties,
        "debug": ANALYTICS_DEBUG,
    }


def _build_identify_event(traits: dict[str, Any]) -> dict[str, Any]:
    """Build an identify event in analytics server format."""
    return {
        "type": "identify",
        "user_id": get_or_create_user_id(),
        "traits": traits,
        "debug": ANALYTICS_DEBUG,
    }


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
    }


def _get_traits_hash() -> str:
    """Get hash of current traits to detect changes."""
    traits = _get_traits()
    return hashlib.sha256(json.dumps(traits, sort_keys=True).encode()).hexdigest()[:16]


def _should_identify() -> bool:
    """Check if we should send identify call (traits changed since last time)."""
    config_dir = _get_config_dir()

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


def _register_shutdown_handlers() -> None:
    """Register shutdown handlers (idempotent - only registers once)."""
    global _shutdown_registered

    if _shutdown_registered:
        return

    _shutdown_registered = True

    # Register atexit handler (works in both dev mode and production)
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


def _do_init_analytics() -> None:
    """Initialize analytics (session tracking and identify)."""
    global _session_id, _session_start_time

    # Session tracking (always set, even in dev mode)
    _session_id = str(uuid.uuid4())
    _session_start_time = time.time()

    # Always register shutdown handlers (including dev mode for testing)
    _register_shutdown_handlers()

    if DEV_MODE:
        print("Analytics: dev mode (events logged, not sent)", file=sys.stderr)
        return

    if _should_identify():
        _send_events([_build_identify_event(_get_traits())])


def init_analytics() -> None:
    """Initialize analytics.

    Prints status to stderr and sets up session tracking.
    Safe to call multiple times - will only initialize once.
    """
    global _init_attempted, _init_failed

    try:
        if _init_attempted:
            return

        _init_attempted = True

        if not ANALYTICS_ENABLED:
            print(f"Analytics: disabled ({_disabled_reason})", file=sys.stderr)
            return

        try:
            _do_init_analytics()
            if not DEV_MODE:
                print("Analytics: enabled", file=sys.stderr)
        except Exception as e:
            print(f"Analytics: disabled (init error: {e})", file=sys.stderr)
            _init_failed = True
    except Exception:
        _init_attempted = True
        _init_failed = True


def _ensure_initialized() -> bool:
    """Ensure analytics is initialized.

    Returns True if analytics is ready to use.
    """
    if not ANALYTICS_ENABLED:
        return False

    if _init_failed:
        return False

    if not _init_attempted:
        init_analytics()

    return not _init_failed


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
    try:
        if not _ensure_initialized():
            return

        props = properties.copy() if properties else {}
        props["session_id"] = _session_id
        props["is_ci"] = is_ci_environment()
        # Preserve caller-provided test_run, or inject if env var is set
        if "test_run" not in props and is_test_run_environment():
            props["test_run"] = True

        if DEV_MODE:
            print(f"[Analytics DEV] {event_name}: {props}", file=sys.stderr)
            return

        _send_events([_build_track_event(event_name, props)])
    except Exception:
        return


def track_tool_call(
    tool_name: str,
    success: bool,
    duration_ms: int,
    error_type: str | None = None,
    size: int | None = None,
    http_status_code: int | None = None,
) -> None:
    """Track a tool call with session stats.

    Args:
        tool_name: Name of the tool that was called
        success: Whether the call succeeded
        duration_ms: Duration of the call in milliseconds
        error_type: Type of error if failed (e.g., "HTTPError")
        size: Size parameter if this was a list operation
        http_status_code: HTTP status code if failed due to HTTPError
    """
    try:
        global _tool_call_count

        with _tool_stats_lock:
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
        if http_status_code is not None:
            properties["http_status_code"] = http_status_code

        track_event("Tool Called", properties)
    except Exception:
        return


def track_server_started() -> None:
    """Track server startup event with environment information."""
    try:
        track_event(
            "MCP Server Started",
            {
                "server_version": get_server_version(),
                "python_version": platform.python_version(),
                "os": platform.system(),
                "is_docker": is_running_in_docker(),
            },
        )
    except Exception:
        return


def _on_shutdown() -> None:
    """Handle server shutdown - send summary event.

    Called via atexit or signal handlers.

    IMPORTANT: This function never calls init_analytics() during shutdown.
    """
    try:
        with _shutdown_lock:
            if _shutdown_once.is_set():
                return
            _shutdown_once.set()

        if not ANALYTICS_ENABLED:
            return

        if _session_start_time is None:
            return

        with _tool_stats_lock:
            total_tool_calls = _tool_call_count
            unique_tools_used = len(_tools_used)

        uptime = int(time.time() - _session_start_time)
        props = {
            "uptime_seconds": uptime,
            "total_tool_calls": total_tool_calls,
            "unique_tools_used": unique_tools_used,
            "session_id": _session_id,
            "is_ci": is_ci_environment(),
        }

        if DEV_MODE:
            print(f"[Analytics DEV] MCP Server Shutdown: {props}", file=sys.stderr)
            return

        _send_events([_build_track_event("MCP Server Shutdown", props)])
    finally:
        _stop_sender_thread()
        _close_http_client()


# =============================================================================
# Utility for extracting size argument from tool calls
# =============================================================================


def _coerce_to_int(value: Any) -> int | None:
    """Coerce a value to int if possible.

    Handles int, str (numeric), and float. Returns None for invalid values.
    Also clamps to a reasonable range for analytics (1-10000).
    """
    if value is None:
        return None

    try:
        if isinstance(value, int):
            result = value
        elif isinstance(value, str):
            result = int(value)
        elif isinstance(value, float):
            result = int(value)
        else:
            return None

        # Clamp to reasonable bounds for analytics
        if result < 1 or result > 10000:
            return None
        return result
    except (ValueError, TypeError):
        return None


def extract_size_from_call(
    func_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> int | None:
    """Extract the 'size' argument from a tool call if present.

    Args:
        func_name: Name of the function (unused but available for future logic)
        args: Positional arguments to the function
        kwargs: Keyword arguments to the function

    Returns:
        The size value if found in kwargs (coerced to int), otherwise None
    """
    # For simplicity, we only check kwargs since most callers use keyword args
    # The MCP protocol typically passes arguments as kwargs
    size = kwargs.get("size")
    return _coerce_to_int(size)
