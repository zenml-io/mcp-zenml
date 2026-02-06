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
import re
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, ParamSpec, TypeVar, cast, get_type_hints
from urllib.parse import urlparse

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
# force=True ensures this config applies even if logging was already configured by imports
logging.basicConfig(
    level=log_level,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,
    force=True,
)

# Never log below WARNING to prevent JSON protocol interference

# Suppress ZenML's internal logging to prevent JSON protocol issues
# Must use ERROR level (not WARNING) to suppress "Setting the global active stack" message
# Also clear any handlers ZenML may have added that write to stdout
zenml_logger = logging.getLogger("zenml")
# Properly close and remove handlers to avoid resource leaks
for handler in list(zenml_logger.handlers):
    zenml_logger.removeHandler(handler)
    try:
        handler.close()
    except Exception:
        pass
zenml_logger.setLevel(logging.ERROR)  # Only show errors, not warnings
logging.getLogger("zenml.client").setLevel(logging.ERROR)

# Suppress MCP/FastMCP logging to prevent stdout pollution (breaks JSON-RPC protocol)
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("mcp.server").setLevel(logging.WARNING)
logging.getLogger("mcp.server.fastmcp").setLevel(logging.WARNING)

# Suppress urllib3/requests retry warnings that leak to stdout
# E.g., "Retrying (Retry(total=9...)) after connection broken by 'RemoteDisconnected'"
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.ERROR)

# Type variables for decorator signatures
P = ParamSpec("P")  # Captures function parameters
T = TypeVar("T")  # Captures return type

# Type alias for functions (callables with __name__ attribute)
# Using ParamSpec preserves the original function's parameter types
from collections.abc import Callable


def _is_text_tool(func: Callable[..., Any]) -> bool:
    """Check if a tool function returns str (text-only) vs structured output."""
    try:
        hints = get_type_hints(func)
        return hints.get("return") is str
    except Exception:
        return False  # Default to structured — only 2 tools (easter_egg, get_step_code) are text


def _is_structured_error_envelope(payload: Any) -> bool:
    """Check if a payload matches the structured error envelope shape.

    The canonical envelope produced by _make_error_result() is:
        {"error": {"tool": str, "message": str, "type": str, "http_status_code"?: int}}

    This validates the full shape to avoid false positives when a successful
    tool result legitimately contains an "error" key (e.g. failed-run metadata).
    """
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    required = {"tool", "message", "type"}
    if not required <= set(error.keys()):
        return False
    return all(isinstance(error[k], str) for k in required)


def _make_error_result(
    tool_name: str,
    message: str,
    error_type: str,
    http_status_code: int | None = None,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured error envelope for non-text tools.

    Canonical shape (validated by smoke tests):
        {"error": {"tool": str, "message": str, "type": str, ...}}
    """
    error: dict[str, Any] = {
        "tool": tool_name,
        "message": message,
        "type": error_type,
    }
    if http_status_code is not None:
        error["http_status_code"] = http_status_code
    if details:
        error["details"] = details
    return {"error": error}


# =============================================================================
# Datetime filter normalization
# =============================================================================
# ZenML requires datetime filters in "%Y-%m-%d %H:%M:%S" format exactly.
# LLMs commonly send date-only strings (e.g. gte:2026-02-02), ISO-8601
# timestamps with T/Z/offsets, or range:.. syntax.  All of these cause
# ValidationErrors if passed through as-is.  This helper normalizes the most
# common inputs so they reach ZenML in the right format.

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$"
)
_SPACE_FRAC_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+$")
_RANGE_RE = re.compile(r"^range:(?P<lower>.+?)\.\.(?P<upper>.+)$")
_DATETIME_FILTER_KEYS = frozenset({"created", "updated", "start_time", "end_time"})
_KNOWN_OPS = frozenset(
    {
        "equals",
        "notequals",
        "contains",
        "startswith",
        "endswith",
        "oneof",
        "gte",
        "gt",
        "lte",
        "lt",
        "in",
    }
)
_UPPER_BOUND_OPS = frozenset({"lte", "lt"})


def _parse_iso_to_zenml(s: str) -> str | None:
    """Best-effort parse an ISO-8601 string into ZenML format (YYYY-MM-DD HH:MM:SS).

    Returns None if the string isn't recognizable ISO-8601.
    Timezone-aware inputs are converted to UTC before formatting.
    """
    try:
        # Python 3.11+ fromisoformat handles Z, offsets, fractional seconds
        adjusted = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = datetime.fromisoformat(adjusted)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _norm_datetime_token(s: str, *, upper_bound: bool) -> str:
    """Normalize a single datetime token to ZenML's required format.

    Handles ISO-8601 (T separator, Z suffix, timezone offsets, fractional
    seconds), space-separated datetimes with fractional seconds, and
    date-only strings (YYYY-MM-DD).
    """
    s = s.strip()
    # Try stdlib ISO parser first (handles offsets, Z, missing seconds, etc.)
    if _ISO_DT_RE.match(s):
        parsed = _parse_iso_to_zenml(s)
        if parsed:
            return parsed
    # Space-separated with fractional seconds: 2026-02-01 10:00:00.123 → drop fraction
    m = _SPACE_FRAC_RE.match(s)
    if m:
        return m.group(1)
    # Date-only: append time of day (start or end depending on operator context)
    if _DATE_ONLY_RE.match(s):
        return f"{s} {'23:59:59' if upper_bound else '00:00:00'}"
    return s


def _normalize_datetime_filter(value: str) -> str:
    """Normalize a datetime filter value for ZenML compatibility.

    Handles:
    - range:lower..upper → in:lower 00:00:00,upper 23:59:59
    - gte:YYYY-MM-DD → gte:YYYY-MM-DD 00:00:00
    - lte:YYYY-MM-DD → lte:YYYY-MM-DD 23:59:59
    - ISO timestamps (T separator) → space separator
    - Bare YYYY-MM-DD (no operator) → YYYY-MM-DD 00:00:00
    """
    raw = value.strip()
    if not raw:
        return value

    # Convenience: range:lower..upper → in:lower,upper
    m = _RANGE_RE.match(raw)
    if m:
        lower = _norm_datetime_token(m.group("lower"), upper_bound=False)
        upper = _norm_datetime_token(m.group("upper"), upper_bound=True)
        return f"in:{lower},{upper}"

    # Split optional op:value
    head, sep, tail = raw.partition(":")
    if sep and head in _KNOWN_OPS:
        op, rest = head, tail
    else:
        op, rest = None, raw

    # Handle in: operator (comma-separated pair)
    if op == "in" and "," in rest:
        lower, upper = rest.split(",", 1)
        lower = _norm_datetime_token(lower, upper_bound=False)
        upper = _norm_datetime_token(upper, upper_bound=True)
        return f"in:{lower},{upper}"

    # Normalize single value
    is_upper = op in _UPPER_BOUND_OPS
    norm = _norm_datetime_token(rest, upper_bound=is_upper)
    return f"{op}:{norm}" if op else norm


# =============================================================================
# Exception classification (stable categories + actionable user messages)
# =============================================================================

_ERROR_MISSING_ENV_RE = re.compile(
    r"^(?P<var>[A-Z0-9_]+) environment variable not set$"
)


def _redact_url(url: str | None) -> str | None:
    """Redact URL to scheme+hostname only (avoid leaking paths/tokens)."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.scheme and parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}"
        return parsed.hostname or "<invalid-url>"
    except Exception:
        return "<invalid-url>"


def _classify_exception(
    *,
    tool_name: str,
    exc: Exception,
    http_status_code: int | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Map exceptions into stable categories + safe user messages.

    Returns (category, user_message, details) where category is a stable
    string like 'AuthenticationError', 'DependencyMissing', etc.
    """
    raw_type = type(exc).__name__
    details: dict[str, Any] = {"raw_type": raw_type}

    # ---- HTTP errors (requests) ----
    if isinstance(exc, requests.HTTPError):
        status = http_status_code
        if status is not None:
            details["http_status_code"] = status

        if status == 401:
            return (
                "AuthenticationError",
                "Authentication failed. Please check your API key.",
                details,
            )
        if status == 403:
            return (
                "AuthenticationError",
                "Authorization failed. Your API key may not have access.",
                details,
            )
        if status == 404:
            if tool_name == "get_step_logs":
                return (
                    "NotFound",
                    "Logs not found. Please check the step ID. Also note that if the step was run "
                    "on a stack with a local or non-cloud-based artifact store then no logs will "
                    "have been stored by ZenML.",
                    details,
                )
            if tool_name == "get_deployment_logs":
                return (
                    "NotFound",
                    "Deployment not found or logs unavailable. Please check the deployment "
                    "name/ID. Note that log availability depends on the deployer type and "
                    "infrastructure configuration.",
                    details,
                )
            return ("NotFound", "Resource not found (HTTP 404).", details)

        if status is not None and 400 <= status < 500:
            return (
                "ConfigurationError",
                f"Request failed (HTTP {status}). Please check your inputs and configuration.",
                details,
            )
        if status is not None and status >= 500:
            return (
                "UpstreamError",
                f"ZenML server error (HTTP {status}). Please try again later.",
                details,
            )

        return ("UpstreamError", "Request failed.", details)

    # ---- Validation errors ----
    # Detect by class name + module to avoid false positives from unrelated
    # exceptions that happen to contain "validation" in their text.
    exc_mod = getattr(exc.__class__, "__module__", "")
    is_validation = raw_type == "ValidationError" or (
        "pydantic" in exc_mod and "Validation" in raw_type
    )
    if is_validation:
        error_snippet = str(exc)[:300]
        details["validation_error"] = error_snippet
        # Generic message suitable for any validation error
        msg = "Validation failed. Please check your inputs.\n\n" + error_snippet
        # Add filter-syntax help only for tools that accept filters
        if tool_name.startswith("list_"):
            msg += (
                "\n\nFILTER SYNTAX REFERENCE:\n"
                "- Operators: gte:, lte:, gt:, lt:, contains:, startswith:, oneof:, in:\n"
                "- Datetime format: YYYY-MM-DD HH:MM:SS (e.g. gte:2026-02-01 00:00:00)\n"
                "- Date-only and ISO-8601 inputs are auto-normalized\n"
                "- Date range: in:2026-02-01 00:00:00,2026-02-07 23:59:59"
            )
        return ("ValidationError", msg, details)

    # ---- Common configuration errors (missing env vars) ----
    if isinstance(exc, ValueError):
        msg = str(exc)
        m = _ERROR_MISSING_ENV_RE.match(msg.strip())
        if m:
            var = m.group("var")
            details["missing_env_var"] = var
            return (
                "ConfigurationError",
                f"Missing required environment variable: {var}.",
                details,
            )

    # ---- Missing Python deps / integrations ----
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        details["import_error"] = str(exc)
        return (
            "DependencyMissing",
            f"Missing dependency or integration: {exc}",
            details,
        )

    # ---- Request connectivity/timeouts ----
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        details["connection_error"] = raw_type
        return (
            "UpstreamError",
            "Could not reach ZenML server. Please check network connectivity and ZENML_STORE_URL.",
            details,
        )

    # ---- ZenML auth exceptions (detected by class name to avoid importing ZenML) ----
    if raw_type in ("CredentialsNotValid", "AuthorizationException"):
        return (
            "AuthenticationError",
            "Authentication to ZenML failed. Check your ZENML_STORE_API_KEY.",
            details,
        )

    # ---- Project not configured ----
    msg = str(exc)
    if "No project is currently set as active" in msg:
        return (
            "ProjectNotConfigured",
            "No project is currently set as active. Set ZENML_ACTIVE_PROJECT_ID (or configure an active project in ZenML).",
            details,
        )

    # ---- Version mismatch (heuristics) ----
    if "ZenML" in msg and ("version" in msg.lower() or "incompatible" in msg.lower()):
        details["version_message"] = msg[:200]
        return (
            "VersionMismatch",
            "Version mismatch between this MCP server and your ZenML installation/server.",
            details,
        )

    # ---- Default ----
    # Always show details for ImportError/RuntimeError since they indicate setup/config issues
    if isinstance(exc, (ImportError, RuntimeError)):
        return ("UnexpectedError", f"Error in {tool_name}: {msg}", details)

    if analytics.DEV_MODE:
        return ("UnexpectedError", f"Error in {tool_name}: {msg}", details)

    return ("UnexpectedError", f"Error in {tool_name}: {raw_type}", details)


# =============================================================================
# MCP client detection (best-effort, request-scoped)
# =============================================================================

# Track whether we've already captured client info this session
_mcp_client_info_captured = False
_mcp_client_info_lock = Lock()


def _getattr_multi(obj: Any, *names: str) -> Any:
    """Try multiple attribute names on an object, return first non-None."""
    if obj is None:
        return None
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return None


def _get_mcp_client_info_safe() -> dict[str, Any] | None:
    """Best-effort MCP client detection (only valid during a request).

    Checks both camelCase and snake_case field names to handle different
    MCP SDK versions.
    """
    try:
        ctx = mcp.get_context()
        session = getattr(ctx, "session", None)
        if session is None:
            return None

        params = _getattr_multi(session, "client_params", "clientParams")
        if params is None:
            return None

        client_info = _getattr_multi(params, "clientInfo", "client_info")
        if client_info is None:
            return None

        name = getattr(client_info, "name", None)
        version = getattr(client_info, "version", None)
        if not name and not version:
            return None

        return {"name": name, "version": version}
    except Exception:
        return None


# Decorator for handling exceptions in tool functions (with analytics tracking)
def handle_tool_exceptions(func: Callable[P, T]) -> Callable[P, T]:
    """Decorator for MCP tools - handles exceptions and tracks analytics.

    Use this decorator for @mcp.tool() functions. It:
    - Catches exceptions and returns friendly error messages
    - Tracks tool usage via analytics (timing, success/failure, size param)
    - Returns structured error dicts for structured tools, strings for text tools
    """
    # Capture function name and return type at decoration time
    func_name = func.__name__  # type: ignore[attr-defined]
    text_tool = _is_text_tool(func)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        import time

        global _mcp_client_info_captured

        start_time = time.perf_counter()
        success = True
        error_type: str | None = None
        http_status_code: int | None = None

        # Capture MCP client info once per session (best-effort)
        client: dict[str, Any] | None = None
        try:
            if not _mcp_client_info_captured:
                client = _get_mcp_client_info_safe()
                if client:
                    with _mcp_client_info_lock:
                        if not _mcp_client_info_captured:
                            _mcp_client_info_captured = True
                            analytics.set_client_info_once(
                                client_name=client.get("name"),
                                client_version=client.get("version"),
                            )
        except Exception:
            client = None

        try:
            # Normalize datetime filter kwargs before calling the tool.
            # Uses a copy so analytics.extract_size_from_call sees original kwargs.
            call_kwargs = dict(kwargs) if kwargs else kwargs
            if call_kwargs:
                for key in _DATETIME_FILTER_KEYS:
                    if key in call_kwargs and isinstance(call_kwargs[key], str):
                        call_kwargs[key] = _normalize_datetime_filter(call_kwargs[key])

            result = func(*args, **call_kwargs)
            # Detect structured error envelopes (full shape validation to avoid
            # false positives from legitimate "error" fields in successful results)
            if _is_structured_error_envelope(result):
                success = False
                error_type = cast(dict[str, Any], result)["error"]["type"]
            return result
        except requests.HTTPError as e:
            success = False
            http_status_code = (
                e.response.status_code
                if getattr(e, "response", None) is not None
                else None
            )

            category, message, details = _classify_exception(
                tool_name=func_name,
                exc=e,
                http_status_code=http_status_code,
            )
            error_type = category

            err_log = f"Error in {func_name}: {category}"
            if http_status_code is not None:
                err_log = f"{err_log} (HTTP {http_status_code})"
            if analytics.DEV_MODE:
                err_log = f"{err_log} - {e}"
            print(err_log, file=sys.stderr)

            if text_tool:
                return cast(T, message)
            return cast(
                T,
                _make_error_result(
                    func_name,
                    message,
                    category,
                    http_status_code,
                    details=details,
                ),
            )
        except Exception as e:
            success = False
            category, message, details = _classify_exception(
                tool_name=func_name,
                exc=e,
            )
            error_type = category

            print(message, file=sys.stderr)

            if text_tool:
                return cast(T, message)
            return cast(
                T, _make_error_result(func_name, message, category, details=details)
            )
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
                    mcp_client_name=(client or {}).get("name"),
                    mcp_client_version=(client or {}).get("version"),
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

Most tools return structured JSON data. You should present this data to the
user in a more readable format (e.g. a table or summary) rather than showing
raw JSON.
"""

try:
    logger.debug("Importing MCP dependencies...")
    from mcp.server.fastmcp import FastMCP
    from mcp.types import Tool as MCPTool

    class ZenMLFastMCP(FastMCP):
        """FastMCP subclass that supports _meta on tools and resources.

        The upstream FastMCP may not support the ``meta`` kwarg on
        ``tool()`` / ``resource()`` depending on the installed version.
        This subclass stores per-tool and per-resource meta at registration
        time and injects it into list_tools() / list_resources().
        """

        def __init__(self, *a: Any, **kw: Any) -> None:
            import inspect as _inspect

            super().__init__(*a, **kw)
            self._tool_meta: dict[str, dict[str, Any]] = {}
            self._resource_meta: dict[str, dict[str, Any]] = {}
            # Cache upstream resource() signature to avoid re-inspecting on every call
            self._upstream_resource_params: set[str] = set(
                _inspect.signature(FastMCP.resource).parameters.keys()
            )
            # Only trust proxy headers when explicitly behind a reverse proxy
            self._forwarded_allow_ips: str = "127.0.0.1"

        def add_tool(
            self,
            fn: Any,
            name: str | None = None,
            title: str | None = None,
            description: str | None = None,
            annotations: Any = None,
            structured_output: bool | None = None,
            *,
            meta: dict[str, Any] | None = None,
        ) -> None:
            tool_name = name or fn.__name__
            if meta is not None:
                self._tool_meta[tool_name] = meta
            super().add_tool(
                fn,
                name=name,
                title=title,
                description=description,
                annotations=annotations,
                structured_output=structured_output,
            )

        def tool(
            self,
            name: str | None = None,
            title: str | None = None,
            description: str | None = None,
            annotations: Any = None,
            structured_output: bool | None = None,
            *,
            meta: dict[str, Any] | None = None,
        ) -> Callable[..., Any]:
            if callable(name):
                raise TypeError(
                    "The @tool decorator was used incorrectly. "
                    "Did you forget to call it? Use @tool() instead of @tool"
                )

            def decorator(fn: Any) -> Any:
                self.add_tool(
                    fn,
                    name=name,
                    title=title,
                    description=description,
                    annotations=annotations,
                    structured_output=structured_output,
                    meta=meta,
                )
                return fn

            return decorator

        def resource(
            self,
            uri: str,
            *,
            name: str | None = None,
            title: str | None = None,
            description: str | None = None,
            mime_type: str | None = None,
            meta: dict[str, Any] | None = None,
            **extra: Any,
        ) -> Callable[..., Any]:
            """Override to intercept ``meta`` for older SDK versions.

            If the upstream FastMCP.resource() already supports ``meta``,
            we pass it through. Otherwise, we strip it and store it
            ourselves, injecting it in list_resources().
            """
            upstream_params = self._upstream_resource_params

            # Build kwargs for the upstream call, only passing what it accepts
            kwargs: dict[str, Any] = {}
            if name is not None:
                kwargs["name"] = name
            if description is not None:
                kwargs["description"] = description
            if mime_type is not None:
                kwargs["mime_type"] = mime_type
            # These may not exist in older SDK versions
            if "title" in upstream_params and title is not None:
                kwargs["title"] = title
            if "meta" in upstream_params and meta is not None:
                kwargs["meta"] = meta
            kwargs.update(extra)

            parent_decorator = super().resource(uri, **kwargs)

            # If upstream didn't accept meta, store it ourselves
            if "meta" not in upstream_params and meta is not None:
                self._resource_meta[uri] = meta

            return parent_decorator

        async def list_resources(self) -> list[Any]:
            """Override to inject stored resource meta for older SDK versions."""
            resources = await super().list_resources()
            if not self._resource_meta:
                return resources
            # Inject _meta for resources where we stored meta
            patched = []
            for r in resources:
                uri_str = str(r.uri)
                meta = self._resource_meta.get(uri_str)
                if meta is not None and getattr(r, "meta", None) is None:
                    try:
                        data = r.model_dump(by_alias=True)
                        data["_meta"] = meta
                        patched.append(type(r)(**data))
                    except Exception:
                        patched.append(r)  # graceful fallback
                else:
                    patched.append(r)
            return patched

        async def list_tools(self) -> list[MCPTool]:
            tools = await super().list_tools()
            if not self._tool_meta:
                return tools
            # Inject _meta for tools where we stored meta
            patched = []
            for tool in tools:
                meta = self._tool_meta.get(tool.name)
                if meta is not None and getattr(tool, "meta", None) is None:
                    try:
                        data = tool.model_dump(by_alias=True)
                        data["_meta"] = meta
                        patched.append(MCPTool(**data))
                    except Exception:
                        patched.append(tool)  # graceful fallback
                else:
                    patched.append(tool)
            return patched

        async def run_streamable_http_async(self) -> None:
            """Run StreamableHTTP with proxy-aware uvicorn config.

            The upstream FastMCP creates uvicorn.Config without proxy_headers
            or forwarded_allow_ips, so requests through reverse proxies
            (e.g. cloudflared tunnels) are rejected with 421 Misdirected
            Request due to Host header mismatch. This override fixes that.
            """
            import uvicorn

            starlette_app = self.streamable_http_app()
            config = uvicorn.Config(
                starlette_app,
                host=self.settings.host,
                port=self.settings.port,
                log_level=self.settings.log_level.lower(),
                proxy_headers=True,
                forwarded_allow_ips=self._forwarded_allow_ips,
            )
            server = uvicorn.Server(config)
            await server.serve()

    # Initialize FastMCP server
    logger.debug("Initializing FastMCP server...")
    mcp = ZenMLFastMCP(name="zenml", instructions=INSTRUCTIONS)
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

    data = response.json()
    # The ZenML API returns a list of log entries, but FastMCP expects a dict.
    if isinstance(data, list):
        return {"logs": data}
    return data


# =============================================================================
# Startup Diagnostics (works without ZenML SDK)
# =============================================================================


def collect_zenml_setup_diagnostics(
    *, include_client_info: bool = False
) -> dict[str, Any]:
    """Collect setup diagnostics without requiring ZenML SDK initialization.

    This function is safe even if zenml cannot be imported, env vars are missing,
    or the server URL is unreachable.
    """
    store_url = os.environ.get("ZENML_STORE_URL")
    api_key_present = bool(os.environ.get("ZENML_STORE_API_KEY"))
    active_project_id_present = bool(os.environ.get("ZENML_ACTIVE_PROJECT_ID"))

    checks: dict[str, Any] = {
        "env": {
            "ZENML_STORE_URL_present": bool(store_url),
            "ZENML_STORE_API_KEY_present": api_key_present,
            "ZENML_ACTIVE_PROJECT_ID_present": active_project_id_present,
            "ZENML_STORE_URL_redacted": _redact_url(store_url),
        },
        "python": {
            "version": sys.version.split()[0],
        },
        "analytics": {
            "enabled": analytics.is_analytics_enabled(),
            "dev_mode": analytics.DEV_MODE,
        },
    }

    # ZenML import check (no Client() call)
    try:
        import zenml as _zenml

        checks["zenml"] = {
            "importable": True,
            "version": getattr(_zenml, "__version__", "unknown"),
        }
    except Exception as e:
        checks["zenml"] = {"importable": False, "error_type": type(e).__name__}

    # Connectivity probe (best-effort, short timeouts)
    connectivity: dict[str, Any] = {"attempted": False}
    if store_url:
        connectivity["attempted"] = True
        base = store_url.rstrip("/")
        probe_urls = [f"{base}/api/v1/info", f"{base}/health"]
        for url in probe_urls:
            try:
                r = requests.get(url, timeout=(1.0, 2.5))
                connectivity.update(
                    {
                        "url": _redact_url(url),
                        "status_code": r.status_code,
                        "ok": r.status_code in (200, 204),
                    }
                )
                # Try to extract server version from /api/v1/info response
                if r.status_code == 200 and "info" in url:
                    try:
                        info = r.json()
                        if isinstance(info, dict) and "version" in info:
                            checks["zenml_server_version"] = info["version"]
                    except Exception:
                        pass
                break
            except Exception as e:
                connectivity.update({"ok": False, "last_error_type": type(e).__name__})

    checks["connectivity"] = connectivity

    if include_client_info:
        checks["mcp_client"] = _get_mcp_client_info_safe()

    # Summarize issues
    issues: list[dict[str, Any]] = []
    if not store_url:
        issues.append(
            {
                "severity": "error",
                "code": "missing_store_url",
                "message": "ZENML_STORE_URL is not set.",
            }
        )
    if store_url and not api_key_present:
        issues.append(
            {
                "severity": "error",
                "code": "missing_api_key",
                "message": "ZENML_STORE_API_KEY is not set.",
            }
        )
    if store_url and connectivity.get("attempted") and connectivity.get("ok") is False:
        issues.append(
            {
                "severity": "warning",
                "code": "unreachable",
                "message": "Could not reach ZenML server.",
            }
        )
    if checks.get("zenml", {}).get("importable") is False:
        issues.append(
            {
                "severity": "error",
                "code": "zenml_not_importable",
                "message": "ZenML SDK is not importable in this environment.",
            }
        )

    ok = not any(i["severity"] == "error" for i in issues)
    return {"ok": ok, "issues": issues, "checks": checks}


@mcp.tool()
@handle_tool_exceptions
def diagnose_zenml_setup() -> dict[str, Any]:
    """Diagnose ZenML MCP server setup (env vars, connectivity, auth, versions).

    Returns structured diagnostics about the server's configuration and
    connectivity. This tool works even when the ZenML SDK is not installed
    or environment variables are missing - use it to troubleshoot setup issues.
    """
    return collect_zenml_setup_diagnostics(include_client_info=True)


@mcp.tool()
@handle_tool_exceptions
def get_step_logs(step_run_id: str) -> dict[str, Any]:
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
    return make_step_logs_request(server_url, step_run_id, access_token)


# Page-size defaults for list tools:
#   50 – lightweight resources (users, projects, tags, secrets)
#   20 – medium resources (stacks, pipelines, models, connectors, etc.)
#   10 – heavy payloads (pipeline runs, run steps, artifacts)
@mcp.tool()
@handle_tool_exceptions
def list_users(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 50,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    active: bool | None = None,
) -> dict[str, Any]:
    """List all users in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.
    The 'total' field gives the global count matching your filters.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-01-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        active: Filter by active status
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
    return users.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def get_user(name_id_or_prefix: str) -> dict[str, Any]:
    """Get detailed information about a specific user.

    Args:
        name_id_or_prefix: The name, ID or prefix of the user to retrieve
    """
    user = get_zenml_client().get_user(name_id_or_prefix)
    return user.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def get_active_user() -> dict[str, Any]:
    """Get the currently active user."""
    user = get_zenml_client().active_user
    return user.model_dump(mode="json")


# =============================================================================
# Project Tools
# =============================================================================


@mcp.tool()
@handle_tool_exceptions
def get_active_project() -> dict[str, Any]:
    """Get the currently active project.

    Projects are organizational containers for ZenML resources. Most SDK methods
    are project-scoped, and this tool returns the default project context.
    """
    project = get_zenml_client().active_project
    return project.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def get_project(name_id_or_prefix: str, hydrate: bool = True) -> dict[str, Any]:
    """Get detailed information about a specific project.

    Args:
        name_id_or_prefix: The name, ID or prefix of the project to retrieve
        hydrate: Whether to hydrate the response with additional details
    """
    project = get_zenml_client().get_project(name_id_or_prefix, hydrate=hydrate)
    return project.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_projects(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 50,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """List all projects in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.
    The 'total' field gives the global count matching your filters.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
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
    return projects.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def get_stack(name_id_or_prefix: str) -> dict[str, Any]:
    """Get detailed information about a specific stack.

    Args:
        name_id_or_prefix: The name, ID or prefix of the stack to retrieve
    """
    stack = get_zenml_client().get_stack(name_id_or_prefix)
    return stack.model_dump(mode="json")


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
    size: int = 20,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """List all stacks in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by stack name (e.g. contains:prod)
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
    return stacks.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_pipelines(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 20,
    created: str | None = None,
    updated: str | None = None,
) -> dict[str, Any]:
    """List all pipelines in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
    """
    pipelines = get_zenml_client().list_pipelines(
        sort_by=sort_by,
        page=page,
        size=size,
        created=created,
        updated=updated,
    )
    return pipelines.model_dump(mode="json")


def _get_latest_runs_status(
    pipeline_response,  # PipelineResponse - imported lazily
    num_runs: int = 5,
) -> list[str]:
    """Get the status of the latest runs of a pipeline.

    Args:
        pipeline_response: The pipeline response to get the latest runs from
        num_runs: The number of runs to get the status of
    """
    latest_runs = pipeline_response.runs[:num_runs]
    return [str(run.status) for run in latest_runs]


@mcp.tool()
@handle_tool_exceptions
def get_pipeline_details(
    name_id_or_prefix: str,
    num_runs: int = 5,
) -> dict[str, Any]:
    """Get detailed information about a specific pipeline.

    Args:
        name_id_or_prefix: The name, ID or prefix of the pipeline to retrieve
        num_runs: The number of runs to get the status of
    """
    pipeline = get_zenml_client().get_pipeline(name_id_or_prefix)
    return {
        "pipeline": pipeline.model_dump(mode="json"),
        "latest_runs_status": _get_latest_runs_status(pipeline, num_runs),
        "num_runs": num_runs,
    }


@mcp.tool()
@handle_tool_exceptions
def get_service(name_id_or_prefix: str) -> dict[str, Any]:
    """Get detailed information about a specific service.

    Args:
        name_id_or_prefix: The name, ID or prefix of the service to retrieve
    """
    service = get_zenml_client().get_service(name_id_or_prefix)
    return service.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_services(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 20,
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
) -> dict[str, Any]:
    """List all services in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        id: Filter by service UUID
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
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
    return services.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def get_stack_component(name_id_or_prefix: str) -> dict[str, Any]:
    """Get detailed information about a specific stack component.

    Args:
        name_id_or_prefix: The name, ID or prefix of the stack component to retrieve
    """
    stack_component = get_zenml_client().get_stack_component(name_id_or_prefix)
    return stack_component.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_stack_components(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 20,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    flavor: str | None = None,
    stack_id: str | None = None,
) -> dict[str, Any]:
    """List all stack components in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by component name (e.g. contains:s3)
        flavor: Filter by flavor name (e.g. contains:aws)
        stack_id: Filter by stack UUID
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
    return stack_components.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def get_flavor(name_id_or_prefix: str) -> dict[str, Any]:
    """Get detailed information about a specific flavor.

    Args:
        name_id_or_prefix: The name, ID or prefix of the flavor to retrieve
    """
    flavor = get_zenml_client().get_flavor(name_id_or_prefix)
    return flavor.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_flavors(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 20,
    logical_operator: str = "and",
    id: str | None = None,
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    integration: str | None = None,
) -> dict[str, Any]:
    """List all flavors in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        id: Filter by flavor UUID
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
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
    return flavors.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def trigger_pipeline(
    pipeline_name_or_id: str,
    snapshot_name_or_id: str | None = None,
    stack_name_or_id: str | None = None,
    template_id: str | None = None,
) -> dict[str, Any]:
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

    deprecation_warning: str | None = None
    used_deprecated_template = False
    if snapshot_name_or_id is not None:
        trigger_kwargs["snapshot_name_or_id"] = snapshot_name_or_id
    elif template_id is not None:
        # Fall back to template_id for backward compatibility, but warn
        trigger_kwargs["template_id"] = template_id
        used_deprecated_template = True
        deprecation_warning = (
            "The `template_id` parameter is deprecated. "
            "Please use `snapshot_name_or_id` instead. Run Templates are being "
            "phased out in favor of Snapshots."
        )

    pipeline_run = get_zenml_client().trigger_pipeline(**trigger_kwargs)
    analytics.track_event(
        "Pipeline Triggered",
        {
            "has_snapshot_id": snapshot_name_or_id is not None,
            "has_template_id": template_id is not None,
            "has_stack_override": stack_name_or_id is not None,
            "used_deprecated_template": used_deprecated_template,
            "success": True,
        },
    )
    result: dict[str, Any] = {
        "pipeline_run": pipeline_run.model_dump(mode="json"),
    }
    if deprecation_warning:
        result["deprecation_warning"] = deprecation_warning
    return result


@mcp.tool()
@handle_tool_exceptions
def get_run_template(name_id_or_prefix: str) -> dict[str, Any]:
    """Get a run template for a pipeline.

    ⚠️ DEPRECATED: Run Templates are deprecated in ZenML. Use `get_snapshot` instead.
    Snapshots are the modern replacement for run templates and provide the same
    functionality with better integration into the ZenML ecosystem.

    Args:
        name_id_or_prefix: The name, ID or prefix of the run template to retrieve
    """
    run_template = get_zenml_client().get_run_template(name_id_or_prefix)
    return {
        "deprecation_notice": (
            "Run Templates are deprecated in ZenML. "
            "Please use `get_snapshot` instead. Run Templates internally reference "
            "Snapshots via `source_snapshot_id` and will be removed in a future version."
        ),
        "run_template": run_template.model_dump(mode="json"),
    }


@mcp.tool()
@handle_tool_exceptions
def list_run_templates(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 20,
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """List all run templates in the ZenML workspace.

    DEPRECATED: Use `list_snapshots` instead. For runnable configs, use
    `list_snapshots(runnable=True)`.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by template name (e.g. contains:train)
        tag: Filter by tag name
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
    return {
        "deprecation_notice": (
            "Run Templates are deprecated in ZenML. "
            "Please use `list_snapshots` instead. For runnable configurations, "
            "use `list_snapshots(runnable=True)`. Run Templates will be removed in a future version."
        ),
        "run_templates": run_templates.model_dump(mode="json"),
    }


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
) -> dict[str, Any]:
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
    return snapshot.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_snapshots(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 20,
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
) -> dict[str, Any]:
    """List all snapshots in the ZenML workspace.

    Snapshots are frozen pipeline configurations (replacing deprecated Run Templates).
    Use `runnable=True` to find snapshots that can be triggered.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by snapshot name (e.g. contains:prod)
        pipeline: Filter by pipeline name or UUID
        runnable: If True, only return snapshots that can be triggered
        deployable: If True, only return deployable snapshots
        deployed: If True, only return currently deployed snapshots
        tag: Filter by tag name
        project: Project scope (defaults to active project)
        named_only: Only named snapshots (default True to skip internal ones)
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
    return snapshots.model_dump(mode="json")


# =============================================================================
# Deployment Tools
# =============================================================================


@mcp.tool()
@handle_tool_exceptions
def get_deployment(
    name_id_or_prefix: str,
    project: str | None = None,
    hydrate: bool = True,
) -> dict[str, Any]:
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
    return deployment.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_deployments(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 20,
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
) -> dict[str, Any]:
    """List all deployments in the ZenML workspace.

    Deployments show what's currently serving/provisioned with runtime status.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by deployment name (e.g. contains:prod)
        status: Filter by status (e.g. oneof:running,error)
        url: Filter by deployment URL
        pipeline: Filter by pipeline name or UUID
        snapshot_id: Filter by source snapshot UUID
        tag: Filter by tag name
        project: Project scope (defaults to active project)
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
    return deployments.model_dump(mode="json")


# Maximum size for deployment logs output (100KB)
MAX_DEPLOYMENT_LOGS_SIZE = 100 * 1024


@mcp.tool()
@handle_tool_exceptions
def get_deployment_logs(
    name_id_or_prefix: str,
    project: str | None = None,
    tail: int = 100,
) -> dict[str, Any]:
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
        Dict with 'logs' (string) and metadata about truncation if applicable
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

        result: dict[str, Any] = {
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

        return result

    except ImportError as e:
        # Handle missing deployer plugin (direct import failure)
        return {
            "error": {
                "tool": "get_deployment_logs",
                "type": "deployer_plugin_not_installed",
                "message": (
                    f"The deployer plugin required to fetch logs is not installed: {e}. "
                    "Please install the appropriate ZenML integration for your stack "
                    "(e.g., `zenml integration install gcp` for GCP deployments), "
                    "then restart the MCP server."
                ),
            },
            "logs": None,
        }
    except Exception as e:
        # Check if this is a deployer instantiation error (missing dependencies)
        error_str = str(e)
        if (
            "could not be instantiated" in error_str
            or "dependencies are not installed" in error_str
        ):
            return {
                "error": {
                    "tool": "get_deployment_logs",
                    "type": "deployer_dependencies_missing",
                    "message": (
                        f"The deployer's dependencies are not installed: {error_str}\n\n"
                        "To fix this:\n"
                        "1. Check which stack/deployer was used for this deployment\n"
                        "2. Install the required ZenML integration for that deployer:\n"
                        "   `zenml integration install <integration-name>`\n"
                        "3. Restart the MCP server\n\n"
                        "Common deployer integrations: gcp, aws, azure, kubernetes, huggingface"
                    ),
                },
                "logs": None,
            }
        # Re-raise other exceptions to be handled by the decorator
        raise


@mcp.tool()
@handle_tool_exceptions
def get_schedule(name_id_or_prefix: str) -> dict[str, Any]:
    """Get a schedule for a pipeline.

    Args:
        name_id_or_prefix: The name, ID or prefix of the schedule to retrieve
    """
    schedule = get_zenml_client().get_schedule(name_id_or_prefix)
    return schedule.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_schedules(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 20,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    pipeline_id: str | None = None,
    orchestrator_id: str | None = None,
    active: bool | None = None,
) -> dict[str, Any]:
    """List all schedules in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by schedule name (e.g. contains:daily)
        pipeline_id: Filter by pipeline UUID
        orchestrator_id: Filter by orchestrator UUID
        active: Filter by active status (True/False)
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
    return schedules.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def get_pipeline_run(name_id_or_prefix: str) -> dict[str, Any]:
    """Get a pipeline run by name, ID, or prefix.

    Args:
        name_id_or_prefix: The name, ID or prefix of the pipeline run to retrieve
    """
    pipeline_run = get_zenml_client().get_pipeline_run(name_id_or_prefix)
    return pipeline_run.model_dump(mode="json")


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
) -> dict[str, Any]:
    """List all pipeline runs in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.
    The 'total' field gives the global count matching your filters — useful
    for answering 'how many runs?' without fetching all pages.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).
    Date range: 'in:2026-02-01 00:00:00,2026-02-07 23:59:59'.

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:start_time)
        page: Page number (1-indexed)
        size: Results per page (keep small for runs — they have large payloads)
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by run name (e.g. contains:training)
        pipeline_id: Filter by pipeline UUID
        pipeline_name: Filter by pipeline name (e.g. contains:my_pipeline)
        stack_id: Filter by stack UUID
        status: Filter by run status (e.g. oneof:completed,failed).
            Values: initializing, failed, completed, running, cached
        start_time: Filter by run start time (e.g. gte:2026-02-01 00:00:00)
        end_time: Filter by run end time (e.g. lte:2026-02-07 23:59:59)
        stack: Filter by stack name
        stack_component: Filter by stack component name
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
    return pipeline_runs.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def get_run_step(step_run_id: str) -> dict[str, Any]:
    """Get a run step by name, ID, or prefix.

    Args:
        step_run_id: The ID of the run step to retrieve
    """
    run_step = get_zenml_client().get_run_step(step_run_id)
    return run_step.model_dump(mode="json")


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
) -> dict[str, Any]:
    """List all run steps in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.
    The 'total' field gives the global count matching your filters.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:start_time)
        page: Page number (1-indexed)
        size: Results per page (keep small — step payloads are large)
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by step name (e.g. contains:train)
        status: Filter by step status (e.g. oneof:completed,failed).
            Values: initializing, failed, completed, running, cached
        start_time: Filter by step start time (e.g. gte:2026-02-01 00:00:00)
        end_time: Filter by step end time (e.g. lte:2026-02-07 23:59:59)
        pipeline_run_id: Filter by pipeline run UUID
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
    return run_steps.model_dump(mode="json")


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
) -> dict[str, Any]:
    """List all artifacts in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page (keep small — artifact payloads are large)
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by artifact name (e.g. contains:model)
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
    return artifacts.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def get_artifact_version(
    name_id_or_prefix: str,
    version: str | None = None,
) -> dict[str, Any]:
    """Get detailed information about a specific artifact version.

    Args:
        name_id_or_prefix: The name, ID or prefix of the artifact
        version: Optional specific version (defaults to latest)
    """
    artifact = get_zenml_client().get_artifact_version(
        name_id_or_prefix=name_id_or_prefix,
        version=version,
    )
    return artifact.model_dump(mode="json")


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
) -> dict[str, Any]:
    """List all versions of a specific artifact.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        artifact_name_or_id: The name or UUID of the artifact
        sort_by: Sort field and direction (e.g. desc:created)
        page: Page number (1-indexed)
        size: Results per page (keep small — version payloads are large)
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        tag: Filter by tag name
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
    return versions.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_secrets(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 50,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """List all secrets in the ZenML workspace (names only, no values).

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by secret name (e.g. contains:api)
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
    return secrets.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def get_service_connector(name_id_or_prefix: str) -> dict[str, Any]:
    """Get a service connector by name, ID, or prefix.

    Args:
        name_id_or_prefix: The name, ID or prefix of the service connector to retrieve
    """
    service_connector = get_zenml_client().get_service_connector(name_id_or_prefix)
    return service_connector.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_service_connectors(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 20,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    connector_type: str | None = None,
) -> dict[str, Any]:
    """List all service connectors in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by connector name (e.g. contains:aws)
        connector_type: Filter by connector type (e.g. contains:gcp)
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
    return service_connectors.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def get_model(name_id_or_prefix: str) -> dict[str, Any]:
    """Get a model by name, ID, or prefix.

    Args:
        name_id_or_prefix: The name, ID or prefix of the model to retrieve
    """
    model = get_zenml_client().get_model(name_id_or_prefix)
    return model.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_models(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 20,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """List all models in the ZenML workspace.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by model name (e.g. contains:bert)
        tag: Filter by tag name (e.g. contains:prod)
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
    return models.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def get_model_version(
    model_name_or_id: str,
    model_version_name_or_number_or_id: str,
) -> dict[str, Any]:
    """Get a model version by name, ID, or prefix.

    Args:
        model_name_or_id: The name, ID or prefix of the model to retrieve
        model_version_name_or_number_or_id: The name, ID or prefix of the model version to retrieve
    """
    model_version = get_zenml_client().get_model_version(
        model_name_or_id,
        model_version_name_or_number_or_id,
    )
    return model_version.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_model_versions(
    model_name_or_id: str,
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 20,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    number: int | None = None,
    stage: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """List all model versions for a model.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        model_name_or_id: The name or UUID of the model
        sort_by: Sort field and direction (e.g. desc:created)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by version name
        number: Filter by version number
        stage: Filter by stage (e.g. oneof:production,staging)
        tag: Filter by tag name
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
    return model_versions.model_dump(mode="json")


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
def get_tag(tag_name_or_id: str, hydrate: bool = True) -> dict[str, Any]:
    """Get detailed information about a specific tag.

    Tags are cross-cutting metadata labels for discovery (prod, staging, latest,
    candidate, etc.). Many ZenML entities can be tagged.

    Args:
        tag_name_or_id: The name or ID of the tag to retrieve
        hydrate: Whether to hydrate the response with additional details
    """
    tag = get_zenml_client().get_tag(tag_name_or_id, hydrate=hydrate)
    return tag.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_tags(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 50,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    name: str | None = None,
    exclusive: bool | None = None,
    resource_type: str | None = None,
) -> dict[str, Any]:
    """List all tags in the ZenML workspace.

    Tags enable queries like 'show me all prod deployments' and help organize
    resources. Exclusive tags can only be applied once per entity.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created, asc:name)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        name: Filter by tag name (e.g. contains:prod)
        exclusive: If True, only return exclusive tags
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
    return tags.model_dump(mode="json")


# =============================================================================
# Build Tools
# =============================================================================


@mcp.tool()
@handle_tool_exceptions
def get_build(
    id_or_prefix: str,
    project: str | None = None,
    hydrate: bool = True,
) -> dict[str, Any]:
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
    return build.model_dump(mode="json")


@mcp.tool()
@handle_tool_exceptions
def list_builds(
    sort_by: str = "desc:created",
    page: int = 1,
    size: int = 20,
    logical_operator: str = "and",
    created: str | None = None,
    updated: str | None = None,
    pipeline_id: str | None = None,
    stack_id: str | None = None,
    is_local: bool | None = None,
    contains_code: bool | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """List all pipeline builds in the ZenML workspace.

    Builds contain image info, code embedding, and stack checksums for
    reproducibility and infrastructure debugging.

    Returns paginated results with 'items', 'total', 'page', 'size' fields.

    Filter syntax: String params support 'op:value' operators (gte, lte, gt,
    lt, equals, notequals, contains, startswith, endswith, oneof, in).
    Datetime format: 'YYYY-MM-DD HH:MM:SS' (e.g. gte:2026-02-01 00:00:00).

    Args:
        sort_by: Sort field and direction (e.g. desc:created)
        page: Page number (1-indexed)
        size: Results per page
        logical_operator: Combine filters with 'and' or 'or'
        created: Filter by creation time (e.g. gte:2026-02-01 00:00:00)
        updated: Filter by update time (same syntax as created)
        pipeline_id: Filter by pipeline UUID
        stack_id: Filter by stack UUID
        is_local: If True, only local builds (not runnable from server)
        contains_code: If True, only builds with embedded code
        project: Project scope (defaults to active project)
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
    return builds.model_dump(mode="json")


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


# =============================================================================
# MCP Apps: Pipeline Run Dashboard & Run Activity Chart
# =============================================================================

_UI_ROOT = Path(__file__).resolve().parent / "ui"
DASHBOARD_UI_URI = "ui://zenml/apps/pipeline-runs/index.html"
CHART_UI_URI = "ui://zenml/apps/run-activity-chart/index.html"


@mcp.resource(
    uri=DASHBOARD_UI_URI,
    mime_type="text/html;profile=mcp-app",
    meta={"ui": {"csp": {"resourceDomains": ["https://unpkg.com"]}}},
)
@handle_exceptions
def pipeline_runs_dashboard_ui() -> str:
    """ZenML MCP App: Pipeline Run Dashboard (HTML entrypoint)."""
    return (_UI_ROOT / "pipeline-runs" / "index.html").read_text(encoding="utf-8")


@mcp.resource(
    uri=CHART_UI_URI,
    mime_type="text/html;profile=mcp-app",
    meta={"ui": {"csp": {"resourceDomains": ["https://unpkg.com"]}}},
)
@handle_exceptions
def run_activity_chart_ui() -> str:
    """ZenML MCP App: Run Activity Chart (HTML entrypoint)."""
    return (_UI_ROOT / "run-activity-chart" / "index.html").read_text(encoding="utf-8")


@mcp.resource(uri="resource://zenml_server/apps", mime_type="application/json")
@handle_exceptions
def list_apps() -> str:
    """List available MCP Apps provided by this server."""
    return json.dumps(
        {
            "apps": [
                {
                    "id": "zenml.pipeline_runs_dashboard",
                    "title": "Pipeline Run Dashboard",
                    "description": "Interactive dashboard showing recent pipeline runs with status, steps, and logs.",
                    "entry": DASHBOARD_UI_URI,
                },
                {
                    "id": "zenml.run_activity_chart",
                    "title": "Run Activity Chart",
                    "description": "Interactive bar chart showing pipeline run activity over the last 30 days with status breakdown.",
                    "entry": CHART_UI_URI,
                },
            ]
        }
    )


@mcp.tool(
    meta={
        "ui": {
            "resourceUri": DASHBOARD_UI_URI,
        },
    }
)
@handle_tool_exceptions
def open_pipeline_run_dashboard() -> str:
    """Open an interactive dashboard of recent ZenML pipeline runs.

    The dashboard shows pipeline runs with status indicators, expandable step
    details, filtering, and drill-down into step logs — all in an interactive UI.
    The dashboard fetches its own data dynamically.
    """

    # Return a short message only — no data payload.
    # The iframe fetches its own data via callServerTool("list_pipeline_runs").
    # This prevents Claude from re-rendering the runs as a table below the app.
    return (
        "Opened interactive pipeline runs dashboard. "
        "The dashboard loads data automatically — "
        "do not summarize or re-present pipeline run data below, "
        "the interactive UI above handles all display."
    )


@mcp.tool(
    meta={
        "ui": {
            "resourceUri": CHART_UI_URI,
        },
    }
)
@handle_tool_exceptions
def open_run_activity_chart() -> str:
    """Open an interactive chart showing pipeline run activity over the last 30 days.

    Shows a bar chart with daily run counts, hover tooltips, and status
    breakdown (completed in green, failed in red, other in amber).
    """

    return (
        "Opened pipeline run activity chart. "
        "The chart loads data automatically — "
        "do not summarize or re-present pipeline run data below, "
        "the interactive chart above handles all display."
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
    import argparse

    parser = argparse.ArgumentParser(description="ZenML MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol (default: stdio). Use 'streamable-http' for MCP Apps support.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for HTTP transport (default: 8000)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for HTTP transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--disable-dns-rebinding-protection",
        action="store_true",
        default=False,
        help="Disable DNS rebinding protection for HTTP transport. "
        "Required when running behind reverse proxies (cloudflared, ngrok). "
        "WARNING: Only use this in trusted network environments.",
    )
    _startup_env = (os.getenv("ZENML_MCP_STARTUP_VALIDATION") or "off").lower().strip()
    if _startup_env not in {"off", "warn", "strict"}:
        print(
            f"Warning: ZENML_MCP_STARTUP_VALIDATION={_startup_env!r} is not valid "
            f"(expected off/warn/strict), defaulting to 'off'",
            file=sys.stderr,
        )
        _startup_env = "off"
    parser.add_argument(
        "--startup-validation",
        choices=["off", "warn", "strict"],
        default=_startup_env,
        help="Run a lightweight startup diagnostic before serving MCP. "
        "'warn' prints problems but continues. 'strict' exits non-zero if "
        "required setup is missing. (default: off, env: ZENML_MCP_STARTUP_VALIDATION)",
    )
    args = parser.parse_args()

    try:
        analytics.init_analytics()

        # Attach transport to session-wide analytics properties
        try:
            analytics.set_session_properties({"transport": args.transport})
        except Exception:
            pass

        # Run startup validation if enabled
        startup_extra: dict[str, Any] = {
            "startup_validation_mode": args.startup_validation
        }
        if args.startup_validation != "off":
            diag = collect_zenml_setup_diagnostics(include_client_info=False)
            startup_extra["startup_validation_ok"] = bool(diag.get("ok"))

            # Include ZenML versions if detected
            zenml_info = diag.get("checks", {}).get("zenml", {})
            if zenml_info.get("importable"):
                startup_extra["zenml_sdk_version"] = zenml_info.get("version")
            server_version = diag.get("checks", {}).get("zenml_server_version")
            if server_version:
                startup_extra["zenml_server_version"] = server_version

            if args.startup_validation == "warn" and not diag.get("ok"):
                print("Startup validation warnings:", file=sys.stderr)
                for issue in diag.get("issues", []):
                    print(
                        f"  - [{issue.get('severity')}] {issue.get('message')}",
                        file=sys.stderr,
                    )

            if args.startup_validation == "strict" and not diag.get("ok"):
                print(
                    "Startup validation failed (strict mode). Refusing to start.",
                    file=sys.stderr,
                )
                for issue in diag.get("issues", []):
                    print(
                        f"  - [{issue.get('severity')}] {issue.get('message')}",
                        file=sys.stderr,
                    )
                analytics.track_event(
                    "Startup Validation Failed",
                    {
                        "issues_count": len(diag.get("issues", [])),
                    },
                )
                raise SystemExit(2)

        analytics.track_server_started(extra_properties=startup_extra)

        if args.transport == "streamable-http":
            from mcp.server.transport_security import TransportSecuritySettings

            # Configure HTTP settings before running
            mcp.settings.host = args.host
            mcp.settings.port = args.port

            if args.disable_dns_rebinding_protection:
                # Disable DNS rebinding protection — required behind reverse
                # proxies (cloudflared, ngrok) where Host header ≠ localhost.
                print(
                    "WARNING: DNS rebinding protection is disabled. "
                    "Only use this behind a trusted reverse proxy.",
                    file=sys.stderr,
                )
                mcp.settings.transport_security = TransportSecuritySettings(
                    enable_dns_rebinding_protection=False,
                )
                # Trust proxy headers from any IP (needed behind reverse proxies)
                mcp._forwarded_allow_ips = "*"
                # Ensure no stale session manager exists so the new security
                # settings take effect when streamable_http_app() is called.
                mcp._session_manager = None

            logger.info(
                f"Starting ZenML MCP server on http://{args.host}:{args.port}/mcp"
            )

        mcp.run(transport=args.transport)
    except Exception as e:
        logger.error(f"Error running MCP server: {e}")
