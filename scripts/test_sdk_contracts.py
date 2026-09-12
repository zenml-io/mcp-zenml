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
# ///
"""Verify server adapters bind to the pinned ZenML SDK contracts."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import zenml
from zenml.client import Client

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO_ROOT / "server" / "zenml_server.py"
EXPECTED_ZENML_VERSION = "0.96.4"


def _direct_client_calls() -> list[tuple[str, int, list[str], bool, int]]:
    """Return direct Client calls as method, positional count, keywords, kwargs, line."""
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"), filename=str(SERVER_PATH))
    calls: list[tuple[str, int, list[str], bool, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        if not (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Name)
            and receiver.func.id == "get_zenml_client"
        ):
            continue
        keyword_names = [keyword.arg for keyword in node.keywords if keyword.arg]
        has_expansion = any(keyword.arg is None for keyword in node.keywords)
        calls.append(
            (
                node.func.attr,
                len(node.args),
                keyword_names,
                has_expansion,
                node.lineno,
            )
        )
    return calls


def test_direct_calls_bind() -> None:
    """Every statically declared Client call binds to the released signature."""
    failures: list[str] = []
    for (
        method_name,
        positional_count,
        keywords,
        has_expansion,
        line,
    ) in _direct_client_calls():
        method = getattr(Client, method_name, None)
        if method is None:
            failures.append(f"line {line}: Client.{method_name} does not exist")
            continue
        if has_expansion:
            continue
        signature = inspect.signature(method)
        try:
            signature.bind(
                object(),
                *([object()] * positional_count),
                **dict.fromkeys(keywords, object()),
            )
        except TypeError as error:
            failures.append(f"line {line}: Client.{method_name}{signature}: {error}")
    assert not failures, "\n".join(failures)


def test_dynamic_calls_bind() -> None:
    """The trigger variants assembled at runtime bind to the released signature."""
    signature = inspect.signature(Client.trigger_pipeline)
    for kwargs in (
        {"pipeline_name_or_id": "pipeline", "stack_name_or_id": None},
        {
            "pipeline_name_or_id": "pipeline",
            "stack_name_or_id": "stack",
            "snapshot_name_or_id": "snapshot",
        },
        {
            "pipeline_name_or_id": "pipeline",
            "stack_name_or_id": None,
            "template_id": "template",
        },
    ):
        signature.bind(object(), **kwargs)


def test_expected_released_signatures() -> None:
    """Critical drift-sensitive parameters remain present in ZenML 0.96.4."""
    assert zenml.__version__ == EXPECTED_ZENML_VERSION
    expected_parameters = {
        "list_snapshots": {"tags"},
        "list_deployments": {"tags"},
        "list_artifacts": {"tags"},
        "list_artifact_versions": {"artifact", "tags"},
        "list_models": {"tags"},
        "list_model_versions": {"model", "tags"},
        "list_run_templates": set(),
        "get_stack_component": {"component_type", "name_id_or_prefix"},
    }
    for method_name, required_names in expected_parameters.items():
        parameters = inspect.signature(getattr(Client, method_name)).parameters
        assert required_names <= set(parameters), method_name
        assert "tag" not in parameters, method_name


def main() -> int:
    tests: list[tuple[str, Any]] = [
        ("test_direct_calls_bind", test_direct_calls_bind),
        ("test_dynamic_calls_bind", test_dynamic_calls_bind),
        ("test_expected_released_signatures", test_expected_released_signatures),
    ]
    for name, test in tests:
        test()
        print(f"PASS: {name}")
    print(f"All {len(tests)} SDK contract tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
