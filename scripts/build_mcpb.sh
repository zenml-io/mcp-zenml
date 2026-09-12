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

python3 - "${STAGE_DIR}" <<'PY'
import json
import pathlib
import sys

stage = pathlib.Path(sys.argv[1])
manifest_path = stage / "manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
manifest["icon"] = "assets/icon.png"
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

version = (stage / "VERSION").read_text(encoding="utf-8").strip()
(stage / "pyproject.toml").write_text(
    f'''[project]
name = "mcp-zenml-bundle"
version = "{version}"
requires-python = ">=3.12,<3.15"
dependencies = [
    "httpx==0.28.1",
    "mcp[cli]==2.2.0",
    "zenml==0.96.4",
    "setuptools==82.0.1",
    "requests==2.32.5",
]
''',
    encoding="utf-8",
)
PY

lock_mode=(--offline)
if [[ "${MCPB_REFRESH_LOCK:-0}" == "1" ]]; then
  lock_mode=(--upgrade)
fi

uvx --from "uv==${UV_VERSION}" uv lock --project "${STAGE_DIR}" "${lock_mode[@]}" \
  --exclude-newer-package "mcp=2026-09-08T00:00:00Z" \
  --exclude-newer-package "mcp-types=2026-09-08T00:00:00Z"
cp "${STAGE_DIR}/uv.lock" "${ROOT}/mcpb-uv.lock"

export npm_config_cache="${npm_config_cache:-${TMPDIR:-/tmp}/mcpb-npm-cache}"
npm exec --yes --package "@anthropic-ai/mcpb@${MCPB_VERSION}" -- \
  mcpb validate "${STAGE_DIR}/manifest.json"
npm exec --yes --package "@anthropic-ai/mcpb@${MCPB_VERSION}" -- \
  mcpb pack "${STAGE_DIR}" "${TARGET_PATH}"

echo "Cross-platform UV bundle ready: ${TARGET_PATH}"
