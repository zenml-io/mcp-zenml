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
# exclude-newer-package = { mcp = "2026-09-08T00:00:00Z", "mcp-types" = "2026-09-08T00:00:00Z", zenml = false }
#
# [tool.ty.rules]
# # ty >=0.0.62 takes rules from this block, not pyproject.toml. See CLAUDE.md "Note on third-party imports".
# unresolved-import = "ignore"
# ///
import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypedDict, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from zenml_tool_catalog import tool_names  # noqa: E402


class ToolInfo(TypedDict):
    """Type definition for tool information."""

    name: str
    description: str | None


class ResourceInfo(TypedDict):
    """Type definition for resource information."""

    uri: str
    name: str
    description: str | None


class PromptInfo(TypedDict):
    """Type definition for prompt information."""

    name: str
    description: str | None


class ToolTestResult(TypedDict, total=False):
    """Type definition for tool test result."""

    success: bool
    content_length: int
    error: str


class SmokeTestResults(TypedDict):
    """Type definition for smoke test results."""

    connection: bool
    initialization: bool
    tools: list[ToolInfo]
    resources: list[ResourceInfo]
    prompts: list[PromptInfo]
    tool_test_results: dict[str, ToolTestResult]
    errors: list[str]


def _generic_project_id(payload: Mapping[str, Any]) -> str:
    """Validate the generic project-list contract and return its first ID."""
    required_fields = {
        "resource_type",
        "items",
        "total",
        "page",
        "size",
        "effective_scope",
    }
    missing_fields = required_fields - set(payload)
    items = payload.get("items")
    if missing_fields or not isinstance(items, list):
        raise ValueError(
            "generic project list has an invalid shape; "
            f"missing={sorted(missing_fields)}"
        )
    if payload["resource_type"] != "project":
        raise ValueError("generic project list reported the wrong resource type")
    if (
        not items
        or not isinstance(items[0], Mapping)
        or not isinstance(items[0].get("id"), str)
    ):
        raise ValueError("generic project list returned no project identifier")
    return items[0]["id"]


def _validate_generic_project_get(
    kind: str, payload: Mapping[str, Any], project_id: str
) -> None:
    """Validate that generic get returned the exact requested project."""
    item = payload.get("item")
    if (
        kind != "structured"
        or payload.get("resource_type") != "project"
        or not isinstance(item, Mapping)
        or item.get("id") != project_id
    ):
        raise ValueError("generic project get returned an invalid project payload")


def _make_tool_info(name: str, description: str | None) -> ToolInfo:
    """Create a ToolInfo TypedDict from values."""
    return {"name": name, "description": description}


def _make_resource_info(uri: Any, name: str, description: str | None) -> ResourceInfo:
    """Create a ResourceInfo TypedDict from values."""
    return {"uri": str(uri), "name": name, "description": description}


def _make_prompt_info(name: str, description: str | None) -> PromptInfo:
    """Create a PromptInfo TypedDict from values."""
    return {"name": name, "description": description}


def _get_mcp_field(obj: Any, *names: str, default: Any = None) -> Any:
    """Read a field from an MCP result object, trying multiple name variants.

    Handles both camelCase (structuredContent, isError) and snake_case
    (structured_content, is_error) field names across MCP client versions.
    """
    if isinstance(obj, Mapping):
        for n in names:
            if n in obj and obj[n] is not None:
                return obj[n]
        return default
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _extract_call_tool_output(result: Any) -> tuple[str, Any]:
    """Extract output from an MCP call_tool result, supporting both structured and text.

    Returns (kind, payload) where:
    - kind: "structured" if structuredContent is present, "text" otherwise
    - payload: dict for structured, str for text
    """
    # Check for structured content first (new MCP structured output)
    structured = _get_mcp_field(result, "structuredContent", "structured_content")
    if structured is not None:
        return ("structured", structured)

    # Fall back to text content extraction
    if not hasattr(result, "content") or not result.content:
        return ("text", "")

    text_parts: list[str] = []
    for item in result.content:
        if hasattr(item, "text"):
            text_parts.append(item.text)
        else:
            text_parts.append(str(item))

    return ("text", "\n".join(text_parts))


def _is_structured_error_envelope(payload: Any) -> bool:
    """Check if a payload matches the canonical structured error envelope shape.

    The envelope is: {"error": {"tool": str, "message": str, "type": str, ...}}
    Validates the full shape to avoid false positives from legitimate "error" fields.
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


def _detect_tool_error(tool_name: str, kind: str, payload: Any) -> str | None:
    """Detect if tool output represents an error.

    Handles both structured error envelopes ({"error": {"tool", "message", "type"}})
    from structured tools and legacy error string patterns from text-only tools.

    Args:
        tool_name: The name of the tool being tested
        kind: "structured" or "text" (from _extract_call_tool_output)
        payload: dict for structured, str for text

    Returns:
        None if the output looks like success, or an error reason string.
    """
    # Check structured error envelope (full shape validation)
    if kind == "structured" and _is_structured_error_envelope(payload):
        message = payload["error"]["message"]
        return message[:100] + ("..." if len(message) > 100 else "")

    # For structured results without "error" key, it's a success
    if kind == "structured":
        return None

    # Legacy text-based error detection (for text-only tools like easter_egg, get_step_code)
    text = payload if isinstance(payload, str) else str(payload)
    if not text:
        return None

    normalized = text.lstrip()

    # Check for generic exception pattern (most specific match using tool_name)
    if normalized.startswith(f"Error in {tool_name}:"):
        return normalized[:100] + ("..." if len(normalized) > 100 else "")

    # Fallback: catch any "Error in " pattern
    if normalized.startswith("Error in "):
        return normalized[:100] + ("..." if len(normalized) > 100 else "")

    # Error patterns from handle_tool_exceptions / _classify_exception
    error_patterns = [
        "Authentication failed",  # HTTP 401
        "Authorization failed",  # HTTP 403
        "Request failed",  # HTTPError (various status codes)
        "Logs not found",  # 404 for get_step_logs
        "Deployment not found or logs unavailable",  # 404 for get_deployment_logs
        "Missing dependency or integration",  # ImportError / DependencyMissing
        "Missing required environment variable",  # ConfigurationError
        "No project is currently set as active",  # ProjectNotConfigured
        "Authentication to ZenML failed",  # CredentialsNotValid
        "Could not reach ZenML server",  # ConnectionError / Timeout
        "Version mismatch",  # VersionMismatch
        "ZenML server error",  # HTTP 5xx
    ]

    for pattern in error_patterns:
        if normalized.startswith(pattern):
            return normalized[:100] + ("..." if len(normalized) > 100 else "")

    return None


def _required_tools_for_profile(profile: str, write_policy: str) -> frozenset[str]:
    """Return the minimum advertised tool set for a registration profile."""
    if profile not in {"compact", "legacy"}:
        raise ValueError(f"Unknown MCP tool profile: {profile}")
    if write_policy not in {"read_write", "read_only"}:
        raise ValueError(f"Unknown MCP write policy: {write_policy}")
    return frozenset(tool_names(profile, write_policy))  # type: ignore[arg-type]


def _profile_inventory_errors(
    profile: str, write_policy: str, available_tools: set[str]
) -> list[str]:
    """Describe required tools missing from an advertised profile."""
    required_tools = _required_tools_for_profile(profile, write_policy)
    missing_tools = sorted(required_tools - available_tools)
    unexpected_tools = sorted(available_tools - required_tools)
    errors = []
    if missing_tools:
        errors.append(
            f"MCP tool profile {profile!r} is missing required tools: "
            + ", ".join(missing_tools)
        )
    if unexpected_tools:
        errors.append(
            f"MCP tool profile {profile!r} has unexpected tools: "
            + ", ".join(unexpected_tools)
        )
    return errors


class MCPSmokeTest:
    def __init__(
        self,
        server_path: str,
        expected_profile: str = "compact",
        expected_write_policy: str = "read_write",
    ):
        """Initialize the smoke test with the server path."""
        self.server_path = Path(server_path)
        self.expected_profile = expected_profile
        self.expected_write_policy = expected_write_policy
        # Explicitly pass environment variables to the subprocess
        # This ensures ZENML_STORE_URL, ZENML_STORE_API_KEY, etc. are available
        server_env = dict(os.environ)
        server_env["ZENML_MCP_PROFILE"] = expected_profile
        server_env["ZENML_MCP_WRITE_POLICY"] = expected_write_policy
        server_env.pop("ZENML_MCP_READ_ONLY", None)
        self.server_params = StdioServerParameters(
            command="uv",
            args=["run", str(self.server_path)],
            env=server_env,
        )

    async def run_smoke_test(self) -> SmokeTestResults:
        """Run a comprehensive smoke test of the MCP server."""
        results: SmokeTestResults = {
            "connection": False,
            "initialization": False,
            "tools": [],
            "resources": [],
            "prompts": [],
            "tool_test_results": {},
            "errors": [],
        }

        try:
            print(f"🚀 Starting smoke test for MCP server: {self.server_path}")

            # Connect to the server
            async with stdio_client(self.server_params) as (read, write):
                print("✅ Connected to MCP server")
                results["connection"] = True

                async with ClientSession(read, write) as session:
                    # Initialize the session
                    print("🔄 Initializing session...")
                    await asyncio.wait_for(session.initialize(), timeout=60.0)
                    print("✅ Session initialized")
                    results["initialization"] = True

                    # List available tools
                    print("🔄 Listing available tools...")
                    tools_result = await asyncio.wait_for(
                        session.list_tools(), timeout=30.0
                    )
                    print(
                        f"🔄 Got tools result: {len(tools_result.tools) if tools_result.tools else 0} tools"
                    )
                    if tools_result.tools:
                        results["tools"] = [
                            _make_tool_info(tool.name, tool.description)
                            for tool in tools_result.tools
                        ]
                        print(f"✅ Found {len(tools_result.tools)} tools:")
                        for tool in tools_result.tools:
                            print(f"  - {tool.name}: {tool.description}")

                    available_tools = {tool.name for tool in tools_result.tools or []}
                    inventory_errors = _profile_inventory_errors(
                        self.expected_profile,
                        self.expected_write_policy,
                        available_tools,
                    )
                    for error in inventory_errors:
                        print(f"❌ {error}")
                        results["errors"].append(error)

                    # List available resources
                    print("🔄 Listing available resources...")
                    try:
                        resources_result = await asyncio.wait_for(
                            session.list_resources(), timeout=30.0
                        )
                        print(
                            f"🔄 Got resources result: {len(resources_result.resources) if resources_result.resources else 0} resources"
                        )
                        if resources_result.resources:
                            results["resources"] = [
                                _make_resource_info(res.uri, res.name, res.description)
                                for res in resources_result.resources
                            ]
                            print(
                                f"✅ Found {len(resources_result.resources)} resources:"
                            )
                            for res in resources_result.resources:
                                print(f"  - {res.name}: {res.description}")
                    except Exception as e:
                        print(
                            f"ℹ️  No resources available or error listing resources: {e}"
                        )

                    # List available prompts
                    print("🔄 Listing available prompts...")
                    try:
                        prompts_result = await asyncio.wait_for(
                            session.list_prompts(), timeout=30.0
                        )
                        print(
                            f"🔄 Got prompts result: {len(prompts_result.prompts) if prompts_result.prompts else 0} prompts"
                        )
                        if prompts_result.prompts:
                            results["prompts"] = [
                                _make_prompt_info(prompt.name, prompt.description)
                                for prompt in prompts_result.prompts
                            ]
                            print(f"✅ Found {len(prompts_result.prompts)} prompts:")
                            for prompt in prompts_result.prompts:
                                print(f"  - {prompt.name}: {prompt.description}")
                    except Exception as e:
                        print(f"ℹ️  No prompts available or error listing prompts: {e}")

                    # Test a few basic tools (if available)
                    print("🔄 Starting tool tests...")
                    await self._test_basic_tools(session, results)
                    print("✅ Tool tests completed")

        except Exception as e:
            error_msg = f"❌ Error during smoke test: {e}"
            print(error_msg)
            results["errors"].append(error_msg)

        return results

    async def _test_basic_tools(
        self, session: ClientSession, results: SmokeTestResults
    ) -> None:
        """Test basic tools that are likely to be safe to call.

        Safe tools are read-only, don't require entity IDs, and should return
        empty pages (not errors) when no data exists.
        """
        safe_tools_to_test: list[tuple[str, dict[str, Any]]] = [
            # Safe tools: read-only, no required parameters, return empty pages when no data
            ("diagnose_zenml_setup", {}),
            ("zenml_describe_resources", {}),
            (
                "zenml_list_resources",
                {"resource_type": "project", "page": 1, "size": 1},
            ),
            ("list_users", {}),
            ("list_stacks", {}),
            ("list_pipelines", {}),
            ("get_active_project", {}),
            ("get_active_user", {}),
            ("list_projects", {}),
            ("list_snapshots", {}),
            ("list_deployments", {}),
            ("list_tags", {}),
            ("list_builds", {}),
            ("list_artifacts", {}),
            ("open_pipeline_run_dashboard", {}),
            ("open_run_activity_chart", {}),
            # Note: Do NOT add tools that require parameters (e.g., get_artifact_version,
            # list_artifact_versions) since this test calls tools with empty args {}
        ]

        available_tools = {tool["name"] for tool in results["tools"]}
        print(f"🔄 Available tools for testing: {available_tools}")

        generic_project_id: str | None = None
        for tool_name, arguments in safe_tools_to_test:
            if tool_name in available_tools:
                try:
                    print(f"🧪 Testing tool: {tool_name}")
                    print(f"🔄 Calling tool {tool_name}...")
                    # Add timeout to prevent hanging (60s to handle slow CI environments)
                    result = await asyncio.wait_for(
                        session.call_tool(tool_name, arguments), timeout=60.0
                    )
                    print(f"🔄 Tool {tool_name} returned result")

                    # Check MCP-level isError flag first (support both camelCase and snake_case)
                    is_error = _get_mcp_field(
                        result, "isError", "is_error", default=False
                    )
                    if is_error:
                        error_msg = f"Tool {tool_name} returned isError=True"
                        print(f"❌ {error_msg}")
                        results["tool_test_results"][tool_name] = cast(
                            ToolTestResult,
                            {"success": False, "error": error_msg},
                        )
                        results["errors"].append(error_msg)
                        continue

                    # Extract output (structured or text) and check for errors
                    kind, payload = _extract_call_tool_output(result)
                    error_reason = _detect_tool_error(tool_name, kind, payload)

                    if error_reason:
                        error_msg = f"Tool {tool_name} returned error: {error_reason}"
                        print(f"❌ {error_msg}")
                        results["tool_test_results"][tool_name] = cast(
                            ToolTestResult,
                            {"success": False, "error": error_reason},
                        )
                        results["errors"].append(error_msg)
                    else:
                        # Tool executed successfully - compute content length
                        if kind == "structured":
                            content_length = len(json.dumps(payload))
                            if tool_name == "zenml_list_resources":
                                generic_project_id = _generic_project_id(payload)
                            print(
                                f"✅ Tool {tool_name} returned structured output ({content_length} bytes)"
                            )
                        else:
                            content_length = len(payload)
                            print(f"✅ Tool {tool_name} executed successfully")
                        results["tool_test_results"][tool_name] = cast(
                            ToolTestResult,
                            {"success": True, "content_length": content_length},
                        )
                except TimeoutError:
                    error_msg = f"Tool {tool_name} timed out after 60s"
                    print(f"❌ {error_msg}")
                    results["tool_test_results"][tool_name] = cast(
                        ToolTestResult,
                        {"success": False, "error": "timeout"},
                    )
                    results["errors"].append(error_msg)
                except Exception as e:
                    error_msg = f"Tool {tool_name} failed with exception: {e}"
                    print(f"❌ {error_msg}")
                    results["tool_test_results"][tool_name] = cast(
                        ToolTestResult,
                        {"success": False, "error": str(e)},
                    )
                    results["errors"].append(error_msg)

        if generic_project_id and "zenml_get_resource" in available_tools:
            result = await asyncio.wait_for(
                session.call_tool(
                    "zenml_get_resource",
                    {"resource_type": "project", "resource_id": generic_project_id},
                ),
                timeout=60.0,
            )
            kind, payload = _extract_call_tool_output(result)
            error_reason = _detect_tool_error("zenml_get_resource", kind, payload)
            if result.is_error or error_reason:
                error_msg = (
                    "Tool zenml_get_resource returned error: "
                    f"{error_reason or 'isError=True'}"
                )
                results["tool_test_results"]["zenml_get_resource"] = {
                    "success": False,
                    "error": error_msg,
                }
                results["errors"].append(error_msg)
            else:
                try:
                    _validate_generic_project_get(kind, payload, generic_project_id)
                except ValueError:
                    error_msg = (
                        "Tool zenml_get_resource returned an invalid project payload"
                    )
                    results["tool_test_results"]["zenml_get_resource"] = {
                        "success": False,
                        "error": error_msg,
                    }
                    results["errors"].append(error_msg)
                    return
                results["tool_test_results"]["zenml_get_resource"] = {
                    "success": True,
                    "content_length": len(json.dumps(payload)),
                }

    def print_summary(self, results: SmokeTestResults) -> None:
        """Print a summary of the smoke test results."""
        print("\n" + "=" * 50)
        print("🔍 SMOKE TEST SUMMARY")
        print("=" * 50)

        print(f"Connection: {'✅ PASS' if results['connection'] else '❌ FAIL'}")
        print(
            f"Initialization: {'✅ PASS' if results['initialization'] else '❌ FAIL'}"
        )
        print(f"Tools found: {len(results['tools'])}")
        print(f"Resources found: {len(results['resources'])}")
        print(f"Prompts found: {len(results['prompts'])}")

        # Tool test results
        tool_tests_passed = True
        if results["tool_test_results"]:
            successful_tests = sum(
                1 for r in results["tool_test_results"].values() if r.get("success")
            )
            total_tests = len(results["tool_test_results"])
            tool_tests_passed = successful_tests == total_tests
            status = "✅ PASS" if tool_tests_passed else "❌ FAIL"
            print(f"Tool tests: {successful_tests}/{total_tests} passed {status}")

        if results["errors"]:
            print(f"\nErrors ({len(results['errors'])}):")
            for error in results["errors"]:
                print(f"  - {error}")

        # Overall status now includes tool test results
        overall_status = (
            results["connection"]
            and results["initialization"]
            and len(results["tools"]) > 0
            and tool_tests_passed
            and not results["errors"]
        )
        print(f"\nOverall: {'✅ PASS' if overall_status else '❌ FAIL'}")


async def main():
    """Main entry point for the smoke test."""
    parser = argparse.ArgumentParser(description="Smoke-test the ZenML MCP server")
    parser.add_argument("server_path", help="Path to the MCP server entrypoint")
    parser.add_argument(
        "--profile",
        choices=("legacy", "compact"),
        default=os.environ.get("ZENML_MCP_PROFILE", "compact"),
        help="Registration profile whose required tools must be advertised",
    )
    parser.add_argument(
        "--write-policy",
        choices=("read_write", "read_only"),
        default=os.environ.get("ZENML_MCP_WRITE_POLICY", "read_write"),
        help="Write policy whose exact tool inventory must be advertised",
    )
    args = parser.parse_args()
    server_path = args.server_path

    # Verify server file exists
    if not Path(server_path).exists():
        print(f"❌ Server file not found: {server_path}")
        sys.exit(1)

    smoke_test = MCPSmokeTest(
        server_path,
        expected_profile=args.profile,
        expected_write_policy=args.write_policy,
    )
    results = await smoke_test.run_smoke_test()
    smoke_test.print_summary(results)

    # Exit with appropriate code - now includes tool test failures
    # Check if all tool tests passed (or no tools were tested)
    tool_tests_ok = (
        all(r.get("success") for r in results["tool_test_results"].values())
        if results["tool_test_results"]
        else True
    )

    overall_success = (
        results["connection"]
        and results["initialization"]
        and len(results["tools"]) > 0
        and tool_tests_ok
        and not results["errors"]
    )

    if overall_success:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
