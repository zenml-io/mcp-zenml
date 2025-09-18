#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict

# Resolve repository root relative to this script (scripts/ -> repo root)
ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
MANIFEST_JSON = ROOT / "manifest.json"
SERVER_JSON = ROOT / "server.json"

# Valid SemVer regex allowing optional prerelease/build metadata
# Example matches: 1.2.3, 1.2.3-rc.1, 1.2.3+build, 1.2.3-rc.1+build.5
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _read_version_from_file() -> str:
    if not VERSION_FILE.exists():
        print(f"Error: VERSION file not found at {VERSION_FILE}", file=sys.stderr)
        sys.exit(1)
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not version:
        print("Error: VERSION file is empty", file=sys.stderr)
        sys.exit(1)
    return version


def _validate_semver(version: str) -> None:
    if not SEMVER_RE.match(version):
        print(
            f"Error: '{version}' is not a valid SemVer (expected MAJOR.MINOR.PATCH with optional -pre and +build metadata)",
            file=sys.stderr,
        )
        sys.exit(2)


def _write_version_file(version: str) -> None:
    VERSION_FILE.write_text(f"{version}\n", encoding="utf-8")


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        print(f"Error: JSON file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse JSON at {path}: {e}", file=sys.stderr)
        sys.exit(1)


def _dump_json(path: Path, data: Dict[str, Any]) -> None:
    # Always pretty-print with indent=2 and write a trailing newline
    text = json.dumps(data, indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def _update_manifest_version(version: str) -> None:
    manifest = _load_json(MANIFEST_JSON)
    if "version" not in manifest:
        print("Error: manifest.json missing required 'version' field", file=sys.stderr)
        sys.exit(1)
    manifest["version"] = version
    _dump_json(MANIFEST_JSON, manifest)


def _update_server_versions(version: str) -> None:
    server = _load_json(SERVER_JSON)
    if "version" not in server:
        print("Error: server.json missing required 'version' field", file=sys.stderr)
        sys.exit(1)
    if (
        "packages" not in server
        or not isinstance(server["packages"], list)
        or not server["packages"]
    ):
        print(
            "Error: server.json missing a non-empty 'packages' array", file=sys.stderr
        )
        sys.exit(1)
    first_pkg = server["packages"][0]
    if not isinstance(first_pkg, dict):
        print("Error: server.json packages[0] is not an object", file=sys.stderr)
        sys.exit(1)
    if "version" not in first_pkg:
        print("Error: server.json packages[0] missing 'version' field", file=sys.stderr)
        sys.exit(1)

    server["version"] = version
    first_pkg["version"] = version
    _dump_json(SERVER_JSON, server)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate SemVer and propagate version to VERSION, manifest.json, and server.json"
    )
    parser.add_argument(
        "-v",
        "--version",
        help="SemVer to set (e.g., 1.2.3, 1.2.3-rc.1, 1.2.3+build.5). If omitted, read from VERSION.",
    )
    args = parser.parse_args()

    if args.version:
        ver = args.version.strip()
        _validate_semver(ver)
        _write_version_file(ver)
    else:
        ver = _read_version_from_file()
        _validate_semver(ver)

    _update_manifest_version(ver)
    _update_server_versions(ver)

    print(f"Version set to {ver} in VERSION, manifest.json, and server.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
