# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "mcp[cli]==2.2.0",
#     "zenml==0.97.0",
#     "setuptools",
#     "requests>=2.32.0",
# ]
#
# [tool.uv]
# exclude-newer-package = { mcp = "2026-09-08T00:00:00Z", "mcp-types" = "2026-09-08T00:00:00Z" }
#
# [tool.ty.rules]
# # ty >=0.0.62 takes rules from this block, not pyproject.toml. See CLAUDE.md "Note on third-party imports".
# unresolved-import = "ignore"
# ///

# Ensure setuptools is imported first to provide distutils compatibility
try:
    import setuptools  # noqa
except ImportError:
    pass

import argparse
import asyncio
import functools
import inspect
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import time
import warnings
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version as distribution_version
from pathlib import Path
from threading import Lock
from typing import Any, Dict, ParamSpec, TypeVar, cast
from urllib.parse import urlparse

import requests
import zenml_mcp_analytics as analytics
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import ConfigDict
from urllib3.util.retry import Retry
from zenml_resource_dispatch import (
    ResourceDispatchError,
    ResourceFeatureUnavailable,
    ResourceNotFound,
    ResourcePermissionDenied,
    _is_pre_dispatch_connection_failure,
    ensure_writes_enabled,
    safe_project,
)
from zenml_resource_dispatch import (
    action_resource as dispatch_action_resource,
)
from zenml_resource_dispatch import (
    create_resource as dispatch_create_resource,
)
from zenml_resource_dispatch import (
    delete_resource as dispatch_delete_resource,
)
from zenml_resource_dispatch import (
    get_resource as dispatch_get_resource,
)
from zenml_resource_dispatch import (
    list_resources as dispatch_list_resources,
)
from zenml_resource_dispatch import (
    update_resource as dispatch_update_resource,
)
from zenml_resource_registry import (
    ACTION_REGISTRY,
    RESOURCE_REGISTRY,
    ResourceRegistryError,
    describe_resources,
)
from zenml_tool_catalog import (
    ALL_TOOL_NAMES,
    configured_profile,
    configured_write_policy,
    tool_names,
)

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
logging.getLogger("mcp.server.mcpserver").setLevel(logging.WARNING)

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


class FilterSyntaxError(ValueError):
    """Raised when a list filter uses obsolete ambiguous syntax."""


def _validate_filter_syntax(value: str) -> None:
    """Reject ambiguous comma-separated values for list-valued filter operators."""
    operator, separator, operand = value.partition(":")
    if not separator or operator not in {"oneof", "notoneof"}:
        return
    try:
        parsed = json.loads(operand)
    except json.JSONDecodeError as error:
        raise FilterSyntaxError(
            'List filters require a JSON array, for example oneof:["running","error"].'
        ) from error
    if not isinstance(parsed, list):
        raise FilterSyntaxError(
            'List filters require a JSON array, for example oneof:["running","error"].'
        )


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
_SENSITIVE_ERROR_TEXT_RE = re.compile(
    r"(?i)(?:api[_ -]?key|authorization|bearer|credential|password|secret|token)"
)


def _list_input_help(tool_name: str) -> str:
    """Return compact filter and sort guidance for list-tool input failures."""
    if not (tool_name.startswith("list_") or tool_name == "zenml_list_resources"):
        return ""
    return (
        "\n\nLIST INPUT REFERENCE:\n"
        "- Sort fields use direction:field, for example desc:created.\n"
        "- Filter operators include gte:, lte:, contains:, startswith:, oneof:, "
        "notoneof:, and in:.\n"
        '- Multi-value filters use a JSON array, for example oneof:["running","error"].\n'
        "- Datetimes use YYYY-MM-DD HH:MM:SS; date-only and ISO-8601 inputs are normalized."
    )


def _bounded_input_error_message(exc: Exception) -> str | None:
    """Return a short SDK input error unless it looks capable of leaking a secret."""
    message = str(exc).strip().strip("'")
    if not message or len(message) > 500 or _SENSITIVE_ERROR_TEXT_RE.search(message):
        return None
    if any(ord(character) < 32 and character not in "\n\t" for character in message):
        return None
    return message


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

    if isinstance(exc, ResourcePermissionDenied):
        return ("PermissionDenied", "Permission denied for this resource.", details)
    if isinstance(exc, ResourceFeatureUnavailable):
        return (
            "FeatureUnavailable",
            str(exc) or "This ZenML server feature is disabled or unavailable.",
            details,
        )
    if isinstance(exc, ResourceNotFound):
        return ("NotFound", str(exc), details)
    if isinstance(exc, (ResourceDispatchError, ResourceRegistryError)):
        return ("ValidationError", str(exc), details)

    if isinstance(exc, FilterSyntaxError):
        return ("ValidationError", str(exc), details)

    if isinstance(exc, (json.JSONDecodeError, requests.exceptions.JSONDecodeError)):
        return (
            "UpstreamError",
            "ZenML server returned an invalid JSON response.",
            details,
        )

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
            if tool_name.startswith("zenml_"):
                return (
                    "PermissionDenied",
                    "Permission denied for this resource.",
                    details,
                )
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
        msg = "Validation failed. Please check your inputs."
        msg += _list_input_help(tool_name)
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
        return (
            "ValidationError",
            "Invalid input. Please check the supplied values."
            + _list_input_help(tool_name),
            details,
        )

    # ---- Missing Python deps / integrations ----
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return (
            "DependencyMissing",
            "A required dependency or integration is unavailable.",
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
        return (
            "VersionMismatch",
            "Version mismatch between this MCP server and your ZenML installation/server.",
            details,
        )

    # A few ZenML SDK paths still use RuntimeError for ordinary lookup and
    # argument failures. Recognize their shape but keep their text redacted;
    # arbitrary RuntimeErrors stay on the fully redacted default path.
    if isinstance(exc, RuntimeError):
        bounded_message = _bounded_input_error_message(exc)
        lowered = bounded_message.lower() if bounded_message else ""
        if any(
            marker in lowered
            for marker in ("not found", "does not exist", "could not find")
        ):
            return (
                "NotFound",
                "The requested ZenML resource was not found.",
                details,
            )
        if any(
            marker in lowered
            for marker in (
                "already exists",
                "invalid value",
                "must be",
                "expected one of",
            )
        ):
            return (
                "ValidationError",
                "Invalid input. Please check the supplied values."
                + _list_input_help(tool_name),
                details,
            )

    # ---- Default ----
    return ("UnexpectedError", f"Error in {tool_name}: {raw_type}", details)


# =============================================================================
# MCP client detection (best-effort, request-scoped)
# =============================================================================


def _getattr_multi(obj: Any, *names: str) -> Any:
    """Try multiple attribute names on an object, return first non-None."""
    if obj is None:
        return None
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return None


_current_mcp_context: ContextVar[Context[Any, Any] | None] = ContextVar(
    "current_mcp_context", default=None
)


def _get_mcp_client_info_safe(
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any] | None:
    """Best-effort MCP client detection (only valid during a request).

    Checks both camelCase and snake_case field names to handle different
    MCP SDK versions.
    """
    try:
        ctx = ctx or _current_mcp_context.get()
        if ctx is None:
            return None
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
    - Returns structured MCP error results
    """
    # getattr-with-default keeps the type checker honest: a generic Callable
    # isn't guaranteed to have __name__, even though our decorated tools always do.
    func_name = getattr(func, "__name__", "unknown_tool")

    @functools.wraps(func)
    def wrapper(*args: Any, ctx: Context[Any, Any] | None = None, **kwargs: Any) -> T:
        start_time = time.perf_counter()
        success = True
        reported_outcome = "success"
        error_type: str | None = None
        http_status_code: int | None = None
        generic_operation = {
            "zenml_describe_resources": "describe",
            "zenml_list_resources": "list",
            "zenml_get_resource": "get",
            "zenml_create_resource": "create",
            "zenml_update_resource": "update",
            "zenml_delete_resource": "delete",
            "zenml_action_resource": "action",
        }.get(func_name)
        generic_resource_type: str | None = None
        generic_action: str | None = None
        if generic_operation:
            try:
                bound = inspect.signature(func).bind_partial(*args, **kwargs)
                candidate_resource_type = bound.arguments.get("resource_type")
                generic_resource_type = (
                    candidate_resource_type
                    if candidate_resource_type in RESOURCE_REGISTRY
                    else "unknown"
                )
                if generic_operation == "action":
                    candidate_action = bound.arguments.get("action")
                    generic_action = (
                        candidate_action
                        if (generic_resource_type, candidate_action) in ACTION_REGISTRY
                        else "unknown"
                    )
            except Exception:
                pass

        client = _get_mcp_client_info_safe(ctx)
        try:
            if client:
                analytics.set_client_info_once(
                    client_name=client.get("name"),
                    client_version=client.get("version"),
                )
        except Exception:
            pass

        context_token = _current_mcp_context.set(ctx)
        try:
            # Normalize datetime filter kwargs before calling the tool.
            # Uses a copy so analytics.extract_size_from_call sees original kwargs.
            call_kwargs = dict(kwargs) if kwargs else kwargs
            if call_kwargs and func_name.startswith("list_"):
                for value in call_kwargs.values():
                    if isinstance(value, str):
                        _validate_filter_syntax(value)
            if call_kwargs:
                for key in _DATETIME_FILTER_KEYS:
                    if key in call_kwargs and isinstance(call_kwargs[key], str):
                        call_kwargs[key] = _normalize_datetime_filter(call_kwargs[key])

            with _zenml_client_call_lock:
                result = func(*args, **call_kwargs)
            # Detect structured error envelopes (full shape validation to avoid
            # false positives from legitimate "error" fields in successful results)
            if _is_structured_error_envelope(result):
                success = False
                error_type = cast(dict[str, Any], result)["error"]["type"]
                error = cast(dict[str, Any], result)["error"]
                reported_outcome = cast(dict[str, Any], result).get("outcome", "error")
                return cast(
                    T,
                    CallToolResult(
                        content=[TextContent(type="text", text=error["message"])],
                        structured_content=cast(dict[str, Any], result),
                        is_error=True,
                    ),
                )
            if generic_operation and isinstance(result, dict):
                outcome = result.get("outcome")
                if outcome in {"accepted", "completed", "success"}:
                    reported_outcome = outcome
            return result
        except requests.HTTPError as e:
            success = False
            reported_outcome = "error"
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
            print(err_log, file=sys.stderr)

            return cast(
                T,
                CallToolResult(
                    content=[TextContent(type="text", text=message)],
                    structured_content=_make_error_result(
                        func_name,
                        message,
                        category,
                        http_status_code,
                        details=details,
                    ),
                    is_error=True,
                ),
            )
        except Exception as e:
            success = False
            reported_outcome = "error"
            category, message, details = _classify_exception(
                tool_name=func_name,
                exc=e,
            )
            error_type = category

            print(f"Error in {func_name}: {category}", file=sys.stderr)

            return cast(
                T,
                CallToolResult(
                    content=[TextContent(type="text", text=message)],
                    structured_content=_make_error_result(
                        func_name, message, category, details=details
                    ),
                    is_error=True,
                ),
            )
        finally:
            _current_mcp_context.reset(context_token)
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
                    resource_type=generic_resource_type,
                    operation=generic_operation,
                    action=generic_action,
                    profile=ACTIVE_TOOL_PROFILE,
                    outcome=reported_outcome,
                )
            except Exception:
                pass

    signature = inspect.signature(func)
    parameters = list(signature.parameters.values())
    context_parameter = inspect.Parameter(
        "ctx",
        kind=inspect.Parameter.KEYWORD_ONLY,
        annotation=Context,
        default=None,
    )
    var_keyword_index = next(
        (
            index
            for index, parameter in enumerate(parameters)
            if parameter.kind is inspect.Parameter.VAR_KEYWORD
        ),
        len(parameters),
    )
    parameters.insert(var_keyword_index, context_parameter)
    cast(Any, wrapper).__signature__ = signature.replace(parameters=parameters)
    wrapper.__annotations__ = dict(getattr(func, "__annotations__", {}))
    wrapper.__annotations__["ctx"] = Context
    return wrapper


# Decorator for handling exceptions in prompts/resources (no analytics)
def handle_exceptions(func: Callable[P, T]) -> Callable[P, T]:
    """Decorator for prompts/resources - handles exceptions without analytics.

    Use this decorator for @mcp.prompt() and @mcp.resource() functions.
    It catches exceptions but does NOT track analytics (to avoid noise from
    non-tool endpoints).
    """
    # Capture function name at decoration time. getattr-with-default avoids type
    # checker issues: a generic Callable isn't guaranteed to expose __name__.
    func_name = getattr(func, "__name__", "unknown")

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            with _zenml_client_call_lock:
                return func(*args, **kwargs)
        except Exception as e:
            error_type = type(e).__name__
            message = f"Error in {func_name}: {error_type}"
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

Prefer zenml_describe_resources and the advertised generic resource tools.
The create, update, delete, and action tools are advertised only when the write
policy is read_write. Entity-specific list and get tools may be advertised by
the legacy compatibility profile for existing clients. Use the generic
resource tools for new calls and migrations.
"""

logger.debug("Initializing MCP server...")
mcp = MCPServer(
    name="zenml",
    instructions=INSTRUCTIONS,
    log_level=cast(Any, logging.getLevelName(log_level)),
)
logger.debug("MCP server initialized successfully")

ACTIVE_TOOL_PROFILE = configured_profile()
ACTIVE_WRITE_POLICY = configured_write_policy()
ACTIVE_TOOL_NAMES = frozenset(tool_names(ACTIVE_TOOL_PROFILE, ACTIVE_WRITE_POLICY))

# ZenML's Client and REST session are singletons. Tool and resource handlers
# execute in MCP worker threads, so serialize access until the SDK guarantees
# thread-safe concurrent use.
zenml_client = None
_zenml_client_call_lock = Lock()


# Track if we've already reported client init failure (avoid spam)
_client_init_failure_reported = False
_zenml_client_init_lock = Lock()


def _rest_session(client: Any) -> requests.Session | None:
    """The client's REST session, or None if it isn't connected to a server."""
    session = getattr(client.zen_store, "session", None)
    return session if isinstance(session, requests.Session) else None


def _configure_zero_retry_rest_session(client: Any) -> None:
    """Disable automatic REST retries once while preserving pool sizing.

    ZenML 0.96 retries every HTTP method by default, including mutations. The
    MCP server cannot safely repeat a mutation after a response is lost, so the
    shared public REST session uses zero-retry adapters for both schemes.
    ZenML's explicit re-authentication after a rejected token remains intact.
    """
    session = _rest_session(client)
    if session is None:
        return

    retries = Retry(
        total=0,
        connect=0,
        read=0,
        redirect=0,
        status=0,
        other=0,
    )
    for scheme in ("http://", "https://"):
        adapter = session.adapters.get(scheme)
        if adapter is not None:
            adapter.max_retries = retries


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
            initialized_client = Client()
            _configure_zero_retry_rest_session(initialized_client)
            zenml_client = initialized_client
            logger.debug("ZenML client initialized successfully")
        except Exception as e:
            logger.error("ZenML client initialization failed: %s", type(e).__name__)
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


# Connect and read timeouts for direct REST calls to the ZenML server.
_ZENML_REST_TIMEOUT = (3.05, 30)


def get_access_token(
    server_url: str,
    api_key: str,
    *,
    timeout: tuple[float, float] = _ZENML_REST_TIMEOUT,
) -> str:
    """
    Generate a short-lived access token using the ZenML API key.

    ``diagnose_zenml_setup`` uses this to check the credentials with a fresh
    login. Tools use the ZenML client's own authenticated session instead.

    Args:
        server_url: The base URL of the ZenML server
        api_key: The ZenML API key

    Returns:
        The access token as a string

    Raises:
        requests.HTTPError: If the request fails
        RuntimeError: If the response doesn't contain a usable access token
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
        timeout=timeout,
    )
    response.raise_for_status()

    try:
        token_data = response.json()
    except (json.JSONDecodeError, requests.exceptions.JSONDecodeError):
        raise
    try:
        access_token = token_data["access_token"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("Invalid ZenML authentication response") from error
    if not isinstance(access_token, str) or not access_token.strip():
        raise RuntimeError("Invalid ZenML authentication response")
    return access_token


# Most log entries one get_step_logs call returns. It matches ZenML's default
# LOGS_MAX_ENTRIES_PER_REQUEST, which is what 0.96 servers already returned.
STEP_LOGS_MAX_ENTRIES = 50_000

# Server URLs that answered /logs/{id}/entries with FastAPI's "no such route"
# 404 (servers older than ZenML 0.97), with the time.monotonic() of that
# answer. For the next hour, calls skip straight to the older /steps/{id}/logs
# endpoint; after that they probe again, so an upgraded server is picked up
# without restarting the MCP server.
_servers_without_log_entries: dict[str, float] = {}
_LOG_ENTRIES_RECHECK_S = 3600.0


def _is_missing_route(response: requests.Response) -> bool:
    """Tell FastAPI's unknown-route 404 apart from ZenML's own not-found errors.

    An unknown route returns ``{"detail": "Not Found"}``. ZenML's KeyError
    handler returns a list-shaped ``detail``, e.g. for an unknown logs ID.
    """
    if response.status_code != 404:
        return False
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return False
    return isinstance(detail, str)


def _resolve_step_logs_id(
    session: requests.Session, server_url: str, step_id: str, source: str
) -> str | None:
    """Find the ID of the step's log stream with the given source."""
    response = session.get(
        f"{server_url}/api/v1/steps/{step_id}",
        params={"hydrate": "true"},
        timeout=_ZENML_REST_TIMEOUT,
    )
    response.raise_for_status()
    resources = response.json().get("resources") or {}
    for logs in resources.get("log_collection") or []:
        if (logs.get("body") or {}).get("source") == source:
            return logs.get("id")
    return None


def _store_limit_note() -> str:
    return (
        f"The log store returns only the first {STEP_LOGS_MAX_ENTRIES:,} entries "
        "of a log, counted from the start, so later entries are missing."
    )


def _fetch_log_entries(
    session: requests.Session, server_url: str, logs_id: str, *, wanted: int
) -> tuple[list[Any], bool, str | None] | None:
    """Read a log stream through the paginated ZenML 0.97+ entries endpoint.

    The first request leaves ``start`` unset so each log store reads from its
    own default end: the artifact store from the oldest entry (it rejects
    ``start=newest``), Datadog from the newest. The code then follows whichever
    cursor comes back. ``before`` pages hold older entries and ``after`` pages
    newer ones; the pages are joined oldest first at the end.

    Returns:
        ``(entries, server_may_have_more, note)``, or None if the server has no
        entries endpoint. ``note`` says why reading stopped early, except when
        it stopped because it already had the ``tail`` entries it needed.
    """
    url = f"{server_url}/api/v1/logs/{logs_id}/entries"
    response = session.get(
        url, params={"limit": STEP_LOGS_MAX_ENTRIES}, timeout=_ZENML_REST_TIMEOUT
    )
    if _is_missing_route(response):
        return None
    response.raise_for_status()
    page = response.json()
    pages: list[list[Any]] = [page.get("items") or []]
    direction = "before" if page.get("before") else "after"
    cursor = page.get(direction)
    if not cursor:
        # A store without cursors (the artifact store) returns one page. A
        # full page means the file may continue past the server's limit.
        full = len(pages[0]) >= STEP_LOGS_MAX_ENTRIES
        return pages[0], full, _store_limit_note() if full else None

    # Walking back from the newest entry: stop once there are enough. Walking
    # forward from the oldest: the tail is at the end, so keep going until the
    # stream ends or the total cap is reached. Any break that leaves `cursor`
    # set means entries were left unread.
    stop_at = wanted if direction == "before" else STEP_LOGS_MAX_ENTRIES
    unread = "older" if direction == "before" else "newer"
    total = len(pages[0])
    note = None
    while cursor and total < stop_at:
        try:
            response = session.get(
                url, params={direction: cursor}, timeout=_ZENML_REST_TIMEOUT
            )
            response.raise_for_status()
            page = response.json()
        except (requests.RequestException, ValueError) as error:
            # Keep the pages already read rather than discarding them over a
            # rate limit (429) or a log-backend outage (502/503) on a later page.
            logger.warning("Stopped paging step logs early: %s", type(error).__name__)
            failure = (
                f"HTTP {error.response.status_code}"
                if isinstance(error, requests.HTTPError) and error.response is not None
                else type(error).__name__
            )
            note = (
                f"A later page of logs failed to load ({failure}), so {unread} "
                "entries are missing. Calling again may return them."
            )
            break
        items = page.get("items") or []
        if not items:
            cursor = None
            break
        pages.append(items)
        total += len(items)
        cursor = page.get(direction)
    if cursor and note is None and stop_at == STEP_LOGS_MAX_ENTRIES:
        note = (
            f"Reading stopped at the {STEP_LOGS_MAX_ENTRIES:,}-entry limit, so "
            f"{unread} entries were not read."
        )
    if direction == "before":
        pages.reverse()
    return [entry for items in pages for entry in items], bool(cursor), note


def make_step_logs_request(
    session: requests.Session,
    server_url: str,
    step_id: str,
    *,
    source: str | None = None,
    logs_id: str | None = None,
    tail: int | None = None,
) -> Dict[str, Any]:
    """Get logs for a specific step from the ZenML API.

    On ZenML 0.97+ servers this reads the paginated ``/logs/{id}/entries``
    endpoint and follows its cursors. Older servers only have
    ``/steps/{id}/logs``, which returns a single list.

    Args:
        session: An authenticated session for the ZenML server
        server_url: The base URL of the ZenML server
        step_id: The ID of the step to get logs for
        source: The log source to read. Exactly one of source/logs_id.
        logs_id: The exact log stream ID to read.
        tail: Return only the newest ``tail`` entries.

    Returns:
        ``{"logs": [...], "possibly_truncated": bool}``, entries oldest first.
        ``possibly_truncated`` is True when the stream may hold entries that
        were not returned (because of ``tail``, the entry cap, or a failed
        later page). A ``note`` then says which entries are missing and why.

    Raises:
        requests.HTTPError: If the request fails
    """
    server_url = server_url.rstrip("/")
    if source is not None:
        source = source.strip()
    if logs_id is not None:
        logs_id = logs_id.strip()
    if (not source and not logs_id) or (source is not None and logs_id is not None):
        raise ValueError("Exactly one of source or logs_id must be provided.")
    wanted = tail or STEP_LOGS_MAX_ENTRIES

    logger.debug(f"Fetching logs for step {step_id}")

    fetched = None
    marked_old_at = _servers_without_log_entries.get(server_url)
    if (
        marked_old_at is None
        or time.monotonic() - marked_old_at >= _LOG_ENTRIES_RECHECK_S
    ):
        stream_id = logs_id or _resolve_step_logs_id(
            session, server_url, step_id, cast(str, source)
        )
        # No stream with that source: the older endpoint below returns
        # ZenML's usual "no logs found" error.
        if stream_id is not None:
            fetched = _fetch_log_entries(session, server_url, stream_id, wanted=wanted)
            if fetched is None:
                _servers_without_log_entries[server_url] = time.monotonic()
            else:
                _servers_without_log_entries.pop(server_url, None)
    if fetched is None:
        params = {"source": source} if source is not None else {"logs_id": logs_id}
        response = session.get(
            f"{server_url}/api/v1/steps/{step_id}/logs",
            params=params,
            timeout=_ZENML_REST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            return data
        # This endpoint returns at most one server page (50,000 entries by
        # default) and gives no sign when it stopped early.
        full = len(data) >= STEP_LOGS_MAX_ENTRIES
        fetched = data, full, _store_limit_note() if full else None
    entries, more, note = fetched
    notes = [note] if note else []
    # A note from the fetch explains any early stop other than having enough
    # entries for `tail`, so only credit `tail` when it actually cut entries
    # or was the reason reading stopped.
    if tail is not None and (len(entries) > wanted or (more and note is None)):
        notes.append(
            f"Only the newest {tail:,} of the entries read were returned, "
            f"because tail={tail}."
        )
    elif len(entries) > wanted:
        notes.append(
            f"Only the newest {wanted:,} entries were returned, the most one "
            "call returns."
        )
    result: Dict[str, Any] = {
        "logs": entries[-wanted:],
        "possibly_truncated": more or len(entries) > wanted,
    }
    if notes:
        result["note"] = " ".join(notes)
    return result


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
    api_key = os.environ.get("ZENML_STORE_API_KEY")
    api_key_present = bool(api_key)
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

    authentication: dict[str, Any] = {"attempted": False}
    if store_url and api_key:
        authentication["attempted"] = True
        try:
            get_access_token(store_url, api_key, timeout=(1.0, 2.5))
            authentication["ok"] = True
        except requests.HTTPError as error:
            status_code = (
                error.response.status_code
                if getattr(error, "response", None) is not None
                else None
            )
            authentication.update(
                {
                    "ok": False,
                    "error_type": type(error).__name__,
                    "status_code": status_code,
                    "failure_kind": (
                        "rejected" if status_code in {401, 403} else "server_error"
                    ),
                }
            )
        except (requests.Timeout, requests.ConnectionError) as error:
            authentication.update(
                {
                    "ok": False,
                    "error_type": type(error).__name__,
                    "failure_kind": "unreachable",
                }
            )
        except Exception as error:
            authentication.update(
                {
                    "ok": False,
                    "error_type": type(error).__name__,
                    "failure_kind": "invalid_response",
                }
            )
    checks["authentication"] = authentication

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
    if authentication.get("failure_kind") == "rejected":
        issues.append(
            {
                "severity": "error",
                "code": "authentication_failed",
                "message": "ZenML authentication failed. Check ZENML_STORE_API_KEY.",
            }
        )
    elif authentication.get("failure_kind") == "server_error":
        issues.append(
            {
                "severity": "error",
                "code": "authentication_server_error",
                "message": "The ZenML authentication endpoint returned a server error.",
            }
        )
    elif authentication.get("failure_kind") == "unreachable":
        issues.append(
            {
                "severity": "error",
                "code": "authentication_unreachable",
                "message": "Could not validate ZenML credentials because the authentication endpoint was unavailable.",
            }
        )
    elif authentication.get("failure_kind") == "invalid_response":
        issues.append(
            {
                "severity": "error",
                "code": "authentication_invalid_response",
                "message": "The ZenML authentication endpoint returned an invalid response.",
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
def get_step_logs(
    step_run_id: str,
    source: str | None = None,
    logs_id: str | None = None,
    tail: int | None = None,
) -> dict[str, Any]:
    """Get the logs for a specific step run.

    Returns ``{"logs": [...], "possibly_truncated": bool}`` with entries oldest
    first. At most 50,000 entries are returned. ``possibly_truncated`` is true
    when the step may have more log entries than were returned, and a ``note``
    then says which entries are missing and why. Pass ``tail``
    (for example 200) to get only the newest entries; that is usually where a
    failure shows up, and it keeps the response small. Log stores that can only
    read a log from its start (the default artifact store) return the newest of
    the first 50,000 entries for a longer log, with ``possibly_truncated`` set.

    Args:
        step_run_id: The ID of the step run to get logs for.
        source: Optional log source. Defaults to ZenML's ordinary ``step`` source.
        logs_id: Optional exact log record ID. Cannot be combined with ``source``.
        tail: Optional number of newest entries to return, from 1 to 50000.
    """
    if source is not None:
        source = source.strip()
        if not source:
            raise ResourceDispatchError("source must be a non-empty string.")
    if logs_id is not None:
        logs_id = logs_id.strip()
        if not logs_id:
            raise ResourceDispatchError("logs_id must be a non-empty string.")
    if source is not None and logs_id is not None:
        raise ResourceDispatchError("Only one of source or logs_id may be provided.")
    if source is None and logs_id is None:
        source = "step"
    if tail is not None and not 1 <= tail <= STEP_LOGS_MAX_ENTRIES:
        raise ResourceDispatchError(
            f"tail must be between 1 and {STEP_LOGS_MAX_ENTRIES}."
        )

    # Use the ZenML client's own session: it carries the store's SSL
    # (`verify_ssl`) and User-Agent settings, and its token is shared with
    # every other tool instead of logging in again here.
    client = get_zenml_client()
    session = _rest_session(client)
    if session is None:
        # Without a server URL the client falls back to a local database,
        # which has no REST API to read logs through.
        raise ValueError("ZENML_STORE_URL environment variable not set")
    store = client.zen_store
    fetch_logs = functools.partial(
        make_step_logs_request,
        session,
        store.url,
        step_run_id,
        source=source,
        logs_id=logs_id,
        tail=tail,
    )
    # Like the ZenML client (`RestZenStore._request`), send whatever token the
    # session already has and log in only when the server answers 401, so
    # servers without authentication never need a login. The client's check
    # for another thread having logged in meanwhile isn't needed here: tool
    # calls hold `_zenml_client_call_lock`.
    sent_token = "Authorization" in session.headers
    try:
        return fetch_logs()
    except requests.HTTPError as error:
        if error.response is None or error.response.status_code != 401:
            raise
    # A rejected token (revoked, or the server restarted with a new secret)
    # must not be reused; with no token sent yet, a valid cached one is fine.
    store.authenticate(force=sent_token)
    return fetch_logs()


# =============================================================================
# Generic resource discovery, reads, and ordinary mutations
# =============================================================================


@mcp.tool()
@handle_tool_exceptions
def zenml_describe_resources(
    resource_type: str | None = None,
    operation: str | None = None,
) -> dict[str, Any]:
    """Discover supported generic ZenML resources or one operation schema.

    With no arguments this returns a short catalog. Pass a canonical singular
    resource type to inspect its operations, and add an operation name for its
    bounded input schema and a small example.
    """
    return describe_resources(resource_type=resource_type, operation=operation)


@mcp.tool()
@handle_tool_exceptions
def zenml_list_resources(
    resource_type: str,
    filters: dict[str, Any] | None = None,
    project_id: str | None = None,
    page: int = 1,
    size: int | None = None,
) -> dict[str, Any]:
    """List one allowlisted ZenML resource type with validated filters.

    Use ``zenml_describe_resources(resource_type, "list")`` to discover the
    accepted filters. Page sizes are capped at 200. Project-scoped reads use
    ``project_id`` when supplied and otherwise report the active project used.
    """
    return dispatch_list_resources(
        get_zenml_client(),
        resource_type,
        filters=filters,
        project_id=project_id,
        page=page,
        size=size,
    )


@mcp.tool()
@handle_tool_exceptions
def zenml_get_resource(
    resource_type: str,
    resource_id: str,
    project_id: str | None = None,
    artifact_id: str | None = None,
    model_id: str | None = None,
    pipeline_run_id: str | None = None,
    component_type: str | None = None,
    hydrate: bool | None = None,
) -> dict[str, Any]:
    """Get one allowlisted ZenML resource by its identifier.

    Artifact versions, model versions, and run steps require their parent
    identifier. Stack components require their fixed component type.
    """
    return dispatch_get_resource(
        get_zenml_client(),
        resource_type,
        resource_id,
        project_id=project_id,
        artifact_id=artifact_id,
        model_id=model_id,
        pipeline_run_id=pipeline_run_id,
        component_type=component_type,
        hydrate=hydrate,
    )


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@handle_tool_exceptions
def zenml_create_resource(
    resource_type: str,
    payload: dict[str, Any] | None = None,
    project_id: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Create one allowlisted ZenML resource with a strict typed payload.

    Inspect ``zenml_describe_resources(resource_type, "create")`` first.
    Project-scoped creates require an exact project UUID and never change the
    client's active project.
    """
    ensure_writes_enabled()
    return dispatch_create_resource(
        get_zenml_client(),
        resource_type,
        payload=payload,
        project_id=project_id,
        model_id=model_id,
    )


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@handle_tool_exceptions
def zenml_update_resource(
    resource_type: str,
    resource_id: str,
    payload: dict[str, Any] | None = None,
    project_id: str | None = None,
    artifact_id: str | None = None,
    model_id: str | None = None,
    component_type: str | None = None,
) -> dict[str, Any]:
    """Update one exact UUID through an allowlisted operation-specific payload."""
    ensure_writes_enabled()
    return dispatch_update_resource(
        get_zenml_client(),
        resource_type,
        resource_id,
        payload=payload,
        project_id=project_id,
        artifact_id=artifact_id,
        model_id=model_id,
        component_type=component_type,
    )


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@handle_tool_exceptions
def zenml_delete_resource(
    resource_type: str,
    resource_id: str,
    payload: dict[str, Any] | None = None,
    project_id: str | None = None,
    artifact_id: str | None = None,
    model_id: str | None = None,
    component_type: str | None = None,
) -> dict[str, Any]:
    """Delete or archive one exact UUID using bounded destructive options."""
    ensure_writes_enabled()
    return dispatch_delete_resource(
        get_zenml_client(),
        resource_type,
        resource_id,
        payload=payload,
        project_id=project_id,
        artifact_id=artifact_id,
        model_id=model_id,
        component_type=component_type,
    )


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@handle_tool_exceptions
def zenml_action_resource(
    resource_type: str,
    action: str,
    resource_id: str,
    payload: dict[str, Any] | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Run one finite ZenML lifecycle or relation action.

    Inspect ``zenml_describe_resources(resource_type, "action")`` for the exact
    action names and payload schema. Every identifier must be an exact UUID.
    Actions may have effects outside the ZenML server and are never retried.
    """
    ensure_writes_enabled()
    return dispatch_action_resource(
        get_zenml_client(),
        resource_type,
        action,
        resource_id,
        payload=payload,
        project_id=project_id,
    )


@mcp.resource(uri="resource://zenml_server/resources", mime_type="application/json")
@handle_exceptions
def zenml_resource_catalog() -> str:
    """Return the bounded generic-resource catalog without detailed schemas."""
    return json.dumps(describe_resources())


@mcp.resource(
    uri="resource://zenml_server/resource-schemas/{resource_type}/{operation}",
    mime_type="application/json",
)
@handle_exceptions
def zenml_resource_operation_schema(resource_type: str, operation: str) -> str:
    """Return one bounded generic-resource operation schema."""
    return json.dumps(describe_resources(resource_type, operation))


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
    return cast(dict[str, Any], safe_project(users, resource_type="user"))


@mcp.tool()
@handle_tool_exceptions
def get_user(name_id_or_prefix: str) -> dict[str, Any]:
    """Get detailed information about a specific user.

    Args:
        name_id_or_prefix: The name, ID or prefix of the user to retrieve
    """
    user = get_zenml_client().get_user(name_id_or_prefix)
    return cast(dict[str, Any], safe_project(user, resource_type="user"))


@mcp.tool()
@handle_tool_exceptions
def get_active_user() -> dict[str, Any]:
    """Get the currently active user."""
    user = get_zenml_client().active_user
    return cast(dict[str, Any], safe_project(user, resource_type="user"))


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
    return cast(dict[str, Any], safe_project(service, resource_type="service"))


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
    from zenml.utils.uuid_utils import parse_name_or_uuid

    client = get_zenml_client()
    parsed_identifier = parse_name_or_uuid(name_id_or_prefix)
    if isinstance(parsed_identifier, str):
        exact_matches = client.list_stack_components(
            name=f"equals:{name_id_or_prefix}", size=2, hydrate=False
        )
        if exact_matches.total > 1:
            return _make_error_result(
                "get_stack_component",
                "The identifier matches multiple stack components. Use a full UUID.",
                "AmbiguousIdentifier",
            )
        candidates = list(exact_matches.items)
        known_total = exact_matches.total
        if not candidates:
            prefix_matches = client.list_stack_components(
                logical_operator="or",
                id=f"startswith:{name_id_or_prefix}",
                name=f"startswith:{name_id_or_prefix}",
                size=2,
                hydrate=False,
            )
            candidates = list(prefix_matches.items)
            known_total = prefix_matches.total
    else:
        matches = client.list_stack_components(
            id=name_id_or_prefix, size=2, hydrate=False
        )
        candidates = list(matches.items)
        known_total = matches.total

    if known_total == 0 or not candidates:
        return _make_error_result(
            "get_stack_component",
            "No stack component matches the supplied identifier.",
            "NotFound",
        )
    if known_total > 1 or len(candidates) > 1:
        return _make_error_result(
            "get_stack_component",
            "The identifier matches multiple stack components. Use a full UUID.",
            "AmbiguousIdentifier",
        )

    resolved = candidates[0]
    stack_component = client.get_stack_component(
        component_type=resolved.type,
        name_id_or_prefix=str(resolved.id),
        allow_name_prefix_match=False,
    )
    return cast(
        dict[str, Any],
        safe_project(stack_component, resource_type="stack_component"),
    )


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
    pipeline_name_or_id: str | None = None,
    snapshot_name_or_id: str | None = None,
    stack_name_or_id: str | None = None,
    template_id: str | None = None,
) -> dict[str, Any]:
    """Trigger a pipeline to run from the server.

    Args:
        pipeline_name_or_id: Optional name or ID of the pipeline to trigger. A
            snapshot or template can be triggered without it.
        snapshot_name_or_id: The name or ID of a specific snapshot to run (preferred)
        stack_name_or_id: Optional stack override for the run
        template_id: Deprecated template-based trigger parameter. Use
            `snapshot_name_or_id` for new integrations. The supported ZenML
            version still retains run-template CRUD APIs.

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
            snapshot_name_or_id=<SNAPSHOT_NAME_OR_ID>
        )
        ```
        * Run a specific template (DEPRECATED - use snapshot_name_or_id instead):
        ```python
        trigger_pipeline(template_id=<ID>)
        ```
    """
    ensure_writes_enabled()
    if snapshot_name_or_id is not None and template_id is not None:
        raise ResourceDispatchError(
            "snapshot_name_or_id and template_id are mutually exclusive; provide only one."
        )
    if (
        pipeline_name_or_id is None
        and snapshot_name_or_id is None
        and template_id is None
    ):
        raise ResourceDispatchError(
            "Provide at least one of pipeline_name_or_id, snapshot_name_or_id, or template_id."
        )

    trigger_kwargs: Dict[str, Any] = {}
    if pipeline_name_or_id is not None:
        trigger_kwargs["pipeline_name_or_id"] = pipeline_name_or_id
    if stack_name_or_id is not None:
        trigger_kwargs["stack_name_or_id"] = stack_name_or_id

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
            "Please use `snapshot_name_or_id` instead. The supported ZenML "
            "version retains run-template CRUD APIs, while snapshots are "
            "preferred for new workflows."
        )

    client = get_zenml_client()
    project_id = str(client.active_project.id)
    event_properties = {
        "has_snapshot_id": snapshot_name_or_id is not None,
        "has_template_id": template_id is not None,
        "has_stack_override": stack_name_or_id is not None,
        "used_deprecated_template": used_deprecated_template,
    }
    try:
        pipeline_run = client.trigger_pipeline(**trigger_kwargs)
    except (
        json.JSONDecodeError,
        requests.ReadTimeout,
        requests.ConnectionError,
        requests.exceptions.JSONDecodeError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    ) as error:
        if _is_pre_dispatch_connection_failure(error):
            raise
        reconciliation = {
            "operation": None,
            "resource_type": "pipeline_run",
            "pipeline_name_or_id": pipeline_name_or_id,
            "project_id": project_id,
            "new_run_id": None,
            "reconcilable": False,
            "note": (
                "The source pipeline cannot prove whether a new run was created because "
                "the new run ID is unavailable; do not retry automatically."
            ),
        }
        unknown = {
            "resource_type": "pipeline_run",
            "operation": "trigger",
            "outcome": "unknown",
            "pipeline_name_or_id": pipeline_name_or_id,
            "project_id": project_id,
            "new_run_id": None,
            "reconciliation": reconciliation,
        }
        analytics.track_event(
            "Pipeline Triggered",
            {
                **event_properties,
                "success": False,
                "outcome": "unknown",
                "error_type": "UnknownOutcome",
            },
        )
        return {
            **unknown,
            "error": {
                "tool": "trigger_pipeline",
                "message": (
                    "The response was lost or could not be decoded after the pipeline "
                    "trigger may have been dispatched, so the outcome is unknown. The new "
                    "run ID is unavailable, and the source pipeline cannot prove success; "
                    "do not retry automatically."
                ),
                "type": "UnknownOutcome",
                "details": unknown,
            },
        }
    analytics.track_event(
        "Pipeline Triggered",
        {
            **event_properties,
            "success": True,
            "outcome": "completed",
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

    The supported ZenML version retains run-template CRUD. Snapshots are
    preferred for new workflows; pipeline convenience creation and
    template-based triggering are deprecated.

    Args:
        name_id_or_prefix: The name, ID or prefix of the run template to retrieve
    """
    run_template = get_zenml_client().get_run_template(name_id_or_prefix)
    return {
        "deprecation_notice": (
            "The supported ZenML version retains run-template CRUD. Snapshots are "
            "preferred for new workflows; pipeline convenience creation and "
            "template-based triggering are deprecated."
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

    The supported ZenML version retains run-template CRUD. For new runnable
    configurations, prefer `list_snapshots(runnable=True)`.

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
        tag: Legacy tag filter. The supported ZenML version has no equivalent
            server-side run-template filter, so non-null values are rejected.
    """
    if tag is not None:
        return _make_error_result(
            "list_run_templates",
            "The supported ZenML version does not support tag filtering for run "
            "templates. Use `list_snapshots` with `tag`, or omit this filter.",
            "UnsupportedFilter",
        )
    run_templates = get_zenml_client().list_run_templates(
        sort_by=sort_by,
        page=page,
        size=size,
        created=created,
        updated=updated,
        name=name,
    )
    return {
        "deprecation_notice": (
            "The supported ZenML version retains run-template CRUD. Snapshots are "
            "preferred for new workflows; use `list_snapshots(runnable=True)` "
            "for runnable configurations."
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
        tags=tag,
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
        status: Filter by status (e.g. oneof:["running","error"])
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
        tags=tag,
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

    except ImportError:
        # Handle missing deployer plugin (direct import failure)
        return {
            "error": {
                "tool": "get_deployment_logs",
                "type": "deployer_plugin_not_installed",
                "message": (
                    "The deployer plugin required to fetch logs is not installed. "
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
                        "The deployer's dependencies are not installed.\n\n"
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
        status: Filter by run status (e.g. oneof:["completed","failed"]).
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
        status: Filter by step status (e.g. oneof:["completed","failed"]).
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
        tags=tag,
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
        tags=tag,
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
        tags=tag,
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
        stage: Filter by stage (e.g. oneof:["production","staging"])
        tag: Filter by tag name
    """
    client = get_zenml_client()
    model = client.get_model(model_name_or_id)
    model_versions = client.list_model_versions(
        model=model.id,
        sort_by=sort_by,
        page=page,
        size=size,
        logical_operator=logical_operator,
        created=created,
        updated=updated,
        name=name,
        number=number,
        stage=stage,
        tags=tag,
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
    from zenml.exceptions import DoesNotExistException

    try:
        step_code = get_zenml_client().get_run_step(step_run_id).source_code
    except (KeyError, DoesNotExistException) as error:
        raise ResourceNotFound("Step run not found.") from error
    if step_code is None:
        raise ResourceFeatureUnavailable(
            "Source code is unavailable for this step run."
        )
    return str(step_code)


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

    return (
        "Requested the ZenML pipeline runs dashboard. An MCP Apps-capable host "
        "can render it and load current data. If no interactive view appears, "
        "use zenml_list_resources for pipeline_run and run_step resources, then "
        "use get_step_logs for a selected step."
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
        "Requested the ZenML pipeline run activity chart. An MCP Apps-capable "
        "host can render it and load current data. If no interactive view appears, "
        "use zenml_list_resources for pipeline_run resources with a descending "
        "created-time sort."
    )


for _tool_name in ALL_TOOL_NAMES:
    if _tool_name not in ACTIVE_TOOL_NAMES:
        mcp.remove_tool(_tool_name)


def _enforce_strict_tool_arguments(server: MCPServer) -> None:
    """Harden MCP 2.2's generated argument models against unknown input."""
    if distribution_version("mcp") != "2.2.0":
        raise RuntimeError("Strict tool argument setup requires mcp==2.2.0")
    for tool in server._tool_manager.list_tools():
        argument_model = tool.fn_metadata.arg_model
        argument_model.model_config = ConfigDict(
            **{
                **dict(argument_model.model_config),
                "extra": "forbid",
                "hide_input_in_errors": True,
            }
        )
        argument_model.model_rebuild(force=True)
        tool.parameters = argument_model.model_json_schema(by_alias=True)


_enforce_strict_tool_arguments(mcp)


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


@dataclass(frozen=True)
class HTTPTransportConfig:
    """Runtime settings for the Streamable HTTP transport."""

    host: str = "127.0.0.1"
    port: int = 8000
    disable_dns_rebinding_protection: bool = False
    forwarded_allow_ips: str = "127.0.0.1"


def _origin_matches_strict_allowlist(origin: str, allowed_origins: list[str]) -> bool:
    """Match an Origin without MCP 2.2's wildcard-prefix ambiguity."""
    try:
        parsed = urlparse(origin)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return False
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    base = f"{parsed.scheme}://{host}"
    candidate = base if port is None else f"{base}:{port}"
    return any(
        candidate == allowed
        or (allowed.endswith(":*") and port is not None and base == allowed[:-2])
        for allowed in allowed_origins
    )


class _StrictOriginMiddleware:
    """Reject malformed wildcard-port Origins before MCP's middleware sees them."""

    def __init__(self, app: Any, *, allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", ())
            }
            origin = headers.get("origin")
            if origin and not _origin_matches_strict_allowlist(
                origin, self.allowed_origins
            ):
                from starlette.responses import PlainTextResponse

                response = PlainTextResponse("Invalid Origin header", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_transport_security_settings(config: HTTPTransportConfig) -> Any:
    """Build Host/Origin policy independently from proxy-header trust."""
    from mcp.server.transport_security import TransportSecuritySettings

    if config.disable_dns_rebinding_protection:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    host = config.host.strip()
    unbracketed_host = (
        host[1:-1] if host.startswith("[") and host.endswith("]") else host
    )
    parsed_host: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        parsed_host = ipaddress.ip_address(unbracketed_host)
        is_unspecified = parsed_host.is_unspecified
    except ValueError:
        parsed_host = None
        is_unspecified = False
    try:
        is_unspecified = is_unspecified or socket.inet_aton(unbracketed_host) == bytes(
            4
        )
    except OSError:
        pass
    if not host or is_unspecified:
        raise ValueError(
            f"DNS rebinding protection cannot derive an allowlist from wildcard "
            f"host {config.host!r}. Bind to a concrete host or explicitly pass "
            "--disable-dns-rebinding-protection."
        )

    if unbracketed_host == "localhost" or (
        parsed_host is not None and parsed_host.is_loopback
    ):
        allowed_hosts = [
            f"127.0.0.1:{config.port}",
            f"localhost:{config.port}",
            f"[::1]:{config.port}",
        ]
        allowed_origins = [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ]
        if parsed_host is not None:
            configured_loopback = (
                f"[{parsed_host.compressed}]"
                if isinstance(parsed_host, ipaddress.IPv6Address)
                else parsed_host.compressed
            )
            configured_host = f"{configured_loopback}:{config.port}"
            configured_origin = f"http://{configured_loopback}:*"
            if configured_host not in allowed_hosts:
                allowed_hosts.append(configured_host)
            if configured_origin not in allowed_origins:
                allowed_origins.append(configured_origin)
    else:
        bracketed_host = (
            f"[{unbracketed_host}]" if ":" in unbracketed_host else unbracketed_host
        )
        allowed_hosts = [f"{bracketed_host}:{config.port}"]
        allowed_origins = [
            f"http://{bracketed_host}:*",
            f"https://{bracketed_host}:*",
        ]

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def create_streamable_http_app(config: HTTPTransportConfig) -> Any:
    """Create the v2 ASGI app with its required session-manager lifespan."""
    security = create_transport_security_settings(config)
    app = mcp.streamable_http_app(
        host=config.host,
        transport_security=security,
    )
    if security.enable_dns_rebinding_protection:
        app.add_middleware(
            _StrictOriginMiddleware,
            allowed_origins=security.allowed_origins,
        )
    return app


async def run_streamable_http(config: HTTPTransportConfig) -> None:
    """Serve Streamable HTTP with explicit proxy and lifespan configuration."""
    import uvicorn

    app = create_streamable_http_app(config)
    uvicorn_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.port,
        log_level=logging.getLevelName(log_level).lower(),
        proxy_headers=True,
        forwarded_allow_ips=config.forwarded_allow_ips,
        lifespan="on",
    )
    await uvicorn.Server(uvicorn_config).serve()


def _valid_port(value: str) -> int:
    """Parse a TCP port accepted by the HTTP server CLI."""
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZenML MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol (default: stdio). Use 'streamable-http' for MCP Apps support.",
    )
    parser.add_argument(
        "--port",
        type=_valid_port,
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
    parser.add_argument(
        "--forwarded-allow-ips",
        default=os.getenv("ZENML_MCP_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        help="Comma-separated proxy IPs whose forwarded headers are trusted "
        "(default: 127.0.0.1; env: ZENML_MCP_FORWARDED_ALLOW_IPS).",
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

    if args.transport == "streamable-http":
        try:
            create_transport_security_settings(
                HTTPTransportConfig(
                    host=args.host,
                    port=args.port,
                    disable_dns_rebinding_protection=args.disable_dns_rebinding_protection,
                )
            )
        except ValueError as error:
            parser.error(str(error))

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
            if args.disable_dns_rebinding_protection:
                print(
                    "WARNING: DNS rebinding protection is disabled. "
                    "Only use this behind a trusted reverse proxy.",
                    file=sys.stderr,
                )

            logger.info(
                f"Starting ZenML MCP server on http://{args.host}:{args.port}/mcp"
            )
            asyncio.run(
                run_streamable_http(
                    HTTPTransportConfig(
                        host=args.host,
                        port=args.port,
                        disable_dns_rebinding_protection=args.disable_dns_rebinding_protection,
                        forwarded_allow_ips=args.forwarded_allow_ips,
                    )
                )
            )
        else:
            mcp.run(transport="stdio")
    except Exception as e:
        logger.error("Error running MCP server: %s", type(e).__name__)
        raise SystemExit(1)
