#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd -P)"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mcp-zenml-bundle.XXXXXX")"
MCPB_VERSION="2.1.2"
UV_VERSION="0.11.28"
TARGET_PATH="${1:-${ROOT}/mcp-zenml.mcpb}"

cleanup() {
  rm -rf -- "${STAGE_DIR}"
}
trap cleanup EXIT

mkdir -p "${STAGE_DIR}/server" "${STAGE_DIR}/assets"
cp "${ROOT}/server/zenml_server.py" "${STAGE_DIR}/server/"
cp "${ROOT}/server/zenml_mcp_analytics.py" "${STAGE_DIR}/server/"
cp "${ROOT}/server/zenml_resource_registry.py" "${STAGE_DIR}/server/"
cp "${ROOT}/server/zenml_resource_dispatch.py" "${STAGE_DIR}/server/"
cp "${ROOT}/server/zenml_tool_catalog.py" "${STAGE_DIR}/server/"
cp -R "${ROOT}/server/ui" "${STAGE_DIR}/server/"
cp "${ROOT}/assets/icon.png" "${STAGE_DIR}/assets/"
cp "${ROOT}/manifest.json" "${STAGE_DIR}/manifest.json"
cp "${ROOT}/VERSION" "${ROOT}/README.md" "${ROOT}/LICENSE" "${STAGE_DIR}/"
cp "${ROOT}/mcpb-uv.lock" "${STAGE_DIR}/uv.lock"

python3 - "${ROOT}" "${STAGE_DIR}" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
stage = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(root / "scripts"))
from check_pep723_requirements import (  # noqa: E402
    dated_exemptions,
    read_pyproject,
    toml_inline_table,
)

manifest_path = stage / "manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
manifest["icon"] = "assets/icon.png"
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

# The bundle uses the same dependency list as the repo's pyproject.toml. The
# exact versions come from mcpb-uv.lock, which the bundle runs with --locked.
dependencies, all_exemptions = read_pyproject(root / "pyproject.toml")
exemptions = dated_exemptions(all_exemptions)
dependency_lines = "".join(f"    {json.dumps(dep)},\n" for dep in dependencies)
version = (stage / "VERSION").read_text(encoding="utf-8").strip()
(stage / "pyproject.toml").write_text(
    f'''[project]
name = "mcp-zenml-bundle"
version = "{version}"
requires-python = ">=3.12,<3.15"
dependencies = [
{dependency_lines}]

[tool.uv]
exclude-newer-package = {toml_inline_table(exemptions)}
''',
    encoding="utf-8",
)
PY

# Default: offline, so an ordinary build reproduces the committed lock exactly.
# MCPB_REFRESH_LOCK=1: online, keeps every locked version that still satisfies
# pyproject.toml and moves only what a dependency change forces (use after
# editing [project].dependencies). MCPB_REFRESH_LOCK=upgrade: online, moves
# every package to its newest allowed version.
case "${MCPB_REFRESH_LOCK:-0}" in
  0) lock_mode=(--offline) ;;
  1) lock_mode=() ;;
  upgrade) lock_mode=(--upgrade) ;;
  *)
    echo "MCPB_REFRESH_LOCK must be 0, 1 or upgrade, got: ${MCPB_REFRESH_LOCK}" >&2
    exit 1
    ;;
esac

uvx --from "uv==${UV_VERSION}" uv lock --project "${STAGE_DIR}" ${lock_mode[@]+"${lock_mode[@]}"}
cp "${STAGE_DIR}/uv.lock" "${ROOT}/mcpb-uv.lock"

export npm_config_cache="${npm_config_cache:-${TMPDIR:-/tmp}/mcpb-npm-cache}"
npm exec --yes --package "@anthropic-ai/mcpb@${MCPB_VERSION}" -- \
  mcpb validate "${STAGE_DIR}/manifest.json"
npm exec --yes --package "@anthropic-ai/mcpb@${MCPB_VERSION}" -- \
  mcpb pack "${STAGE_DIR}" "${TARGET_PATH}"

echo "Cross-platform UV bundle ready: ${TARGET_PATH}"
