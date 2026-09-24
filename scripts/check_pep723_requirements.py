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

Run with --fix to rewrite the headers to match instead of only reporting, or
with --print-pin NAME to print the exact version pyproject.toml pins NAME to.
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


class CheckError(Exception):
    """Raised when a dependency declaration cannot be checked."""


def exclude_newer_packages(metadata: dict[str, Any]) -> Any:
    """Return the [tool.uv] exclude-newer-package table, if any."""
    return metadata.get("tool", {}).get("uv", {}).get("exclude-newer-package")


def read_pyproject(
    path: Path = PYPROJECT_FILE,
) -> tuple[list[str], dict[str, Any]]:
    """Return pyproject.toml's dependencies and [tool.uv] exclude-newer-package."""
    pyproject = tomllib.loads(path.read_text(encoding="utf-8"))
    dependencies = pyproject.get("project", {}).get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        msg = f"{path}: [project].dependencies must be a non-empty list"
        raise CheckError(msg)
    exemptions = exclude_newer_packages(pyproject)
    if not isinstance(exemptions, dict):
        msg = f"{path}: [tool.uv] exclude-newer-package must be a table"
        raise CheckError(msg)
    return dependencies, exemptions


def dated_exemptions(exemptions: dict[str, Any]) -> dict[str, Any]:
    """Keep only the date-string exemptions, dropping `name = false` entries.

    Only these are copied into files that run on users' machines (PEP 723
    headers, the MCPB bundle). There a `false` entry does nothing, because no
    general exclude-newer cutoff applies, and uv 0.8.x refuses to parse it, so
    the server would not start. Inside the repo, pyproject.toml's full table
    still applies to every `uv run`.
    """
    return {name: value for name, value in exemptions.items() if isinstance(value, str)}


def pinned_version(name: str, path: Path = PYPROJECT_FILE) -> str:
    """Return the exact version pyproject.toml pins `name` to (`name==X`)."""
    dependencies, _ = read_pyproject(path)
    requirement = dependency_map(dependencies).get(name, "")
    match = re.fullmatch(
        r"\s*[A-Za-z0-9_.-]+\s*(?:\[[^\]]*\])?\s*==\s*([^\s,;]+)\s*", requirement
    )
    if match is None:
        msg = f"{path}: no exact {name}==... pin in [project].dependencies"
        raise CheckError(msg)
    return match.group(1)


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


def pep723_block_bounds(lines: list[str], path: Path) -> tuple[int, int]:
    """Return the line indexes of a PEP 723 block's opening and closing markers."""
    stripped = [line.rstrip() for line in lines]
    if "# /// script" not in stripped:
        msg = f"{path}: no PEP 723 script block found"
        raise CheckError(msg)
    start = stripped.index("# /// script")
    try:
        end = stripped.index("# ///", start + 1)
    except ValueError:
        msg = f"{path}: PEP 723 script block is missing its closing '# ///'"
        raise CheckError(msg) from None
    return start, end


def has_pep723_block(path: Path) -> bool:
    """Return whether a file contains a PEP 723 script block."""
    return "# /// script" in path.read_text(encoding="utf-8").splitlines()


def discover_pep723_files() -> list[Path]:
    """Return every PEP 723 file under the scanned directories."""
    candidates = {path for pattern in SCAN_GLOBS for path in REPO_ROOT.glob(pattern)}
    return sorted(path for path in candidates if has_pep723_block(path))


def read_pep723_metadata(lines: list[str], path: Path) -> dict[str, Any]:
    """Parse a file's PEP 723 block and validate its dependencies array."""
    start, end = pep723_block_bounds(lines, path)
    metadata_lines = [
        strip_pep723_comment(line.rstrip(), path) for line in lines[start + 1 : end]
    ]
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


def desired_dependencies(actual: list[str], expected: list[str]) -> list[str]:
    """Return what a file's PEP 723 dependencies should be.

    A file that depends on zenml mirrors pyproject.toml exactly. Any other file
    keeps its own list, with each package pyproject.toml also lists pinned the
    same way.
    """
    if MIRROR_MARKER in dependency_map(actual):
        return expected
    expected_by_name = dependency_map(expected)
    return [expected_by_name.get(dependency_name(item), item) for item in actual]


def needs_exemptions(
    dependencies: list[str], expected: list[str], exemptions: dict[str, Any]
) -> bool:
    """Return whether a file installs a pyproject.toml dependency that is exempt
    from the cooldown, and so must carry the exclude-newer-package table."""
    triggers = exemptions.keys() & dependency_map(expected).keys()
    return bool(triggers & dependency_map(dependencies).keys())


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
        lines = path.read_text(encoding="utf-8").splitlines()
        metadata = read_pep723_metadata(lines, path)
    except CheckError as error:
        return [str(error)]

    actual: list[str] = metadata["dependencies"]
    desired = desired_dependencies(actual, expected)
    relative_path = path.relative_to(REPO_ROOT)
    diagnostics: list[str] = []

    if actual != desired and MIRROR_MARKER in dependency_map(actual):
        diagnostics += [
            f"PEP 723 dependency drift detected in {relative_path}",
            "",
            *format_dependency_list("Expected from pyproject.toml:", expected),
            "",
            *format_dependency_list("Actual PEP 723 dependencies:", actual),
            "",
            *mismatch_details(expected, actual),
        ]
    elif actual != desired:
        diagnostics += [
            f"{relative_path}: {dependency_name(found)} must be pinned as "
            f"{wanted!r}, found {found!r}"
            for found, wanted in zip(actual, desired, strict=True)
            if found != wanted
        ]

    if needs_exemptions(actual, expected, expected_exemptions) and (
        exclude_newer_packages(metadata) != expected_exemptions
    ):
        diagnostics.append(
            f"{relative_path}: [tool.uv] exclude-newer-package must match "
            f"pyproject.toml: {expected_exemptions!r}"
        )
    return diagnostics


def toml_inline_table(table: dict[str, Any]) -> str:
    """Render a flat table of strings and booleans as a TOML inline table.

    JSON strings and booleans are also valid TOML values.
    """

    def key(name: str) -> str:
        return name if re.fullmatch(r"[A-Za-z0-9_]+", name) else json.dumps(name)

    return (
        "{ " + ", ".join(f"{key(k)} = {json.dumps(v)}" for k, v in table.items()) + " }"
    )


def fixed_header(
    lines: list[str],
    dependencies: list[str],
    exemptions: dict[str, Any],
    *,
    add_exemptions: bool,
    path: Path,
) -> list[str]:
    """Return the file's lines with its PEP 723 block rewritten.

    The dependency list becomes `dependencies`, an existing exclude-newer-package
    line becomes `exemptions`, and if there is none and `add_exemptions` is set,
    a [tool.uv] table holding it is added after the dependency list.
    """
    start, end = pep723_block_bounds(lines, path)
    block = lines[start + 1 : end]
    dep_start = block.index("# dependencies = [")
    dep_end = block.index("# ]", dep_start)
    new_dependency_lines = [
        "# dependencies = [",
        *(f"#     {json.dumps(item)}," for item in dependencies),
        "# ]",
    ]
    block[dep_start : dep_end + 1] = new_dependency_lines
    dep_end = dep_start + len(new_dependency_lines) - 1
    exemption_line = f"# exclude-newer-package = {toml_inline_table(exemptions)}"
    existing = [
        i for i, line in enumerate(block) if line.startswith("# exclude-newer-package")
    ]
    if existing:
        block[existing[0]] = exemption_line
    elif add_exemptions:
        block[dep_end + 1 : dep_end + 1] = ["#", "# [tool.uv]", exemption_line]
    return [*lines[: start + 1], *block, *lines[end:]]


def fix_file(path: Path, expected: list[str], exemptions: dict[str, Any]) -> bool:
    """Rewrite one file's PEP 723 header in place; return whether it changed."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    metadata = read_pep723_metadata(lines, path)
    dependencies = desired_dependencies(metadata["dependencies"], expected)
    new_lines = fixed_header(
        lines,
        dependencies,
        exemptions,
        add_exemptions=needs_exemptions(dependencies, expected, exemptions),
        path=path,
    )
    new_text = "\n".join(new_lines)
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
    parser.add_argument(
        "--print-pin",
        metavar="NAME",
        help="print the exact version pyproject.toml pins NAME to, then exit",
    )
    args = parser.parse_args(argv)
    try:
        if args.print_pin:
            print(pinned_version(args.print_pin))
            return 0
        expected, all_exemptions = read_pyproject()
        exemptions = dated_exemptions(all_exemptions)
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
