# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "mcp[cli]",
#     "zenml",
# ]
# ///
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPSmokeTest:
    def __init__(self, server_path: str):
        """Initialize the smoke test with the server path."""
        self.server_path = Path(server_path)
        self.server_params = StdioServerParameters(
            command="uv",
            args=["run", str(self.server_path)],
        )

    async def run_smoke_test(self) -> Dict[str, Any]:
        """Run a comprehensive smoke test of the MCP server."""
        results = {
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
                            {"name": tool.name, "description": tool.description}
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
                                {
                                    "uri": res.uri,
                                    "name": res.name,
                                    "description": res.description,
                                }
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
                                {"name": prompt.name, "description": prompt.description}
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

    async def _test_basic_tools(self, session: ClientSession, results: Dict[str, Any]):
        """Test basic tools that are likely to be safe to call."""
        safe_tools_to_test = [
            "list_users",
            "list_stacks",
            "list_pipelines",
            "get_server_info",
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
                    results["tool_test_results"][tool_name] = {
                        "success": True,
                        "content_length": len(str(result.content))
                        if result.content
                        else 0,
                    }
                    print(f"✅ Tool {tool_name} executed successfully")
                except Exception as e:
                    error_msg = f"Tool {tool_name} failed: {e}"
                    print(f"❌ {error_msg}")
                    results["tool_test_results"][tool_name] = {
                        "success": False,
                        "error": str(e),
                    }
            else:
                print(f"ℹ️  Tool {tool_name} not available in server")

    def print_summary(self, results: Dict[str, Any]):
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

        if results["tool_test_results"]:
            successful_tests = sum(
                1 for r in results["tool_test_results"].values() if r["success"]
            )
            total_tests = len(results["tool_test_results"])
            print(f"Tool tests: {successful_tests}/{total_tests} passed")

        if results["errors"]:
            print(f"Errors: {len(results['errors'])}")
            for error in results["errors"]:
                print(f"  - {error}")

        overall_status = (
            results["connection"]
            and results["initialization"]
            and len(results["tools"]) > 0
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

    # Exit with appropriate code
    if results["connection"] and results["initialization"]:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
