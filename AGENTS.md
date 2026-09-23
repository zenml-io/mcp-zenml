# Repository Guidelines

Read `CLAUDE.md` first. It is the contributor guide for this repo: commands, the full test list, architecture, how to add ZenML coverage, CI, supply-chain rules and the release summary. `RELEASE.md` is the release runbook. This file only adds the conventions below and does not repeat those.

## Quick start
- Run the server: `uv run server/zenml_server.py`
- Before committing: `bash scripts/format.sh` (ruff + ty; CI does not run ruff)
- Tests: see "Commands" in `CLAUDE.md`

## Coding Style & Naming Conventions
- Indentation: 4 spaces.
- Use snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE_CASE for constants.
- Keep imports tidy; `scripts/format.sh` enforces ruff rules and import sorting.
- Logging: prefer `logging` to stderr; avoid printing from MCP tool functions except returning strings/JSON. Keep logs minimal to avoid MCP JSON protocol interference.
- New ZenML entity coverage goes into the resource registry, not new tools (see "Adding or extending ZenML coverage" in `CLAUDE.md`).

## Testing Guidelines
- **When adding new test scripts, always wire them into `.github/workflows/pr-test.yml`** so they run in CI. Tests that don't need ZenML credentials should run unconditionally.
- Follow descriptive names (e.g., `test_<area>_behavior.py`) and place under `scripts/`. Keep tests fast and network-light; mock ZenML calls when feasible.

## Commit & Pull Request Guidelines
- Commits: concise, imperative subject (e.g., "Update README", "Add smoke test"), group related changes.
- PRs: plain-English titles, a clear description, link related issues, and add logs/screenshots for failures or tool output when relevant. Ensure the PR Tests workflow passes.

## Security & Configuration Tips
- Required env vars to run tools: `ZENML_STORE_URL`, `ZENML_STORE_API_KEY`. The full list, including `ZENML_MCP_PROFILE` and `ZENML_MCP_WRITE_POLICY`, is in `CLAUDE.md`.
- Prefer `uv` for isolated runs. Do not log secrets; scrub values in examples and CI output.
- Responses from the generic tools go through redaction in `server/zenml_resource_dispatch.py`; keep new code paths going through it.
