#!/usr/bin/env python3
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
# unresolved-import = "ignore"
#
# [tool.ty.environment]
# extra-paths = ["../server"]
# ///
"""Check credential isolation with the real SDK and disposable CLI state."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_URL = "https://credential-isolation.example.invalid"
PERSONAL_TOKEN = "personal-access-token"
SERVICE_TOKEN = "service-account-access-token"


def _run_worker(
    action: str,
    config_dir: Path,
    api_key: str | None = None,
    disk_caching: str | None = None,
) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ZENML_") and key != "DISABLE_CREDENTIALS_DISK_CACHING"
    }
    env.update(
        {
            "ZENML_CONFIG_PATH": str(config_dir),
            "ZENML_STORE_URL": SERVER_URL,
            "ZENML_ANALYTICS_OPT_IN": "false",
            "ZENML_MCP_ANALYTICS_ENABLED": "false",
            "ZENML_LOGGING_COLORS_DISABLED": "true",
        }
    )
    if api_key is not None:
        env["ZENML_STORE_API_KEY"] = api_key
    if disk_caching is not None:
        env["DISABLE_CREDENTIALS_DISK_CACHING"] = disk_caching
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), action],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _exercise_worker(action: str) -> None:
    config_dir = Path(os.environ["ZENML_CONFIG_PATH"])
    credentials_file = config_dir / "credentials.yaml"
    if action != "seed":
        original = credentials_file.read_bytes()
        sys.path.insert(0, str(ROOT / "server"))
        import zenml_server  # noqa: F401

    from zenml.login.credentials_store import get_credentials_store
    from zenml.models import OAuthTokenResponse

    credentials = get_credentials_store()

    def save_token(value: str) -> None:
        credentials.set_token(
            SERVER_URL,
            OAuthTokenResponse(
                access_token=value, token_type="bearer", expires_in=3600
            ),
        )

    if action == "seed":
        save_token(PERSONAL_TOKEN)
        return

    if action == "isolate":
        assert credentials.get_token(SERVER_URL) is None, (
            "MCP loaded the terminal's personal access token"
        )

        from zenml.login.pro.constants import ZENML_PRO_API_URL
        from zenml.zen_stores.rest_zen_store import RestZenStoreConfiguration

        api_key = os.environ["ZENML_STORE_API_KEY"]
        RestZenStoreConfiguration(url=SERVER_URL, api_key=api_key)
        key_url = ZENML_PRO_API_URL if api_key.startswith("ZENPROKEY_") else SERVER_URL
        assert credentials.get_api_key(key_url) == api_key
        save_token(SERVICE_TOKEN)
        assert credentials_file.read_bytes() == original, (
            "MCP changed the terminal's saved credentials"
        )

        _run_worker("seed", config_dir)
        token = credentials.get_token(SERVER_URL)
        assert token is not None and token.access_token == SERVICE_TOKEN, (
            "A terminal login replaced the MCP session's credentials"
        )
        assert credentials.get_api_key(key_url) == api_key
        assert SERVICE_TOKEN not in credentials_file.read_text()
        assert api_key not in credentials_file.read_text()
    else:
        assert action == "fallback"
        token = credentials.get_token(SERVER_URL)
        assert token is not None and token.access_token == PERSONAL_TOKEN
        save_token("updated-personal-token")
        assert "updated-personal-token" in credentials_file.read_text(), (
            "Connections without an API key must retain CLI credential caching"
        )


def test_api_keys_cannot_share_terminal_credentials() -> None:
    """Workspace and Pro keys ignore personal tokens and never persist secrets."""
    for api_key in ("ZENKEY_test-service-account", "ZENPROKEY_test-service-account"):
        for disk_caching in (None, "false"):
            with tempfile.TemporaryDirectory(prefix="mcp-credentials-") as directory:
                config_dir = Path(directory)
                _run_worker("seed", config_dir)
                _run_worker("isolate", config_dir, api_key, disk_caching)


def test_connections_without_api_keys_retain_cli_credentials() -> None:
    """Absent and empty API keys preserve the intentional zenml login route."""
    for api_key in (None, ""):
        with tempfile.TemporaryDirectory(prefix="mcp-credentials-") as directory:
            config_dir = Path(directory)
            _run_worker("seed", config_dir)
            _run_worker("fallback", config_dir, api_key)


def main() -> None:
    if len(sys.argv) > 1:
        _exercise_worker(sys.argv[1])
        return
    for test in (
        test_api_keys_cannot_share_terminal_credentials,
        test_connections_without_api_keys_retain_cli_credentials,
    ):
        test()
        print(f"PASS: {test.__name__}")


if __name__ == "__main__":
    main()
