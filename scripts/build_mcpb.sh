#!/usr/bin/env bash
set -euo pipefail

# Determine repository root regardless of where the script is invoked from.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd -P)"

cd "${ROOT}"

VENDOR_DIR="${ROOT}/server/lib"
TEMP_VENDOR_DIR="$(mktemp -d "${ROOT}/server/lib.tmp.XXXXXX")"
MCPB_VERSION="2.1.2"

cleanup() {
  if [[ -n "${TEMP_VENDOR_DIR:-}" && -d "${TEMP_VENDOR_DIR}" ]]; then
    rm -rf -- "${TEMP_VENDOR_DIR}"
  fi
}
trap cleanup EXIT

# Install Python dependencies into a temporary vendored directory first.
# Only replace server/lib after the hashed install succeeds.
uv pip install --require-hashes -r requirements.txt --target "${TEMP_VENDOR_DIR}"
rm -rf -- "${VENDOR_DIR}"
mv -- "${TEMP_VENDOR_DIR}" "${VENDOR_DIR}"
TEMP_VENDOR_DIR=""

# Build the MCP bundle with the exact pinned CLI without installing it globally.
npm exec --yes --package "@anthropic-ai/mcpb@${MCPB_VERSION}" -- mcpb pack

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