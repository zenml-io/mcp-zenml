#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "mcp[cli]==2.2.0",
#     "zenml==0.96.4",
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
"""Explicitly gated CRUD receipt against a disposable loopback ZenML server.

The default invocation is credential-free and performs no network or mutation.
An operator must supply a dedicated loopback server and its disposable API key.
Shared or remote ZenML targets are rejected even when the gate is enabled.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))


def _enabled_target() -> str | None:
    if os.getenv("ZENML_MCP_DISPOSABLE_INTEGRATION") != "1":
        return None
    target = os.getenv("ZENML_STORE_URL", "")
    api_key = os.getenv("ZENML_STORE_API_KEY", "")
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise RuntimeError(
            "Disposable mutation integration requires a loopback ZENML_STORE_URL"
        )
    if not api_key:
        raise RuntimeError(
            "Disposable mutation integration requires ZENML_STORE_API_KEY"
        )
    return target


def run_disposable_crud() -> None:
    from zenml.client import Client
    from zenml_resource_dispatch import (
        ResourceNotFound,
        create_resource,
        delete_resource,
        get_resource,
        update_resource,
    )

    def nested_value(value: Any, key: str) -> Any:
        if isinstance(value, dict):
            if key in value:
                return value[key]
            for child in value.values():
                found = nested_value(child, key)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = nested_value(child, key)
                if found is not None:
                    return found
        return None

    client = Client()
    suffix = uuid.uuid4().hex[:12]
    name = f"mcp-disposable-u5-{suffix}"
    created_id: str | None = None
    delete_attempted = False
    try:
        created = create_resource(
            client,
            "project",
            payload={"name": name, "description": "Disposable MCP U5 fixture"},
        )
        created_id = created["resource_id"]
        assert created_id
        persisted = get_resource(client, "project", created_id)
        assert persisted["item"]["id"] == created_id
        updated = update_resource(
            client,
            "project",
            created_id,
            payload={"name": f"{name}-updated"},
        )
        assert updated["outcome"] == "completed"
        persisted_update = get_resource(client, "project", created_id)
        assert nested_value(persisted_update["item"], "name") == f"{name}-updated"
        delete_attempted = True
        deleted_result = delete_resource(client, "project", created_id)
        assert deleted_result["outcome"] == "completed"
        try:
            get_resource(client, "project", created_id)
        except ResourceNotFound:
            pass
        else:
            raise AssertionError("deleted disposable project still exists")
    finally:
        if created_id and not delete_attempted:
            client.delete_project(name_id_or_prefix=created_id)


def test_integration_gate() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert _enabled_target() is None
    with patch.dict(
        os.environ,
        {
            "ZENML_MCP_DISPOSABLE_INTEGRATION": "1",
            "ZENML_STORE_URL": "https://shared.example.com",
            "ZENML_STORE_API_KEY": "disposable-key",
        },
        clear=True,
    ):
        try:
            _enabled_target()
        except RuntimeError as error:
            assert "loopback" in str(error)
        else:
            raise AssertionError("remote integration target was accepted")
    with patch.dict(
        os.environ,
        {
            "ZENML_MCP_DISPOSABLE_INTEGRATION": "1",
            "ZENML_STORE_URL": "http://127.0.0.1:8237",
        },
        clear=True,
    ):
        try:
            _enabled_target()
        except RuntimeError as error:
            assert "API_KEY" in str(error)
        else:
            raise AssertionError("disposable target without a key was accepted")


def main() -> int:
    test_integration_gate()
    print("PASS: disposable integration gate")
    target = _enabled_target()
    if target is None:
        print("SKIP: disposable resource integration is gated; no server was contacted")
        return 0
    run_disposable_crud()
    print(f"PASS: disposable CRUD persisted on loopback target {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
