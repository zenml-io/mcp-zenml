#!/usr/bin/env python3
"""Check that PEP 723 script dependencies agree with requirements.in.

Every PEP 723 file under server/ and scripts/ is discovered automatically:
- a file that depends on zenml is a runtime mirror and must list exactly the
  dependencies in requirements.in;
- any other file must pin a requirements.in package exactly as requirements.in
  does;
- a file that depends on mcp must carry the same [tool.uv]
  exclude-newer-package exemption as the server.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_FILE = REPO_ROOT / "requirements.in"
SCAN_GLOBS = ("server/*.py", "scripts/*.py")
REFERENCE_FILE = REPO_ROOT / "server" / "zenml_server.py"
MIRROR_MARKER = "zenml"


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


def check_file(path: Path, expected: list[str], reference_exemption: Any) -> list[str]:
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
        if set(actual) != set(expected):
            diagnostics += [
                f"PEP 723 dependency drift detected in {relative_path}",
                "",
                *format_dependency_list("Expected from requirements.in:", expected),
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

    if "mcp" in actual_by_name and (
        exclude_newer_packages(metadata) != reference_exemption
    ):
        diagnostics.append(
            f"{relative_path}: [tool.uv] exclude-newer-package must match "
            f"{REFERENCE_FILE.relative_to(REPO_ROOT)}: {reference_exemption!r}"
        )
    return diagnostics


def main() -> int:
    """Run the drift check."""
    expected = read_requirements(REQUIREMENTS_FILE)
    reference_exemption = exclude_newer_packages(read_pep723_metadata(REFERENCE_FILE))
    paths = discover_pep723_files()
    failures: list[str] = []

    for path in paths:
        failures.extend(check_file(path, expected, reference_exemption))
        if failures and failures[-1] != "":
            failures.append("")

    if failures:
        print("\n".join(failures).rstrip(), file=sys.stderr)
        return 1

    checked_files = ", ".join(str(path.relative_to(REPO_ROOT)) for path in paths)
    print(f"PEP 723 dependencies agree with requirements.in: {checked_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
