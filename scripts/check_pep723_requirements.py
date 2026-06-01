#!/usr/bin/env python3
"""Check that runtime PEP 723 dependencies match requirements.in."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_FILE = REPO_ROOT / "requirements.in"
# Runtime mirrors must match requirements.in exactly. scripts/test_analytics.py is
# intentionally excluded because it is a narrow analytics diagnostic, not a server runtime mirror.
RUNTIME_MIRROR_PEP723_FILES = (
    "server/zenml_server.py",
    "scripts/test_mcp_server.py",
    "scripts/test_datetime_normalization.py",
)


class CheckError(Exception):
    """Raised when a dependency declaration cannot be checked."""


def read_requirements(path: Path) -> list[str]:
    """Read non-empty, non-comment requirement lines from requirements.in."""
    requirements: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            requirements.append(stripped)
    return requirements


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


def read_pep723_dependencies(path: Path) -> list[str]:
    """Extract the dependencies array from a PEP 723 script block."""
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
    return dependencies


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


def check_file(path: Path, expected: list[str]) -> list[str]:
    """Return diagnostics for one file, or an empty list if it matches."""
    try:
        actual = read_pep723_dependencies(path)
    except CheckError as error:
        return [str(error)]

    if set(actual) == set(expected):
        return []

    relative_path = path.relative_to(REPO_ROOT)
    diagnostics = [
        f"PEP 723 dependency drift detected in {relative_path}",
        "",
        *format_dependency_list("Expected from requirements.in:", expected),
        "",
        *format_dependency_list("Actual PEP 723 dependencies:", actual),
        "",
        *mismatch_details(expected, actual),
    ]
    return diagnostics


def main() -> int:
    """Run the drift check."""
    expected = read_requirements(REQUIREMENTS_FILE)
    failures: list[str] = []

    for relative_file in RUNTIME_MIRROR_PEP723_FILES:
        failures.extend(check_file(REPO_ROOT / relative_file, expected))
        if failures and failures[-1] != "":
            failures.append("")

    if failures:
        print("\n".join(failures).rstrip(), file=sys.stderr)
        return 1

    checked_files = ", ".join(RUNTIME_MIRROR_PEP723_FILES)
    print(f"PEP 723 dependencies match requirements.in: {checked_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
