# Repository Guidelines

`CLAUDE.md` is the full contributor guide (architecture, how to add coverage, CI, supply-chain rules, release process) and `RELEASE.md` is the release runbook. This file is the short version; if the two disagree, `CLAUDE.md` wins.

## Project Structure & Module Organization
- `server/` – MCP server implementation.
  - `zenml_server.py` – entry point; registers all tools, prompts and resources.
  - `zenml_tool_catalog.py` – which tool names each profile (`compact`/`legacy`) and write policy (`read_write`/`read_only`) advertises.
  - `zenml_resource_registry.py` – static catalog of resource types, filters, and create/update/delete/action schemas used by the generic `zenml_*_resource` tools.
  - `zenml_resource_dispatch.py` – validates generic calls, applies project scoping and redaction, and calls the ZenML SDK.
  - `zenml_mcp_analytics.py` – anonymous usage analytics.
  - `ui/` – MCP App HTML (`pipeline-runs/`, `run-activity-chart/`).
- `scripts/` – tests (`test_*.py`), JSON contract fixtures (`fixtures/`), and tooling (`format.sh`, `build_mcpb.sh`, `bump_version.py`, `generate_manifest_fields.py`, `check_pep723_requirements.py`, `run_disposable_resource_integration.sh`).
- `assets/` – Images and static assets.
- Root files – `README.md`, `VERSION`, `manifest.json` + `mcp-zenml.mcpb` + `mcpb-uv.lock` (Claude Desktop bundle), `server.json` (MCP Registry), `Dockerfile`, `requirements.in`/`requirements.txt`, CI in `.github/workflows/`.

## Build, Test, and Development Commands
- Run server locally: `uv run server/zenml_server.py`
- Format, lint and type check: `bash scripts/format.sh` (ruff + ty). CI does not run ruff, so run this before committing.
- Credential-free tests (what CI runs): `uv run scripts/<name>.py` for `test_datetime_normalization`, `test_resource_registry`, `test_resource_operations`, `test_resource_mutations`, `test_resource_actions`, `test_resource_integration`, `test_sdk_contracts`, `test_tool_contracts`, `test_mcp_transport`, `test_tool_profiles`, `test_mcp_apps` (needs Chromium), plus `uv run scripts/test_distributions.py self-test` and `uv run scripts/generate_manifest_fields.py --check`.
- Live integration against a throwaway local ZenML server (no credentials): `bash scripts/run_disposable_resource_integration.sh`
- Smoke test against a real ZenML server: `uv run scripts/test_mcp_server.py server/zenml_server.py --profile compact --write-policy read_write`
- PR CI and Docker use Python 3.12; release CI also tests the MCPB bundle on 3.13 and 3.14.

## Coding Style & Naming Conventions
- Language: Python 3.12+. Indentation: 4 spaces.
- Use snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE_CASE for constants.
- Keep imports tidy; `scripts/format.sh` enforces ruff rules and import sorting.
- Logging: prefer `logging` to stderr; avoid printing from MCP tool functions except returning strings/JSON. Keep logs minimal to avoid MCP JSON protocol interference.
- New ZenML entity coverage goes into the resource registry, not new tools. See "Adding or extending ZenML coverage" in `CLAUDE.md`.
- A new `server/*.py` module must also be added to the `Dockerfile` and `scripts/build_mcpb.sh`, which copy modules one by one.

## Testing Guidelines
- The main PR gate is the credential-free suite above plus the disposable-server integration test. The smoke test (`scripts/test_mcp_server.py`) needs ZenML credentials and is skipped in CI when they are unavailable.
- Analytics tests: `scripts/test_analytics.py` tests the analytics pipeline.
- **When adding new test scripts, always wire them into `.github/workflows/pr-test.yml`** so they run in CI. Tests that don't need ZenML credentials should run unconditionally.
- Follow descriptive names (e.g., `test_<area>_behavior.py`) and place under `scripts/`. Keep tests fast and network-light; mock ZenML calls when feasible.
- Tests that import `zenml_server` must set `ZENML_MCP_PROFILE` / `ZENML_MCP_WRITE_POLICY` before importing, because tools are registered at import time.

## Commit & Pull Request Guidelines
- Commits: concise, imperative subject (e.g., "Update README", "Add smoke test"), group related changes.
- PRs: plain-English titles, a clear description, link related issues, and add logs/screenshots for failures or tool output when relevant. Ensure the PR Tests workflow passes.

## Security & Configuration Tips
- Required env vars to run tools: `ZENML_STORE_URL`, `ZENML_STORE_API_KEY`; `ZENML_ACTIVE_PROJECT_ID` sets the default project.
- Tool exposure: `ZENML_MCP_PROFILE=compact|legacy` (default compact, 16 tools) and `ZENML_MCP_WRITE_POLICY=read_write|read_only` (read-only hides all write tools; invalid values fall back to read-only).
- Analytics env vars: `ZENML_MCP_ANALYTICS_ENABLED=false` to disable, `ZENML_MCP_ANALYTICS_DEV=true` for local testing (logs instead of sending).
- Prefer `uv` for isolated runs. Do not log secrets; scrub values in examples and CI output. Responses from the generic tools go through redaction in `zenml_resource_dispatch.py`; keep new code paths going through it.
