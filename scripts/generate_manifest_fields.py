#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Resolve repository root (scripts/ -> repo root)
ROOT = Path(__file__).resolve().parents[1]
SERVER_FILE = ROOT / "server" / "zenml_server.py"
MANIFEST_JSON = ROOT / "manifest.json"


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


def main() -> int:
    if not SERVER_FILE.exists():
        print(f"Error: server file not found: {SERVER_FILE}", file=sys.stderr)
        return 1
    if not MANIFEST_JSON.exists():
        print(f"Error: manifest.json not found: {MANIFEST_JSON}", file=sys.stderr)
        return 1

    server_src = SERVER_FILE.read_text(encoding="utf-8")
    tools, prompts = _collect(server_src)

    data: Dict[str, Any] = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    # Validate schema before replacing arrays
    if "tools" not in data or "prompts" not in data:
        print(
            'Error: manifest.json is missing required "tools" and/or "prompts" keys',
            file=sys.stderr,
        )
        return 1

    data["tools"] = tools
    data["prompts"] = prompts

    MANIFEST_JSON.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Updated manifest.json: {len(tools)} tools, {len(prompts)} prompts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
