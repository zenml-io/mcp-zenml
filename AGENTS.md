# Repository Guidelines

## Project Structure & Module Organization
- `server/` – MCP server implementation. Main entry: `server/zenml_server.py`; analytics: `server/analytics.py`; treat `server/lib/` as vendored support code (avoid edits unless necessary).
- `scripts/` – Developer utilities: `format.sh` (ruff) and `test_mcp_server.py` (smoke test).
- `assets/` – Images and static assets.
- Root files – `README.md`, `manifest.json`, `mcp-zenml.mcpb` (MCP bundle), CI in `.github/workflows/`.

## Build, Test, and Development Commands
- Run server locally: `uv run server/zenml_server.py`
- Smoke test (local): `uv run scripts/test_mcp_server.py server/zenml_server.py`
- Format & lint: `bash scripts/format.sh` (ruff check + import sort + format)
- CI mirrors the smoke test via GitHub Actions and requires Python 3.12.

## Coding Style & Naming Conventions
- Language: Python 3.12+. Indentation: 4 spaces.
- Use snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE_CASE for constants.
- Keep imports tidy; `scripts/format.sh` enforces ruff rules and import sorting.
- Logging: prefer `logging` to stderr; avoid printing from MCP tool functions except returning strings/JSON. Keep logs minimal to avoid MCP JSON protocol interference.

## Testing Guidelines
- Primary test: `scripts/test_mcp_server.py` exercises MCP connection, initialization, and basic tools.
- Run locally with `uv run ...`; CI runs on PRs and a scheduled workflow.
- If adding tests, follow descriptive names (e.g., `test_<area>_behavior.py`) and place alongside existing script tests under `scripts/` or add a `tests/` folder. Keep tests fast and network-light; mock ZenML calls when feasible.

## Commit & Pull Request Guidelines
- Commits: concise, imperative subject (e.g., "Update README", "Add smoke test"), group related changes.
- PRs: include a clear description, link related issues, and add logs/screenshots for failures or tool output when relevant. Ensure CI passes (smoke test and formatting).

## Security & Configuration Tips
- Required env vars to run tools: `ZENML_STORE_URL`, `ZENML_STORE_API_KEY`.
- Analytics env vars: `ZENML_MCP_ANALYTICS_ENABLED=false` to disable, `ZENML_MCP_ANALYTICS_DEV=true` for local testing (logs instead of sending).
- Prefer `uv` for isolated runs. Do not log secrets; scrub values in examples and CI output.
- Avoid modifying `server/lib/` unless you understand downstream effects.

