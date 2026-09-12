#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Resolve repository root (scripts/ -> repo root)
ROOT = Path(__file__).resolve().parents[1]
SERVER_FILE = ROOT / "server" / "zenml_server.py"
MANIFEST_JSON = ROOT / "manifest.json"
sys.path.insert(0, str(ROOT / "server"))

from zenml_tool_catalog import ALL_TOOL_NAMES, tool_names  # noqa: E402


def _decorator_name(node: ast.AST) -> Optional[str]:
    """
    Return a dotted decorator name for calls/attributes, e.g., 'mcp.tool' from @mcp.tool().
    We only need to detect mcp.tool and mcp.prompt.
    """
    target: ast.AST
    if isinstance(node, ast.Call):
        target = node.func
    else:
        target = node

    parts: List[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    parts.reverse()
    return ".".join(parts) if parts else None


def _first_line_doc(fn: ast.FunctionDef) -> str:
    doc = ast.get_docstring(fn) or ""
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def _resolve_prompt_text(fn: ast.FunctionDef) -> Optional[str]:
    """
    Attempt to resolve a static string from the first return statement using ast.literal_eval.
    If it cannot be resolved safely to a string, return None and the caller will warn to stderr.
    """
    for stmt in fn.body:
        if isinstance(stmt, ast.Return) and stmt.value is not None:
            try:
                expr = ast.Expression(body=stmt.value)
                ast.fix_missing_locations(expr)
                value = ast.literal_eval(expr)
                return value if isinstance(value, str) else None
            except Exception:
                return None
    return None


def _collect(server_src: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    tree = ast.parse(server_src, filename=str(SERVER_FILE))
    tools: List[Dict[str, Any]] = []
    prompts: List[Dict[str, Any]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            kinds = {_decorator_name(d) for d in node.decorator_list}
            if "mcp.tool" in kinds:
                tools.append(
                    {
                        "name": node.name,
                        "description": _first_line_doc(node),
                    }
                )
            if "mcp.prompt" in kinds:
                entry: Dict[str, Any] = {
                    "name": node.name,
                    "description": _first_line_doc(node),
                }
                text = _resolve_prompt_text(node)
                if text is None:
                    print(
                        f"Warning: Could not statically resolve text for prompt '{node.name}'",
                        file=sys.stderr,
                    )
                else:
                    entry["text"] = text
                prompts.append(entry)

    return tools, prompts


def _updated_manifest(
    data: Dict[str, Any],
    tools: List[Dict[str, Any]],
    prompts: List[Dict[str, Any]],
    profile: str,
    write_policy: str,
) -> Dict[str, Any]:
    """Return manifest fields aligned with one runtime registration mode."""
    server = {**data["server"]}
    mcp_config = {**server["mcp_config"]}
    env = {
        **mcp_config["env"],
        "ZENML_MCP_PROFILE": profile,
        "ZENML_MCP_WRITE_POLICY": write_policy,
    }
    mcp_config["env"] = env
    server["mcp_config"] = mcp_config
    return {**data, "server": server, "tools": tools, "prompts": prompts}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate profile-aware manifest fields"
    )
    parser.add_argument("--profile", choices=("compact", "legacy"), default="compact")
    parser.add_argument(
        "--write-policy",
        choices=("read_write", "read_only"),
        default="read_write",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not SERVER_FILE.exists():
        print(f"Error: server file not found: {SERVER_FILE}", file=sys.stderr)
        return 1
    if not MANIFEST_JSON.exists():
        print(f"Error: manifest.json not found: {MANIFEST_JSON}", file=sys.stderr)
        return 1

    server_src = SERVER_FILE.read_text(encoding="utf-8")
    candidates, prompts = _collect(server_src)
    by_name = {tool["name"]: tool for tool in candidates}
    if len(by_name) != len(candidates):
        print(
            "Error: duplicate decorated tool names in server entrypoint",
            file=sys.stderr,
        )
        return 1
    candidate_names = set(by_name)
    catalog_names = set(ALL_TOOL_NAMES)
    if candidate_names != catalog_names:
        print(
            "Error: tool catalog and decorated server tools differ: "
            f"missing={sorted(catalog_names - candidate_names)}, "
            f"unexpected={sorted(candidate_names - catalog_names)}",
            file=sys.stderr,
        )
        return 1
    tools = [by_name[name] for name in tool_names(args.profile, args.write_policy)]

    data: Dict[str, Any] = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    # Validate schema before replacing arrays
    if "tools" not in data or "prompts" not in data:
        print(
            'Error: manifest.json is missing required "tools" and/or "prompts" keys',
            file=sys.stderr,
        )
        return 1

    updated = _updated_manifest(data, tools, prompts, args.profile, args.write_policy)

    if args.check:
        if data != updated:
            print(
                "Error: manifest.json profile fields are out of date", file=sys.stderr
            )
            return 1
        print(f"manifest.json is current: {len(tools)} tools, {len(prompts)} prompts")
        return 0

    MANIFEST_JSON.write_text(
        json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Updated manifest.json: {len(tools)} tools, {len(prompts)} prompts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
