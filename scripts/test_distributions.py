#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "mcp[cli]==2.2.0",
# ]
#
# [tool.uv]
# exclude-newer-package = { mcp = "2026-09-08T00:00:00Z", "mcp-types" = "2026-09-08T00:00:00Z", zenml = false }
#
# [tool.ty.rules]
# unresolved-import = "ignore"
#
# [tool.ty.environment]
# extra-paths = ["."]
# ///
"""Verify MCPB and Docker distributions through the MCP stdio protocol."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from check_pep723_requirements import pinned_version, read_pyproject
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Keep this contract independent of the source catalog and generated manifest. A
# packaged server must prove what it actually advertises at runtime.
EXPECTED_COMPACT_READ_WRITE_TOOLS = (
    "diagnose_zenml_setup",
    "get_step_logs",
    "zenml_describe_resources",
    "zenml_list_resources",
    "zenml_get_resource",
    "zenml_create_resource",
    "zenml_update_resource",
    "zenml_delete_resource",
    "zenml_action_resource",
    "get_active_user",
    "get_active_project",
    "trigger_pipeline",
    "get_deployment_logs",
    "get_step_code",
    "open_pipeline_run_dashboard",
    "open_run_activity_chart",
)
# The bundle must declare exactly the repo's runtime dependency list, and the
# installed mcp and zenml versions must match its exact pins.
EXPECTED_BUNDLE_DEPENDENCIES = tuple(read_pyproject()[0])
EXPECTED_PYTHON_REQUIREMENT = ">=3.12,<3.15"
EXPECTED_INSTALLED_VERSIONS = {n: pinned_version(n) for n in ("mcp", "zenml")}
# Calling the .py file directly would make uv honor its PEP 723 block instead of
# this bundle's pyproject.toml and uv.lock. Keep Python as the command boundary.
EXPECTED_MCPB_LAUNCH_ARGS = (
    "run",
    "--project",
    "${__dirname}",
    "--locked",
    "python",
    "${__dirname}/server/zenml_server.py",
)
EXPECTED_MCPB_USER_SETTINGS = {
    "zenml_mcp_profile": ("${user_config.zenml_mcp_profile}", "compact"),
    "zenml_mcp_write_policy": (
        "${user_config.zenml_mcp_write_policy}",
        "read_write",
    ),
}

REQUIRED_PACKAGE_PATHS = (
    "manifest.json",
    "pyproject.toml",
    "uv.lock",
    "VERSION",
    "server/zenml_server.py",
    "server/zenml_mcp_analytics.py",
    "server/zenml_resource_registry.py",
    "server/zenml_resource_dispatch.py",
    "server/zenml_tool_catalog.py",
    "server/ui/pipeline-runs/index.html",
    "server/ui/run-activity-chart/index.html",
)
ALLOWED_PACKAGE_PATHS = frozenset(
    PurePosixPath(path)
    for path in (*REQUIRED_PACKAGE_PATHS, "README.md", "LICENSE", "assets/icon.png")
)
REQUIRED_IMPORTS = (
    "mcp",
    "requests",
    "zenml",
    "zenml_mcp_analytics",
    "zenml_resource_registry",
    "zenml_resource_dispatch",
    "zenml_tool_catalog",
    "zenml_server",
)
LOCAL_IMPORTS = tuple(name for name in REQUIRED_IMPORTS if name.startswith("zenml_"))
REQUIRED_DOCKER_PATHS = tuple(
    path
    for path in REQUIRED_PACKAGE_PATHS
    if path not in {"manifest.json", "pyproject.toml", "uv.lock"}
)

MAX_ARCHIVE_ENTRIES = 100_000
MAX_ARCHIVE_MEMBER_SIZE = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_SIZE = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1_000


class DistributionError(RuntimeError):
    """A packaged distribution did not satisfy its runtime contract."""


def _safe_member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    """Return a normalized archive member path or reject unsafe metadata."""
    name = info.filename
    if not name or "\x00" in name or "\\" in name:
        raise DistributionError(f"unsafe archive member name: {name!r}")

    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DistributionError(f"unsafe archive member path: {name!r}")
    if path.parts[0].endswith(":"):
        raise DistributionError(f"drive-qualified archive member path: {name!r}")

    unix_mode = info.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
        raise DistributionError(f"archive member is not a regular file: {name!r}")
    if info.flag_bits & 0x1:
        raise DistributionError(f"encrypted archive member is unsupported: {name!r}")
    if info.file_size > MAX_ARCHIVE_MEMBER_SIZE:
        raise DistributionError(f"archive member is too large: {name!r}")
    if (
        info.file_size > 1_000_000
        and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO
    ):
        raise DistributionError(
            f"archive member has an unsafe compression ratio: {name!r}"
        )
    return path


def inspect_mcpb_archive(archive: Path) -> tuple[zipfile.ZipInfo, ...]:
    """Inspect an MCPB ZIP without extracting it and reject unsafe contents."""
    if archive.suffix.lower() != ".mcpb":
        raise DistributionError(f"expected an .mcpb archive, got: {archive}")
    if not archive.is_file():
        raise DistributionError(f"MCPB archive does not exist: {archive}")

    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = tuple(bundle.infolist())
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise DistributionError(
                    f"archive contains too many entries: {len(infos):,}"
                )
            seen: set[PurePosixPath] = set()
            total_size = 0
            for info in infos:
                member = _safe_member_path(info)
                normalized = PurePosixPath(str(member).rstrip("/"))
                if normalized in seen:
                    raise DistributionError(
                        f"archive contains a duplicate member: {info.filename!r}"
                    )
                seen.add(normalized)
                total_size += info.file_size
                if total_size > MAX_ARCHIVE_TOTAL_SIZE:
                    raise DistributionError(
                        "archive expands beyond the 2 GiB safety limit"
                    )
            unexpected = sorted(str(path) for path in seen - ALLOWED_PACKAGE_PATHS)
            if unexpected:
                raise DistributionError(
                    "archive contains unexpected members: " + ", ".join(unexpected)
                )
            return infos
    except zipfile.BadZipFile as error:
        raise DistributionError(f"invalid MCPB ZIP archive: {archive}") from error


def _assert_required_members(names: Sequence[str], label: str) -> None:
    available = {name.replace("\\", "/").rstrip("/") for name in names}
    missing = [name for name in REQUIRED_PACKAGE_PATHS if name not in available]
    if missing:
        raise DistributionError(
            f"{label} is missing required packaged paths: {', '.join(missing)}"
        )


def extract_mcpb_archive(archive: Path, destination: Path) -> None:
    """Safely extract an inspected MCPB archive into a new, empty directory."""
    infos = inspect_mcpb_archive(archive)
    _assert_required_members([info.filename for info in infos], str(archive))
    if destination.exists():
        if not destination.is_dir():
            raise DistributionError(
                f"extraction destination is not a directory: {destination}"
            )
        if any(destination.iterdir()):
            raise DistributionError(
                f"extraction destination is not empty: {destination}"
            )
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()

    with zipfile.ZipFile(archive) as bundle:
        for info in infos:
            member = _safe_member_path(info)
            target = destination.joinpath(*member.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                if not target.resolve().is_relative_to(root):
                    raise DistributionError(
                        f"archive member escapes extraction directory: {info.filename!r}"
                    )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.parent.resolve().is_relative_to(root):
                raise DistributionError(
                    f"archive member escapes extraction directory: {info.filename!r}"
                )
            try:
                with bundle.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
            except FileExistsError as error:
                raise DistributionError(
                    f"archive extraction would overwrite a path: {info.filename!r}"
                ) from error


def _write_test_archive(
    path: Path, *, extra: zipfile.ZipInfo | str | None = None, omit: str | None = None
) -> None:
    """Write a small synthetic bundle used by the archive rejection tests."""
    with zipfile.ZipFile(path, "w") as bundle:
        for member in sorted(str(item) for item in ALLOWED_PACKAGE_PATHS):
            if member != omit:
                bundle.writestr(member, b"test")
        if isinstance(extra, zipfile.ZipInfo):
            bundle.writestr(extra, b"target")
        elif extra is not None:
            bundle.writestr(extra, b"test")


def test_archive_rejections() -> None:
    """Exercise the important archive and extraction safety boundaries."""
    with tempfile.TemporaryDirectory(prefix="mcpb-safety-") as temp_dir:
        root = Path(temp_dir)

        cases: list[tuple[str, zipfile.ZipInfo | str]] = [
            ("traversal.mcpb", "../escape"),
            ("unexpected.mcpb", "server/credential_stealer.py"),
        ]
        symlink = zipfile.ZipInfo("server/link")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases.append(("symlink.mcpb", symlink))
        for filename, extra in cases:
            archive = root / filename
            _write_test_archive(archive, extra=extra)
            try:
                inspect_mcpb_archive(archive)
            except DistributionError:
                pass
            else:
                raise AssertionError(f"unsafe archive passed inspection: {filename}")

        duplicate = root / "duplicate.mcpb"
        _write_test_archive(duplicate, extra="manifest.json")
        try:
            inspect_mcpb_archive(duplicate)
        except DistributionError:
            pass
        else:
            raise AssertionError("duplicate archive member passed inspection")

        missing = root / "missing.mcpb"
        _write_test_archive(missing, omit="server/zenml_server.py")
        try:
            _assert_required_members(
                [info.filename for info in inspect_mcpb_archive(missing)], str(missing)
            )
        except DistributionError:
            pass
        else:
            raise AssertionError("archive with a missing server passed inspection")

        _assert_required_members(
            [name.replace("/", "\\") for name in REQUIRED_PACKAGE_PATHS],
            "synthetic Windows directory",
        )

        valid = root / "valid.mcpb"
        _write_test_archive(valid)
        destination = root / "nonempty"
        destination.mkdir()
        (destination / "existing").write_text("keep", encoding="utf-8")
        try:
            extract_mcpb_archive(valid, destination)
        except DistributionError:
            pass
        else:
            raise AssertionError("non-empty extraction destination was accepted")


def _clean_environment() -> dict[str, str]:
    removed = {
        "ZENML_STORE_URL",
        "ZENML_STORE_API_KEY",
        "ZENML_MCP_READ_ONLY",
        "ZENML_MCP_PROFILE",
        "ZENML_MCP_WRITE_POLICY",
        "PYTHONPATH",
    }
    env = {key: value for key, value in os.environ.items() if key not in removed}
    env.update(
        {
            "NO_COLOR": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONNOUSERSITE": "1",
            "ZENML_MCP_ANALYTICS_ENABLED": "false",
            "ZENML_MCP_PROFILE": "compact",
            "ZENML_MCP_WRITE_POLICY": "read_write",
        }
    )
    return env


def _native_failure_hint(details: str) -> str:
    native_markers = (
        "wrong architecture",
        "incompatible architecture",
        "invalid elf",
        "mach-o",
        "dll load failed",
        "undefined symbol",
        "cannot open shared object file",
        "exec format error",
        "no matching manifest",
    )
    if not any(marker in details.lower() for marker in native_markers):
        return ""
    return (
        "\nA native extension is incompatible with the current platform or Python "
        "runtime. Rebuild or resolve the distribution on the target OS, architecture, "
        "and Python minor version."
    )


def _format_process_failure(
    label: str, result: subprocess.CompletedProcess[str]
) -> str:
    details = (result.stderr or result.stdout).strip()
    if len(details) > 4_000:
        details = details[-4_000:]
    suffix = f"\n{details}" if details else ""
    return (
        f"{label} failed with exit code {result.returncode}{suffix}"
        f"{_native_failure_hint(details)}"
    )


def _run_import_check(
    command: Sequence[str], env: dict[str, str], label: str, timeout: float
) -> None:
    try:
        result = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise DistributionError(
            f"required executable was not found: {command[0]}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise DistributionError(
            f"{label} timed out after {timeout:g} seconds"
        ) from error
    if result.returncode != 0:
        raise DistributionError(_format_process_failure(label, result))


async def verify_reproducible_bundle(archive: Path, uv: str, timeout: float) -> None:
    """Compare the committed bundle with a fresh build, then run it."""
    archive = archive.resolve()
    repo_root = archive.parent
    build_script = repo_root / "scripts" / "build_mcpb.sh"
    if not build_script.is_file():
        raise DistributionError(f"bundle build script is missing: {build_script}")
    with tempfile.TemporaryDirectory(prefix="mcpb-reproducible-") as temp_dir:
        root = Path(temp_dir)
        committed_dir = root / "committed"
        generated_dir = root / "generated"
        generated_bundle = root / "generated.mcpb"
        extract_mcpb_archive(archive, committed_dir)
        try:
            result = subprocess.run(
                ["bash", str(build_script), str(generated_bundle)],
                cwd=repo_root,
                env=_clean_environment(),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise DistributionError(
                "fresh MCPB build timed out after 180 seconds"
            ) from error
        if result.returncode != 0:
            raise DistributionError(_format_process_failure("fresh MCPB build", result))
        extract_mcpb_archive(generated_bundle, generated_dir)
        committed_files = {
            path.relative_to(committed_dir): path
            for path in committed_dir.rglob("*")
            if path.is_file()
        }
        generated_files = {
            path.relative_to(generated_dir): path
            for path in generated_dir.rglob("*")
            if path.is_file()
        }
        if committed_files.keys() != generated_files.keys():
            raise DistributionError("committed and generated MCPB member sets differ")
        changed = [
            str(path)
            for path in committed_files
            if committed_files[path].read_bytes() != generated_files[path].read_bytes()
        ]
        if changed:
            raise DistributionError(
                "committed and generated MCPB contents differ: " + ", ".join(changed)
            )
        await verify_unpacked(committed_dir, uv, timeout)


def _unpacked_import_command(bundle_dir: Path, uv: str) -> list[str]:
    code = f"""
import importlib
import importlib.metadata
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "server"))
local_names = set(sys.argv[2].split(","))
for name in sys.argv[3:]:
    module = importlib.import_module(name)
    origin = getattr(module, "__file__", None)
    if name in local_names and (
        origin is None or not pathlib.Path(origin).resolve().is_relative_to(root)
    ):
        raise RuntimeError(f"{{name}} resolved outside packaged contents: {{origin}}")
expected_versions = {EXPECTED_INSTALLED_VERSIONS!r}
actual_versions = {{
    name: importlib.metadata.version(name) for name in expected_versions
}}
if actual_versions != expected_versions:
    raise RuntimeError(
        f"packaged dependency versions differ: {{actual_versions!r}}"
    )
"""
    local_imports = ",".join(LOCAL_IMPORTS)
    return [
        uv,
        "run",
        "--project",
        str(bundle_dir),
        "python",
        "-I",
        "-c",
        code,
        str(bundle_dir),
        local_imports,
        *REQUIRED_IMPORTS,
    ]


def _unpacked_server_parameters(
    bundle_dir: Path, uv: str, launch_args: Sequence[str]
) -> StdioServerParameters:
    resolved_args = [
        arg.replace("${__dirname}", str(bundle_dir)) for arg in launch_args
    ]
    return StdioServerParameters(
        command=uv,
        args=resolved_args,
        env=_clean_environment(),
        cwd=str(bundle_dir),
    )


def _docker_import_command(image: str, docker: str) -> list[str]:
    code = f"""
import importlib
import importlib.metadata
import pathlib
import sys

root = pathlib.Path("/app")
sys.path.insert(0, str(root / "server"))
missing = [path for path in {REQUIRED_DOCKER_PATHS!r} if not (root / path).is_file()]
if missing:
    raise RuntimeError(f"Docker image is missing required packaged paths: {{missing}}")
for name in {REQUIRED_IMPORTS!r}:
    module = importlib.import_module(name)
    origin = getattr(module, "__file__", None)
    if name in {LOCAL_IMPORTS!r} and (
        origin is None or not pathlib.Path(origin).resolve().is_relative_to(root)
    ):
        raise RuntimeError(f"{{name}} resolved outside packaged contents: {{origin}}")
expected_versions = {EXPECTED_INSTALLED_VERSIONS!r}
actual_versions = {{
    name: importlib.metadata.version(name) for name in expected_versions
}}
if actual_versions != expected_versions:
    raise RuntimeError(
        f"packaged dependency versions differ: {{actual_versions!r}}"
    )
"""
    return [
        docker,
        "run",
        "--rm",
        "--network=none",
        "--entrypoint",
        "python",
        image,
        "-I",
        "-c",
        code,
    ]


def _docker_server_parameters(image: str, docker: str) -> StdioServerParameters:
    args = ["run", "--rm", "-i", "--network=none"]
    for key, value in _clean_environment().items():
        if key.startswith("ZENML_") or key == "NO_COLOR":
            args.extend(["-e", f"{key}={value}"])
    args.append(image)
    return StdioServerParameters(command=docker, args=args, env=_clean_environment())


async def _verify_mcp_inventory(
    parameters: StdioServerParameters, label: str, timeout: float
) -> None:
    try:
        async with asyncio.timeout(timeout):
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
    except TimeoutError as error:
        raise DistributionError(
            f"{label} did not initialize and list tools within {timeout:g} seconds"
        ) from error
    except Exception as error:
        raise DistributionError(
            f"{label} failed during MCP stdio startup: {error}"
        ) from error

    actual = tuple(tool.name for tool in result.tools)
    if actual != EXPECTED_COMPACT_READ_WRITE_TOOLS:
        missing = [
            name for name in EXPECTED_COMPACT_READ_WRITE_TOOLS if name not in actual
        ]
        unexpected = [
            name for name in actual if name not in EXPECTED_COMPACT_READ_WRITE_TOOLS
        ]
        order_mismatch = not missing and not unexpected
        raise DistributionError(
            f"{label} advertised the wrong compact/read_write inventory: "
            f"expected {len(EXPECTED_COMPACT_READ_WRITE_TOOLS)}, got {len(actual)}; "
            f"missing={missing}, unexpected={unexpected}, order_mismatch={order_mismatch}; "
            f"actual={list(actual)}"
        )
    if len(actual) != len(set(actual)):
        raise DistributionError(f"{label} advertised duplicate tool names")


async def verify_unpacked(bundle_dir: Path, uv: str, timeout: float) -> None:
    bundle_dir = bundle_dir.resolve()
    if not bundle_dir.is_dir():
        raise DistributionError(f"unpacked MCPB directory does not exist: {bundle_dir}")
    vendored_dependencies = bundle_dir / "server" / "lib"
    if vendored_dependencies.exists():
        raise DistributionError(
            "unpacked MCPB contains server/lib; the cross-platform uv bundle must "
            "resolve dependencies for the target platform instead of shipping native "
            "extensions from the build host"
        )
    _assert_required_members(
        [str(path.relative_to(bundle_dir)) for path in bundle_dir.rglob("*")],
        str(bundle_dir),
    )
    try:
        manifest = json.loads((bundle_dir / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise DistributionError(f"invalid packaged manifest: {error}") from error
    server = manifest.get("server", {})
    if server.get("entry_point") != "server/zenml_server.py":
        raise DistributionError(
            "packaged manifest has an unexpected server entry point"
        )
    if manifest.get("manifest_version") != "0.4" or server.get("type") != "uv":
        raise DistributionError(
            "packaged manifest must declare MCPB 0.4 with server type uv"
        )
    platforms = manifest.get("compatibility", {}).get("platforms")
    if platforms != ["darwin", "win32", "linux"]:
        raise DistributionError(
            "packaged manifest must support darwin, win32, and linux in that order"
        )
    runtime_python = manifest.get("compatibility", {}).get("runtimes", {}).get("python")
    if runtime_python != EXPECTED_PYTHON_REQUIREMENT:
        raise DistributionError(
            "packaged manifest has an unexpected Python runtime requirement"
        )
    mcp_config = server.get("mcp_config", {})
    launch_args = mcp_config.get("args")
    if (
        mcp_config.get("command") != "uv"
        or not isinstance(launch_args, list)
        or tuple(launch_args) != EXPECTED_MCPB_LAUNCH_ARGS
    ):
        raise DistributionError("packaged manifest has an unexpected uv launch command")
    user_config = manifest.get("user_config", {})
    environment = mcp_config.get("env", {})
    for setting, (reference, default) in EXPECTED_MCPB_USER_SETTINGS.items():
        config = user_config.get(setting, {})
        if (
            environment.get(setting.upper()) != reference
            or config.get("type") != "string"
            or config.get("default") != default
        ):
            raise DistributionError(
                f"packaged manifest does not expose configurable {setting}"
            )
    try:
        project = tomllib.loads((bundle_dir / "pyproject.toml").read_text())["project"]
        packaged_version = (bundle_dir / "VERSION").read_text().strip()
    except (KeyError, OSError, tomllib.TOMLDecodeError) as error:
        raise DistributionError(
            f"invalid packaged project metadata: {error}"
        ) from error
    if (
        project.get("version") != packaged_version
        or manifest.get("version") != packaged_version
    ):
        raise DistributionError(
            "packaged VERSION, manifest, and project versions differ"
        )
    if tuple(project.get("dependencies", ())) != EXPECTED_BUNDLE_DEPENDENCIES:
        raise DistributionError("packaged UV dependencies differ from release targets")
    if project.get("requires-python") != EXPECTED_PYTHON_REQUIREMENT:
        raise DistributionError(
            "packaged project has an unexpected Python runtime requirement"
        )

    _run_import_check(
        _unpacked_import_command(bundle_dir, uv),
        _clean_environment(),
        "packaged module import check",
        timeout,
    )
    await _verify_mcp_inventory(
        _unpacked_server_parameters(bundle_dir, uv, launch_args),
        f"unpacked MCPB at {bundle_dir}",
        timeout,
    )


async def verify_docker(image: str, docker: str, timeout: float) -> None:
    _run_import_check(
        _docker_import_command(image, docker),
        _clean_environment(),
        "Docker module import check",
        timeout,
    )
    await _verify_mcp_inventory(
        _docker_server_parameters(image, docker), f"Docker image {image}", timeout
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="target", required=True)

    unpacked = subparsers.add_parser("unpacked", help="test an unpacked MCPB directory")
    unpacked.add_argument("path", type=Path)
    unpacked.add_argument("--uv", default="uv", help="uv executable")
    unpacked.add_argument("--timeout", type=float, default=45.0)

    docker = subparsers.add_parser("docker", help="test a built Docker image")
    docker.add_argument("image")
    docker.add_argument("--docker", default="docker", help="Docker CLI executable")
    docker.add_argument("--timeout", type=float, default=45.0)

    reproducible = subparsers.add_parser(
        "reproducible", help="compare and run a freshly rebuilt MCPB"
    )
    reproducible.add_argument("archive", type=Path)
    reproducible.add_argument("--uv", default="uv", help="uv executable")
    reproducible.add_argument("--timeout", type=float, default=45.0)

    inspect = subparsers.add_parser("inspect", help="safely inspect an MCPB archive")
    inspect.add_argument("archive", type=Path)
    inspect.add_argument(
        "--extract-to",
        type=Path,
        help="safely extract into a new or empty directory after inspection",
    )
    subparsers.add_parser("self-test", help="run archive rejection tests")
    return parser


async def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.target == "unpacked":
            await verify_unpacked(args.path, args.uv, args.timeout)
            print(
                "PASS: unpacked MCPB imports modules and advertises compact/read_write tools"
            )
        elif args.target == "docker":
            await verify_docker(args.image, args.docker, args.timeout)
            print(
                "PASS: Docker image imports modules and advertises compact/read_write tools"
            )
        elif args.target == "reproducible":
            await verify_reproducible_bundle(args.archive, args.uv, args.timeout)
            print("PASS: committed MCPB matches a fresh build and starts correctly")
        elif args.target == "inspect":
            infos = inspect_mcpb_archive(args.archive)
            _assert_required_members(
                [info.filename for info in infos], str(args.archive)
            )
            if args.extract_to is not None:
                extract_mcpb_archive(args.archive, args.extract_to)
            print(f"PASS: safe MCPB archive with {len(infos):,} entries")
        else:
            test_archive_rejections()
            print("PASS: MCPB archive rejection tests")
    except DistributionError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
