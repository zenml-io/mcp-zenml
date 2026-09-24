#!/usr/bin/env python3
"""Check that PEP 723 script dependencies agree with pyproject.toml.

`[project].dependencies` in pyproject.toml is the one hand-edited list of
runtime dependencies. Every PEP 723 file under server/ and scripts/ is
discovered automatically:
- a file that depends on zenml is a runtime mirror and must list exactly the
  pyproject.toml dependencies, in the same order;
- any other file must pin a shared package exactly as pyproject.toml does;
- a file that depends on mcp or zenml must carry the same [tool.uv]
  exclude-newer-package table as pyproject.toml.

Run with --fix to rewrite the headers to match instead of only reporting.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_FILE = REPO_ROOT / "pyproject.toml"
SCAN_GLOBS = ("server/*.py", "scripts/*.py")
MIRROR_MARKER = "zenml"
EXEMPTION_TRIGGERS = frozenset({"mcp", "zenml"})


class CheckError(Exception):
    """Raised when a dependency declaration cannot be checked."""


def read_pyproject(path: Path) -> tuple[list[str], dict[str, Any]]:
    """Return pyproject.toml's dependencies and [tool.uv] exclude-newer-package."""
    pyproject = tomllib.loads(path.read_text(encoding="utf-8"))
    dependencies = pyproject.get("project", {}).get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        msg = f"{path}: [project].dependencies must be a non-empty list"
        raise CheckError(msg)
    exemptions = pyproject.get("tool", {}).get("uv", {}).get("exclude-newer-package")
    if not isinstance(exemptions, dict):
        msg = f"{path}: [tool.uv] exclude-newer-package must be a table"
        raise CheckError(msg)
    return dependencies, exemptions


def dependency_name(requirement: str) -> str:
    """Return the normalized package name from a dependency string."""
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    if match is None:
        return requirement.strip().lower()
    return match.group(1).replace("_", "-").lower()


def dependency_map(requirements: list[str]) -> dict[str, str]:
    """Map normalized package names to their full dependency strings."""
    return {dependency_name(requirement): requirement for requirement in requirements}


def strip_pep723_comment(line: str, path: Path) -> str:
    """Strip one PEP 723 comment marker from a metadata line."""
    if line == "#":
        return ""
    if line.startswith("# "):
        return line[2:]
    if line.startswith("#"):
        return line[1:]
    msg = f"{path}: PEP 723 metadata line is not a comment: {line!r}"
    raise CheckError(msg)


def has_pep723_block(path: Path) -> bool:
    """Return whether a file contains a PEP 723 script block."""
    return "# /// script" in path.read_text(encoding="utf-8").splitlines()


def discover_pep723_files() -> list[Path]:
    """Return every PEP 723 file under the scanned directories."""
    candidates = {path for pattern in SCAN_GLOBS for path in REPO_ROOT.glob(pattern)}
    return sorted(path for path in candidates if has_pep723_block(path))


def read_pep723_metadata(path: Path) -> dict[str, Any]:
    """Parse a PEP 723 block and validate its dependencies array."""
    metadata_lines: list[str] = []
    in_block = False

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if not in_block:
            if line == "# /// script":
                in_block = True
            continue

        if line == "# ///":
            break
        metadata_lines.append(strip_pep723_comment(line, path))
    else:
        if in_block:
            msg = f"{path}: PEP 723 script block is missing its closing '# ///'"
        else:
            msg = f"{path}: no PEP 723 script block found"
        raise CheckError(msg)

    try:
        metadata: dict[str, Any] = tomllib.loads("\n".join(metadata_lines))
    except tomllib.TOMLDecodeError as error:
        msg = f"{path}: failed to parse PEP 723 metadata as TOML: {error}"
        raise CheckError(msg) from error

    dependencies = metadata.get("dependencies")
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        msg = f"{path}: PEP 723 'dependencies' must be a list of strings"
        raise CheckError(msg)
    return metadata


def exclude_newer_packages(metadata: dict[str, Any]) -> Any:
    """Return the [tool.uv] exclude-newer-package table, if any."""
    return metadata.get("tool", {}).get("uv", {}).get("exclude-newer-package")


def format_dependency_list(title: str, dependencies: list[str]) -> list[str]:
    """Format a dependency list for human-readable diagnostics."""
    lines = [title]
    lines.extend(f"  {dependency}" for dependency in dependencies)
    return lines


def mismatch_details(expected: list[str], actual: list[str]) -> list[str]:
    """Build detailed dependency drift diagnostics."""
    details: list[str] = []
    expected_by_name = dependency_map(expected)
    actual_by_name = dependency_map(actual)
    expected_names = set(expected_by_name)
    actual_names = set(actual_by_name)

    missing_names = expected_names - actual_names
    extra_names = actual_names - expected_names
    changed_constraints = [
        name
        for name in sorted(expected_names & actual_names)
        if expected_by_name[name] != actual_by_name[name]
    ]

    if changed_constraints:
        details.append("Changed constraint:")
        for name in changed_constraints:
            details.append(
                f"  {name}: expected {expected_by_name[name]!r}, "
                f"found {actual_by_name[name]!r}"
            )

    if missing_names:
        details.append("Missing dependencies:")
        details.extend(f"  {expected_by_name[name]}" for name in sorted(missing_names))

    if extra_names:
        details.append("Extra dependencies:")
        details.extend(f"  {actual_by_name[name]}" for name in sorted(extra_names))

    return details


def check_file(
    path: Path, expected: list[str], expected_exemptions: dict[str, Any]
) -> list[str]:
    """Return diagnostics for one file, or an empty list if it agrees."""
    try:
        metadata = read_pep723_metadata(path)
    except CheckError as error:
        return [str(error)]

    actual: list[str] = metadata["dependencies"]
    actual_by_name = dependency_map(actual)
    relative_path = path.relative_to(REPO_ROOT)
    diagnostics: list[str] = []

    if MIRROR_MARKER in actual_by_name:
        if actual != expected:
            diagnostics += [
                f"PEP 723 dependency drift detected in {relative_path}",
                "",
                *format_dependency_list("Expected from pyproject.toml:", expected),
                "",
                *format_dependency_list("Actual PEP 723 dependencies:", actual),
                "",
                *mismatch_details(expected, actual),
            ]
    else:
        expected_by_name = dependency_map(expected)
        diagnostics += [
            f"{relative_path}: {name} must be pinned as "
            f"{expected_by_name[name]!r}, found {requirement!r}"
            for name, requirement in actual_by_name.items()
            if name in expected_by_name and requirement != expected_by_name[name]
        ]

    if EXEMPTION_TRIGGERS & actual_by_name.keys() and (
        exclude_newer_packages(metadata) != expected_exemptions
    ):
        diagnostics.append(
            f"{relative_path}: [tool.uv] exclude-newer-package must match "
            f"pyproject.toml: {expected_exemptions!r}"
        )
    return diagnostics


def toml_inline_table(table: dict[str, Any]) -> str:
    """Render a flat table of strings and booleans as a TOML inline table."""

    def key(name: str) -> str:
        return name if re.fullmatch(r"[A-Za-z0-9_]+", name) else json.dumps(name)

    def value(item: Any) -> str:
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, str):
            return json.dumps(item)
        msg = f"cannot render {item!r} in an exclude-newer-package table"
        raise CheckError(msg)

    return "{ " + ", ".join(f"{key(k)} = {value(v)}" for k, v in table.items()) + " }"


def fixed_header(
    lines: list[str], path: Path, expected: list[str], exemptions: dict[str, Any]
) -> list[str]:
    """Return the PEP 723 block lines rewritten to match pyproject.toml."""
    metadata = read_pep723_metadata(path)
    actual: list[str] = metadata["dependencies"]
    if MIRROR_MARKER in dependency_map(actual):
        dependencies = expected
    else:
        expected_by_name = dependency_map(expected)
        dependencies = [
            expected_by_name.get(dependency_name(item), item) for item in actual
        ]
    needs_exemptions = bool(EXEMPTION_TRIGGERS & dependency_map(dependencies).keys())

    start = lines.index("# /// script")
    end = lines.index("# ///", start + 1)
    block = lines[start + 1 : end]
    dep_start = block.index("# dependencies = [")
    dep_end = block.index("# ]", dep_start)
    block[dep_start : dep_end + 1] = [
        "# dependencies = [",
        *(f"#     {json.dumps(item)}," for item in dependencies),
        "# ]",
    ]
    exemption_line = f"# exclude-newer-package = {toml_inline_table(exemptions)}"
    existing = [
        i for i, line in enumerate(block) if line.startswith("# exclude-newer-package")
    ]
    if existing:
        block[existing[0]] = exemption_line
    elif needs_exemptions:
        dep_end = block.index("# ]", dep_start)
        block[dep_end + 1 : dep_end + 1] = ["#", "# [tool.uv]", exemption_line]
    return [*lines[: start + 1], *block, *lines[end:]]


def fix_file(path: Path, expected: list[str], exemptions: dict[str, Any]) -> bool:
    """Rewrite one file's PEP 723 header in place; return whether it changed."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    new_text = "\n".join(fixed_header(lines, path, expected, exemptions))
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    """Run the drift check, or rewrite the headers with --fix."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fix",
        action="store_true",
        help="rewrite PEP 723 dependency lists and [tool.uv] tables to match",
    )
    args = parser.parse_args(argv)
    try:
        expected, exemptions = read_pyproject(PYPROJECT_FILE)
    except CheckError as error:
        print(error, file=sys.stderr)
        return 1
    paths = discover_pep723_files()

    if args.fix:
        for path in paths:
            if fix_file(path, expected, exemptions):
                print(f"Updated {path.relative_to(REPO_ROOT)}")

    failures: list[str] = []
    for path in paths:
        failures.extend(check_file(path, expected, exemptions))
        if failures and failures[-1] != "":
            failures.append("")

    if failures:
        print("\n".join(failures).rstrip(), file=sys.stderr)
        if not args.fix:
            print(
                "\nRun `uv run scripts/check_pep723_requirements.py --fix` "
                "to rewrite the headers.",
                file=sys.stderr,
            )
        return 1

    checked_files = ", ".join(str(path.relative_to(REPO_ROOT)) for path in paths)
    print(f"PEP 723 dependencies agree with pyproject.toml: {checked_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
