#!/usr/bin/env python3
"""
Diagnostic script to test ZenML MCP analytics pipeline.

This script helps debug analytics issues by:
1. Testing direct connectivity to the analytics endpoint
2. Sending test events and checking responses
3. Verifying the event format is correct
4. Testing from within Docker containers

Usage:
    # Test analytics endpoint directly
    python scripts/test_analytics.py --test-endpoint

    # Send a test event (with verbose output)
    python scripts/test_analytics.py --send-test-event

    # Full diagnostic (all tests)
    python scripts/test_analytics.py --full-diagnostic

    # Simulate what Docker container sends (with debug=false for prod Segment)
    python scripts/test_analytics.py --send-test-event --prod
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

# Add parent directory to path so we can import the analytics module
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: uv pip install httpx")
    sys.exit(1)


ANALYTICS_ENDPOINT = "https://analytics.zenml.io/batch"
ANALYTICS_SOURCE_CONTEXT = "mcp-zenml"
TIMEOUT_S = 10.0


def is_ci_environment() -> bool:
    """Check if running in a CI environment."""
    ci_env_vars = [
        "CI", "GITHUB_ACTIONS", "GITLAB_CI", "CIRCLECI",
        "TRAVIS", "JENKINS_URL", "BUILDKITE", "AZURE_PIPELINES"
    ]
    return any(os.getenv(var) for var in ci_env_vars)


def get_server_version() -> str:
    """Get the server version from VERSION file."""
    version_file = Path(__file__).parent.parent / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "unknown"


def build_test_event(event_name: str, debug: bool = True) -> dict:
    """Build a test event in the analytics server format."""
    test_user_id = str(uuid.uuid4())  # Must be a valid UUID for the server
    session_id = str(uuid.uuid4())

    return {
        "type": "track",
        "user_id": test_user_id,
        "event": event_name,
        "properties": {
            "session_id": session_id,
            "is_ci": is_ci_environment(),
            "test_run": True,
            "timestamp": time.time(),
            "server_version": get_server_version(),
            "diagnostic_script": True,
        },
        "debug": debug,
    }


def test_endpoint_connectivity() -> bool:
    """Test basic connectivity to the analytics endpoint."""
    print("\n" + "=" * 60)
    print("TEST 1: Endpoint Connectivity")
    print("=" * 60)

    try:
        # Use the health endpoint for connectivity check
        with httpx.Client(timeout=TIMEOUT_S) as client:
            response = client.get(
                "https://analytics.zenml.io/health",
            )

            print(f"  URL: {ANALYTICS_ENDPOINT}")
            print(f"  Status Code: {response.status_code}")
            print(f"  Response Headers: {dict(response.headers)}")

            if response.status_code in (200, 204, 202):
                print("  ✅ PASS: Endpoint is reachable and accepting requests")
                return True
            else:
                print(f"  ❌ FAIL: Unexpected status code {response.status_code}")
                print(f"  Response body: {response.text[:500]}")
                return False

    except httpx.ConnectError as e:
        print(f"  ❌ FAIL: Connection error - {e}")
        return False
    except httpx.TimeoutException as e:
        print(f"  ❌ FAIL: Timeout - {e}")
        return False
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected error - {type(e).__name__}: {e}")
        return False


def send_test_event(debug: bool = True, verbose: bool = True) -> bool:
    """Send a test event and check the response."""
    mode = "DEV (debug=true)" if debug else "PROD (debug=false)"

    print("\n" + "=" * 60)
    print(f"TEST 2: Send Test Event ({mode})")
    print("=" * 60)

    event = build_test_event("MCP Analytics Diagnostic Test", debug=debug)

    if verbose:
        print(f"  Event payload:")
        print(f"  {json.dumps(event, indent=4)}")

    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            start_time = time.perf_counter()
            response = client.post(
                ANALYTICS_ENDPOINT,
                json=[event],  # Batch of one event
                headers={
                    "Content-Type": "application/json",
                    "Source-Context": ANALYTICS_SOURCE_CONTEXT,
                },
            )
            duration_ms = int((time.perf_counter() - start_time) * 1000)

            print(f"  Status Code: {response.status_code}")
            print(f"  Duration: {duration_ms}ms")
            print(f"  Response: {response.text[:500] if response.text else '(empty)'}")

            if response.status_code in (200, 204, 202):
                print(f"  ✅ PASS: Event accepted by analytics server")
                print(f"  📝 NOTE: Event was sent with debug={debug}")
                if debug:
                    print(
                        "     This routes to DEV Segment - events won't appear in prod!"
                    )
                else:
                    print(
                        "     This routes to PROD Segment - check your Segment dashboard"
                    )
                return True
            else:
                print(f"  ❌ FAIL: Event rejected - {response.status_code}")
                return False

    except Exception as e:
        print(f"  ❌ FAIL: {type(e).__name__}: {e}")
        return False


def test_analytics_module() -> bool:
    """Test the analytics module directly."""
    print("\n" + "=" * 60)
    print("TEST 3: Analytics Module Integration")
    print("=" * 60)

    try:
        import zenml_mcp_analytics as analytics

        print(f"  ANALYTICS_ENABLED: {analytics.ANALYTICS_ENABLED}")
        print(f"  DEV_MODE: {analytics.DEV_MODE}")
        print(f"  ANALYTICS_DEBUG: {analytics.ANALYTICS_DEBUG}")
        print(f"  ANALYTICS_ENDPOINT: {analytics.ANALYTICS_ENDPOINT}")

        if not analytics.ANALYTICS_ENABLED:
            print(f"  ⚠️  Analytics is DISABLED")
            print(f"     Reason: {analytics._disabled_reason}")
            return False

        if analytics.DEV_MODE:
            print("  ⚠️  DEV_MODE is ON - events are logged but NOT sent!")
            print("     Set ZENML_MCP_ANALYTICS_DEV=false to send events")
            return False

        print("  ✅ Analytics module is configured correctly")
        return True

    except ImportError as e:
        print(f"  ❌ FAIL: Cannot import analytics module - {e}")
        return False
    except Exception as e:
        print(f"  ❌ FAIL: {type(e).__name__}: {e}")
        return False


def check_environment() -> None:
    """Print relevant environment variables."""
    print("\n" + "=" * 60)
    print("ENVIRONMENT CHECK")
    print("=" * 60)

    env_vars = [
        "ZENML_MCP_ANALYTICS_ENABLED",
        "ZENML_MCP_DISABLE_ANALYTICS",
        "ZENML_MCP_ANALYTICS_DEV",
        "ZENML_MCP_ANALYTICS_TIMEOUT_S",
        "ZENML_MCP_ANALYTICS_ID",
        "LOGLEVEL",
        "CI",
        "GITHUB_ACTIONS",
    ]

    for var in env_vars:
        value = os.environ.get(var, "(not set)")
        print(f"  {var}: {value}")


def run_full_diagnostic(prod_mode: bool = False) -> bool:
    """Run all diagnostic tests."""
    print("\n" + "#" * 60)
    print("# ZenML MCP Analytics Diagnostic")
    print("#" * 60)
    print(f"# Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"# Server Version: {get_server_version()}")
    print("#" * 60)

    check_environment()

    results = []

    # Test 1: Endpoint connectivity
    results.append(("Endpoint Connectivity", test_endpoint_connectivity()))

    # Test 2: Send test event
    # Use debug=False for prod mode to test actual Segment routing
    results.append(("Send Test Event", send_test_event(debug=not prod_mode)))

    # Test 3: Analytics module
    results.append(("Analytics Module", test_analytics_module()))

    # Summary
    print("\n" + "=" * 60)
    print("DIAGNOSTIC SUMMARY")
    print("=" * 60)

    all_passed = True
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("✅ All tests passed!")
    else:
        print("❌ Some tests failed - see details above")

    return all_passed


def main():
    parser = argparse.ArgumentParser(
        description="Diagnostic script for ZenML MCP analytics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--test-endpoint",
        action="store_true",
        help="Test connectivity to the analytics endpoint",
    )
    parser.add_argument(
        "--send-test-event",
        action="store_true",
        help="Send a test event to the analytics endpoint",
    )
    parser.add_argument(
        "--full-diagnostic",
        action="store_true",
        help="Run full diagnostic (all tests)",
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Use production mode (debug=false) - events go to prod Segment",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Less verbose output",
    )

    args = parser.parse_args()

    # Default to full diagnostic if no specific test requested
    if not (args.test_endpoint or args.send_test_event or args.full_diagnostic):
        args.full_diagnostic = True

    success = True

    if args.full_diagnostic:
        success = run_full_diagnostic(prod_mode=args.prod)
    else:
        if args.test_endpoint:
            success = test_endpoint_connectivity() and success
        if args.send_test_event:
            success = (
                send_test_event(debug=not args.prod, verbose=not args.quiet) and success
            )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
