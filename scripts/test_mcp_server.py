# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "mcp[cli]",
#     "zenml",
# ]
# ///
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, TypedDict, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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


def _make_tool_info(name: str, description: str | None) -> ToolInfo:
    """Create a ToolInfo TypedDict from values."""
    return {"name": name, "description": description}


def _make_resource_info(uri: Any, name: str, description: str | None) -> ResourceInfo:
    """Create a ResourceInfo TypedDict from values."""
    return {"uri": str(uri), "name": name, "description": description}


def _make_prompt_info(name: str, description: str | None) -> PromptInfo:
    """Create a PromptInfo TypedDict from values."""
    return {"name": name, "description": description}


def _extract_call_tool_text(result: Any) -> str:
    """Flatten MCP call_tool result content into text for error detection.

    MCP tool results have a .content attribute that contains a list of content
    items (typically TextContent with a .text attribute). This function extracts
    and joins all text pieces into a single string for inspection.
    """
    if not hasattr(result, "content") or not result.content:
        return ""

    text_parts: list[str] = []
    for item in result.content:
        # Try to get .text attribute (TextContent)
        if hasattr(item, "text"):
            text_parts.append(item.text)
        else:
            # Fallback to string representation
            text_parts.append(str(item))

    return "\n".join(text_parts)


def _detect_tool_error(tool_name: str, text: str) -> str | None:
    """Detect if tool output looks like an error string from handle_tool_exceptions.

    The server's @handle_tool_exceptions decorator catches exceptions and returns
    them as error message strings (not MCP protocol errors). This function detects
    those patterns so we can properly fail the test.

    Args:
        tool_name: The name of the tool being tested (used for precise error matching)
        text: The text content returned by the tool

    Returns:
        None if the output looks like success, or an error reason string if it
        matches known error patterns.
    """
    if not text:
        return None

    # Normalize: strip leading whitespace that could bypass startswith()
    normalized = text.lstrip()

    # Check for generic exception pattern first (most specific match using tool_name)
    # This catches non-HTTP exceptions like ValueError, client init failures, etc.
    if normalized.startswith(f"Error in {tool_name}:"):
        return normalized[:100] + ("..." if len(normalized) > 100 else "")

    # Fallback: catch any "Error in " pattern (in case of tool name mismatch)
    if normalized.startswith("Error in "):
        return normalized[:100] + ("..." if len(normalized) > 100 else "")

    # HTTP error patterns from handle_tool_exceptions in zenml_server.py
    error_patterns = [
        "Authentication failed",  # HTTP 401
        "Request failed",  # HTTPError (various status codes)
        "Logs not found",  # 404 for get_step_logs
        "Deployment not found or logs unavailable",  # 404 for get_deployment_logs
    ]

    for pattern in error_patterns:
        if normalized.startswith(pattern):
            # Return first 100 chars as the error reason
            return normalized[:100] + ("..." if len(normalized) > 100 else "")

    return None


class MCPSmokeTest:
    def __init__(self, server_path: str):
        """Initialize the smoke test with the server path."""
        self.server_path = Path(server_path)
        # Explicitly pass environment variables to the subprocess
        # This ensures ZENML_STORE_URL, ZENML_STORE_API_KEY, etc. are available
        self.server_params = StdioServerParameters(
            command="uv",
            args=["run", str(self.server_path)],
            env=dict(os.environ),  # Pass all env vars to subprocess
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
        safe_tools_to_test = [
            # Safe tools: read-only, no required parameters, return empty pages when no data
            "list_users",
            "list_stacks",
            "list_pipelines",
            "get_active_project",
            "get_active_user",
            "list_projects",
            "list_snapshots",
            "list_deployments",
            "list_tags",
            "list_builds",
            "list_artifacts",
            # Note: Do NOT add tools that require parameters (e.g., get_artifact_version,
            # list_artifact_versions) since this test calls tools with empty args {}
        ]

        available_tools = {tool["name"] for tool in results["tools"]}
        print(f"🔄 Available tools for testing: {available_tools}")

        for tool_name in safe_tools_to_test:
            if tool_name in available_tools:
                try:
                    print(f"🧪 Testing tool: {tool_name}")
                    print(f"🔄 Calling tool {tool_name}...")
                    # Add timeout to prevent hanging
                    result = await asyncio.wait_for(
                        session.call_tool(tool_name, {}), timeout=30.0
                    )
                    print(f"🔄 Tool {tool_name} returned result")

                    # Extract text content and check for error patterns
                    text_content = _extract_call_tool_text(result)
                    error_reason = _detect_tool_error(tool_name, text_content)

                    if error_reason:
                        # Tool returned an error string (from handle_tool_exceptions)
                        error_msg = f"Tool {tool_name} returned error: {error_reason}"
                        print(f"❌ {error_msg}")
                        results["tool_test_results"][tool_name] = cast(
                            ToolTestResult,
                            {"success": False, "error": error_reason},
                        )
                        results["errors"].append(error_msg)
                    else:
                        # Tool executed successfully with valid content
                        content_length = len(text_content)
                        results["tool_test_results"][tool_name] = cast(
                            ToolTestResult,
                            {"success": True, "content_length": content_length},
                        )
                        print(f"✅ Tool {tool_name} executed successfully")
                except Exception as e:
                    error_msg = f"Tool {tool_name} failed with exception: {e}"
                    print(f"❌ {error_msg}")
                    results["tool_test_results"][tool_name] = cast(
                        ToolTestResult,
                        {"success": False, "error": str(e)},
                    )
                    results["errors"].append(error_msg)
            else:
                print(f"ℹ️  Tool {tool_name} not available in server")

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
        )
        print(f"\nOverall: {'✅ PASS' if overall_status else '❌ FAIL'}")


async def main():
    """Main entry point for the smoke test."""
    if len(sys.argv) != 2:
        print("Usage: python test_mcp_server.py <path_to_mcp_server.py>")
        print("Example: python test_mcp_server.py ./zenml_server.py")
        sys.exit(1)

    server_path = sys.argv[1]

    # Verify server file exists
    if not Path(server_path).exists():
        print(f"❌ Server file not found: {server_path}")
        sys.exit(1)

    smoke_test = MCPSmokeTest(server_path)
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
    )

    if overall_success:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
