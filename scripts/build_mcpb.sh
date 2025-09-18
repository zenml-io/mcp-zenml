#!/usr/bin/env bash
set -euo pipefail

# Determine repository root regardless of where the script is invoked from.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd -P)"

cd "${ROOT}"

# Install Python dependencies into the vendored server/lib directory.
uv pip install -r requirements.txt --target server/lib

# Install the MCP bundler CLI globally.
npm install --global @anthropic-ai/mcpb

# Build the MCP bundle.
mcpb pack

# Find the first generated .mcpb file (prefer the most recently modified).
# Use a non-failing capture to avoid set -e aborting if no files are found.
set +e
BUNDLE_PATH="$(ls -1t ./*.mcpb 2>/dev/null | head -n 1)"
set -e

if [[ -z "${BUNDLE_PATH}" ]]; then
  echo "Error: No .mcpb bundle produced by 'mcpb pack'." >&2
  exit 1
fi

BUNDLE_NAME="$(basename -- "${BUNDLE_PATH}")"
TARGET_NAME="mcp-zenml.mcpb"

# If the generated name differs from our canonical name, rename it.
if [[ "${BUNDLE_NAME}" != "${TARGET_NAME}" ]]; then
  mv -f -- "${BUNDLE_PATH}" "${TARGET_NAME}"
  BUNDLE_PATH="${ROOT}/${TARGET_NAME}"
fi

echo "Bundle ready: ${BUNDLE_PATH}"