#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd -P)"
# Start the same ZenML version the server pins in pyproject.toml.
ZENML_VERSION="$(python3 "${ROOT}/scripts/check_pep723_requirements.py" --print-pin zenml)"
if [[ -z "${ZENML_VERSION}" ]]; then
  echo "Could not find an exact \"zenml==...\" pin in ${ROOT}/pyproject.toml" >&2
  exit 1
fi
readonly ROOT ZENML_VERSION
readonly ZENML_HOST="127.0.0.1"
if [[ -n "${ZENML_MCP_INTEGRATION_PORT:-}" ]]; then
  ZENML_PORT="${ZENML_MCP_INTEGRATION_PORT}"
else
  ZENML_PORT="$(python -c 'import socket; sock = socket.socket(); sock.bind(("127.0.0.1", 0)); print(sock.getsockname()[1]); sock.close()')"
fi
readonly ZENML_PORT
readonly ZENML_URL="http://${ZENML_HOST}:${ZENML_PORT}"

integration_root="$(mktemp -d "${RUNNER_TEMP:-/tmp}/mcp-zenml-integration.XXXXXX")"
server_log="${integration_root}/server.log"
server_pid=""

cleanup() {
  result=$?
  trap - EXIT
  if [[ -n "${server_pid}" ]]; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  if [[ "${result}" -ne 0 && -s "${server_log}" ]]; then
    echo "Disposable ZenML server log (last 80 lines):" >&2
    tail -n 80 "${server_log}" >&2
  fi
  rm -rf "${integration_root}"
  exit "${result}"
}
trap cleanup EXIT

export ZENML_CONFIG_PATH="${integration_root}/config"
export ZENML_LOCAL_STORES_PATH="${integration_root}/stores"
export ZENML_ANALYTICS_OPT_IN="false"
export ZENML_AUTO_OPEN_DASHBOARD="false"

uv run --with "zenml[server]==${ZENML_VERSION}" \
  zenml login --local --blocking --ip-address "${ZENML_HOST}" --port "${ZENML_PORT}" \
  >"${server_log}" 2>&1 &
server_pid=$!

server_ready="false"
for _ in $(seq 1 60); do
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    echo "Disposable ZenML server exited before becoming ready" >&2
    exit 1
  fi
  if curl --fail --silent --connect-timeout 1 --max-time 3 \
    "${ZENML_URL}/health" >/dev/null 2>&1; then
    server_ready="true"
    break
  fi
  sleep 1
done

if [[ "${server_ready}" != "true" ]]; then
  echo "Disposable ZenML server did not become ready" >&2
  exit 1
fi

ZENML_STORE_URL="${ZENML_URL}" \
ZENML_STORE_API_KEY="local-server-has-no-authentication" \
ZENML_MCP_DISPOSABLE_INTEGRATION="1" \
  uv run "${ROOT}/scripts/test_resource_integration.py"
