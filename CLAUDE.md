# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Running and testing
- **Run MCP server locally**: `uv run server/zenml_server.py` (stdio). For HTTP: `uv run server/zenml_server.py --transport streamable-http --port 8001`
- **Smoke test (needs `ZENML_STORE_URL`, `ZENML_STORE_API_KEY`, `ZENML_ACTIVE_PROJECT_ID`)**: `uv run scripts/test_mcp_server.py server/zenml_server.py --profile compact --write-policy read_write`. Pass `--profile legacy` or `--write-policy read_only` to check the other tool inventories; the test fails if the advertised tool list does not exactly match `zenml_tool_catalog.tool_names()`.
- **Analytics test**: `uv run scripts/test_analytics.py --full-diagnostic`
- **Credential-free test suite** (the same list CI runs in `pr-test.yml`; run each with `uv run`):
  - `scripts/test_datetime_normalization.py` - datetime filter normalization and exception classification
  - `scripts/test_resource_registry.py`, `test_resource_operations.py`, `test_resource_mutations.py`, `test_resource_actions.py` - the generic resource catalog and its read, create/update/delete and action dispatch
  - `scripts/test_resource_integration.py` - skips its live part unless run through the disposable-server script below
  - `scripts/test_sdk_contracts.py` - checks the ZenML SDK method signatures the dispatcher calls still match
  - `scripts/test_tool_contracts.py`, `test_tool_profiles.py` - tool schemas, and which tools each profile/write policy advertises
  - `scripts/test_mcp_transport.py` - the MCP 2.2 runtime: protocol negotiation, HTTP host/origin security, timeouts and cancellation, error sanitising, analytics allowlist
  - `scripts/test_mcp_apps.py` - drives both MCP Apps in headless Chromium. Install the browser first with `uv run --with playwright==1.55.0 playwright install --with-deps chromium`, or point `PLAYWRIGHT_CHROMIUM_EXECUTABLE` at an existing Chromium
  - `scripts/test_distributions.py self-test`
- **Live integration against a throwaway ZenML server**: `bash scripts/run_disposable_resource_integration.sh`. It starts a local ZenML 0.96.4 OSS server on a random loopback port with a temporary database, runs `test_resource_integration.py` against it (real create/update/delete calls, plus a check that two projects with same-named resources stay separate), then deletes everything. No credentials needed. Extra opt-in integration gates (`ZENML_MCP_ACTION_INTEGRATION`, `ZENML_MCP_RESTRICTED_INTEGRATION`) need an external server; see `RELEASE.md`.
- **Manifest check**: `uv run scripts/generate_manifest_fields.py --check` (drop `--check` to regenerate the `tools`/`prompts` fields in `manifest.json`). CI fails if the manifest does not match the registered tools.
- **MCPB bundle**: `bash scripts/build_mcpb.sh` builds `mcp-zenml.mcpb` (needs Node/npm; CI uses Node 20). `uv run scripts/test_distributions.py reproducible mcp-zenml.mcpb` rebuilds it and checks the bytes match and the unpacked bundle starts and lists the right tools.
- **Docker**: `docker build -t mcp-zenml:test . && uv run scripts/test_distributions.py docker mcp-zenml:test` (the second command starts MCP inside the container and checks the tool list).

### Code quality
- **Format + type check**: `bash scripts/format.sh` (ruff lint/format + ty). CI does **not** run ruff, so run this locally before committing.
- **Type check only**: `uvx --constraints requirements-dev.txt ty check` (rule config lives in each file's PEP 723 header, see below)
- **Check PEP 723 dependency drift**: `python scripts/check_pep723_requirements.py`
- **Recompile requirements**: `uv pip compile --generate-hashes --exclude-newer "7 days" --exclude-newer-package "mcp=2026-09-08T00:00:00Z" --exclude-newer-package "mcp-types=2026-09-08T00:00:00Z" --python-version 3.12 requirements.in -o requirements.txt`
- **Validate hashed requirements**: `uv pip install --dry-run --require-hashes --exclude-newer-package "mcp=2026-09-08T00:00:00Z" --exclude-newer-package "mcp-types=2026-09-08T00:00:00Z" -r requirements.txt` inside an active virtualenv (CI creates a throwaway Python 3.12 environment under the runner temp directory for this check)
- **Workflow security scan**: `GH_TOKEN=$(gh auth token) uvx zizmor==1.25.2 --format=github --config=.github/zizmor.yml .github/workflows/`

## Development Workflow

**IMPORTANT: Always use feature branches and pull requests for changes.**

ALSO IMPORTANT: **Before opening a PR or making a large commit**, always run `/simplify` to review changed code for reuse opportunities, quality issues, and efficiency improvements. Fix any issues it finds before committing.

1. **Create a feature branch** for any changes:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** and ensure tests pass: run `bash scripts/format.sh`, the credential-free suite above, and, if you touched dispatch or the registry, `bash scripts/run_disposable_resource_integration.sh`. If you changed packaging, also run the Docker and MCPB checks.

3. **Create a pull request** - never commit directly to main:
   ```bash
   git push -u origin feature/your-feature-name
   gh pr create --fill
   ```

4. **Wait for CI to pass** before merging. `pr-test.yml` runs on every PR and on pushes to main:
   - Disposable ZenML integration (`run_disposable_resource_integration.sh`) - no credentials needed
   - PEP 723 drift check and hashed requirements verification
   - MCP smoke test - **skipped** (not failed) when the ZenML secrets are unavailable, e.g. on Dependabot PRs
   - Analytics pipeline test
   - The credential-free test suite listed above, plus the manifest check
   - Reproducible MCPB rebuild and discovery check
   - Docker build plus MCP discovery inside the container
   - Type checking (ty)
   - Workflow security linting runs separately in `.github/workflows/zizmor.yml`, only when workflow/config files change

5. **After merge, trigger release** if needed (see Release Process below)

**Why this matters**: Direct commits to main bypass CI checks and can result in broken releases (e.g., Docker images that fail to start). The PR workflow ensures all changes are validated before release. Note that `docker-publish.yml` pushes `zenmldocker/mcp-zenml:latest` on every push to main, so `latest` tracks main, not only releases.

### PR and Commit Style

- **PR titles**: Use plain English titles without conventional commit prefixes (e.g., "Improve error detection in smoke test" not "fix: improve error detection in smoke test")
- **Commit messages**: Can use conventional commits for the commit history, but PR titles should be human-readable

## Architecture

### Core Components

The project is a Model Context Protocol (MCP) server that gives AI assistants access to a ZenML server: reading entities, creating/updating/deleting them, running lifecycle actions, and triggering pipeline runs.

**Server entry point**: `server/zenml_server.py`
- Built on the MCP Python SDK 2.2.0 `MCPServer` class (`from mcp.server.mcpserver import MCPServer`), not the 1.x `FastMCP`. Startup refuses to run unless exactly `mcp==2.2.0` is installed, because `_enforce_strict_tool_arguments` patches SDK internals so that any tool call with an unknown argument is rejected instead of silently ignored.
- Registers every tool, prompt and resource. At the end of registration it removes the tools that the configured profile and write policy do not allow (see "Tool profiles and write policy" below).
- Tools use `@mcp.tool()` + `@handle_tool_exceptions`, which turns exceptions into structured error results, records analytics (including `resource_type`, `operation` and `action` for the generic tools), and normalizes datetime filters. Prompts and resources use the simpler `@handle_exceptions`.
- The ZenML client is created lazily on first use. Tool handlers run in worker threads, so every client/REST call is serialized through `_zenml_client_call_lock`. The REST session is configured with zero automatic retries, because a retried create or delete could run twice.
- Logs go to stderr only (stdout carries the MCP JSON protocol), and `LOGLEVEL` is clamped to WARNING or higher.

**Tool catalog**: `server/zenml_tool_catalog.py` - the single list of which tool names each profile and write policy advertises (`tool_names()`), and which tools write (`MUTATING_TOOLS`). Module-level asserts pin the counts (compact 16/11, legacy 57/52).

**Resource registry**: `server/zenml_resource_registry.py` - a static, standard-library-only description of every resource type the generic tools support: which operations exist, which filters are valid and their types, the create/update/delete payload schemas (`_MUTATION_SPECS`) and allowed lifecycle actions (`_ACTION_SPECS`). This file decides what the generic tools advertise and accept; SDK introspection is only used by tests to catch drift.

**Resource dispatch**: `server/zenml_resource_dispatch.py` - takes a validated generic call and turns it into ZenML SDK calls. It applies project scoping (an explicit `project_id`, else the active project), strips credential-like and config/settings fields from responses (`_SENSITIVE_KEY_PARTS`, `_OPAQUE_SENSITIVE_FIELDS`), restricts `source` import paths in mutations to `ZENML_MCP_ALLOWED_IMPORT_PREFIXES` (default `zenml.`), and reports each mutation as `completed`, `accepted` or `unknown`. It never retries a mutation: if a request timed out, the result is `unknown` plus instructions for checking what happened, so the model does not blindly create a second copy.

**Analytics Module**: `server/zenml_mcp_analytics.py`
- Anonymous usage tracking via the ZenML Analytics Server (opt-out available)
- Sends events to `https://analytics.zenml.io/batch` with `Source-Context: mcp-zenml`
- Tracks tool usage, session duration, error rates, and MCP client info
- **Property allowlist**: `_sanitize_properties` silently drops any event property not in `ALLOWED_ANALYTICS_PROPERTIES` and truncates strings to 128 characters. If you add a new property, add it to the allowlist or it will never be sent.
- Stable user IDs: `ZENML_MCP_ANALYTICS_ID` if set, else an ID file in the user config dir; if that file cannot be written *and* the server runs in Docker, a UUID5 derived from `ZENML_STORE_URL`
- Synchronous shutdown flush (atexit + SIGTERM/SIGINT) for reliable delivery
- Session-wide properties via `set_session_properties()` / `set_client_info_once()`
- Failure-safe: analytics errors never affect server functionality
- Environment variables: `ZENML_MCP_ANALYTICS_ENABLED` / `ZENML_MCP_DISABLE_ANALYTICS` (opt out), `ZENML_MCP_ANALYTICS_DEV` (print to stderr instead of sending), `ZENML_MCP_ANALYTICS_ID`, `ZENML_MCP_ANALYTICS_TIMEOUT_S`, `ZENML_MCP_ANALYTICS_SHUTDOWN_TIMEOUT_S`, `ZENML_MCP_ANALYTICS_TEST_RUN`

**MCP Apps**: `server/ui/pipeline-runs/` and `server/ui/run-activity-chart/` - self-contained HTML apps served as `ui://zenml/apps/...` resources and opened by `open_pipeline_run_dashboard` / `open_run_activity_chart`. They call only compact-profile tools (`zenml_list_resources`, `get_step_logs`).

### Environment variables

| Variable | Values | Effect |
|---|---|---|
| `ZENML_STORE_URL`, `ZENML_STORE_API_KEY` | | ZenML server connection (required for tools) |
| `ZENML_ACTIVE_PROJECT_ID` | project UUID | Default project for calls that omit `project_id` |
| `ZENML_MCP_PROFILE` | `compact` (default), `legacy` | Which tool names are advertised. An invalid value stops startup |
| `ZENML_MCP_WRITE_POLICY` | `read_write` (default), `read_only` | `read_only` removes the four generic write tools and `trigger_pipeline`, and hides mutation schemas. Any unrecognized value falls back to read-only |
| `ZENML_MCP_READ_ONLY` | `true`/`false` | Older flag; if set, it wins over `ZENML_MCP_WRITE_POLICY`. An unrecognized value also means read-only |
| `ZENML_MCP_ALLOWED_IMPORT_PREFIXES` | comma list, default `zenml.` | Allowed `source` import paths in mutation payloads |
| `ZENML_MCP_STARTUP_VALIDATION` | `off` (default), `warn`, `strict` | Check required setup at startup |
| `ZENML_MCP_FORWARDED_ALLOW_IPS` | default `127.0.0.1` | Which proxies' forwarded headers are trusted (HTTP transport) |
| `LOGLEVEL` | | Clamped to WARNING or higher |

Analytics variables are listed under the analytics module above.

### Domain Model: Snapshots vs Run Templates

**Historical context:** ZenML underwent a significant evolution in its "runnable pipeline artifact" concepts:

- **2024-07-22**: Run Templates introduced, pointing to "pipeline deployments"
- **2025-07-22**: Pipeline Deployments renamed to **Snapshots**; Run Templates now reference snapshots via `source_snapshot_id`
- **Current (ZenML 0.96.4)**: run-template CRUD is still supported. Snapshots are preferred; pipeline convenience creation and template-based triggering are deprecated.

**What this means:**
- **Snapshots** = The core "frozen pipeline configuration" artifact (immutable, runnable, deployable)
- **Run Templates** = A legacy wrapper that just references a snapshot (effectively a named pointer)

**For contributors:**
- New development should be snapshot-first. In the compact profile, both are reached through the generic tools with `resource_type="snapshot"` or `"run_template"`.
- `get_run_template` / `list_run_templates` exist only in the legacy profile and carry deprecation notices.
- `trigger_pipeline` supports both `snapshot_name_or_id` (preferred) and `template_id` (deprecated)

### Tool profiles and write policy

The server has two tool profiles. Which one is active decides which tool names a client sees; the list itself lives in `server/zenml_tool_catalog.py`.

**`compact` (default since 2.0) - 16 tools.** Seven generic tools take a `resource_type` argument and cover every supported entity:

| Tool | Purpose | Writes? |
|---|---|---|
| `zenml_describe_resources` | List supported resource types, or give the exact input schema for one type + operation | no |
| `zenml_list_resources` | List one resource type with validated filters and pagination | no |
| `zenml_get_resource` | Get one resource | no |
| `zenml_create_resource` | Create one resource from a typed payload | yes |
| `zenml_update_resource` | Update one resource by exact UUID | yes |
| `zenml_delete_resource` | Delete or archive one resource by exact UUID | yes |
| `zenml_action_resource` | Run one allowlisted lifecycle action (no retries) | yes |

Nine specialized tools stay because they don't fit the list/get/create shape: `diagnose_zenml_setup`, `get_active_user`, `get_active_project`, `trigger_pipeline` (writes), `get_step_logs`, `get_step_code`, `get_deployment_logs`, `open_pipeline_run_dashboard`, `open_run_activity_chart`.

**`legacy` - 57 tools.** Adds back the 1.x entity-specific tools (`list_pipeline_runs`, `get_stack`, `list_snapshots`, ...) with their exact old schemas, recorded in `scripts/fixtures/legacy_tool_schemas.json`. This exists only so existing clients keep working while they migrate. Do not add new functionality to legacy tools.

**Read-only policy** removes the tools marked "writes" above: compact 11 tools, legacy 52.

**Prompts and resources** are the same in both profiles: prompts `stack_components_analysis` and `recent_runs_analysis`; resources `resource://zenml_server/resources`, `resource://zenml_server/resource-schemas/{resource_type}/{operation}`, `resource://zenml_server/apps`, `resource://zenml_server/most_recent_runs?run_count={run_count}`, and the two `ui://zenml/apps/...` HTML apps.

For the current list of resource types and what each supports, call `zenml_describe_resources` or read `RESOURCE_REGISTRY` in `server/zenml_resource_registry.py`, rather than copying it here.

### Adding or extending ZenML coverage

**Default path: add to the registry, not a new tool.** To support a new entity type, operation, filter or action:
1. Edit `server/zenml_resource_registry.py`: the `_spec(...)` entry in `_RESOURCE_SPECS`, `LIST_SDK_METHODS` / `GET_SDK_METHODS`, `_MUTATION_SPECS` for create/update/delete, `_ACTION_SPECS` for actions, and the filter type sets (`DATETIME_FILTERS` etc.) if you add filters.
2. If the SDK call doesn't fit the generic pattern, add a special case in `server/zenml_resource_dispatch.py` (`_create_call`, `_update_call`, `_delete_call`, `_run_action_call`, ...). Return data through the existing redaction so secrets and config fields are stripped.
3. Update the tests that pin the contract: the count asserts in `scripts/test_resource_registry.py`, the JSON fixtures in `scripts/fixtures/` (`resource_mutation_schemas.json`, `resource_mutation_calls.json`, `resource_action_contracts.json`; these are edited by hand), `scripts/test_sdk_contracts.py`, and `scripts/test_resource_operations.py`.
4. Update the coverage paragraph in `README.md`.
5. No tool, catalog or manifest change is needed.

**Only add a new tool** when the behaviour genuinely doesn't fit list/get/create/update/delete/action (e.g. streaming logs, an MCP App). Then:
1. Add the tool to `server/zenml_server.py` with `@mcp.tool()` + `@handle_tool_exceptions`. If it writes, call `ensure_writes_enabled()` first.
2. Add its name to `COMPACT_SPECIALIZED_TOOLS` (and/or `LEGACY_TOOLS`) in `server/zenml_tool_catalog.py`; add it to `MUTATING_TOOLS` if it writes; update the count asserts at the bottom of that file.
3. Update the hard-coded `COMPACT_READ_WRITE` list in `scripts/test_tool_profiles.py` and the tool/profile tables in `README.md`.
4. Regenerate the manifest: `uv run scripts/generate_manifest_fields.py`.
5. If the tool is read-only and needs no arguments, add it to `safe_tools_to_test` in `scripts/test_mcp_server.py`.

**Adding a new MCP App**: HTML in `server/ui/<app>/index.html`; an `@mcp.resource` with `mime_type="text/html;profile=mcp-app"` with `meta={"ui": {"csp": {"resourceDomains": ["https://unpkg.com"]}}}` (the apps load their helper library from unpkg, and nothing else is allowed); an entry in `list_apps`; an `open_*` tool with `meta={"ui": {"resourceUri": ...}}` registered as above; and browser tests in `scripts/test_mcp_apps.py`. Apps must only call compact-profile tools.

**Adding a new `server/*.py` module**: the `Dockerfile` and `scripts/build_mcpb.sh` copy server modules one by one. Add the new file to both, or the Docker image and the `.mcpb` bundle will fail to import it at startup. (`server/ui/` is copied as a whole directory.)

**Two datetime normalizers exist**: `_normalize_datetime_filter` in `zenml_server.py` (legacy tools) and `normalize_datetime_filter` in `zenml_resource_dispatch.py` (generic tools). A fix to one probably needs the same fix in the other; both are tested in `scripts/test_datetime_normalization.py`.

**Importing the server in tests**: tests add `server/` to `sys.path` and import `zenml_server` directly. Tool registration happens at import time using the environment at that moment, so set `ZENML_MCP_PROFILE` / `ZENML_MCP_WRITE_POLICY` *before* the import (see `scripts/test_tool_contracts.py`).

### Environment Setup

The server requires:
- Python 3.12-3.14 (the Docker image and PR CI use 3.12; the release workflow tests the MCPB bundle on Linux, macOS and Windows with 3.12, 3.13 and 3.14)
- Exactly `mcp[cli]==2.2.0` and `zenml==0.96.4`, installed via `uv` (preferred) or pip
- ZenML server URL and API key configured as environment variables

### Testing Infrastructure

- **PR Testing**: `pr-test.yml` runs on every PR and push to main (see the list under Development Workflow). Formatting is not checked in CI.
- **Scheduled testing**: `mcp-smoke-test.yml` runs the smoke test every 3 days against a real ZenML server; on failure it opens a GitHub issue and sends a Discord alert.
- **Manual testing**: Use the test scripts or the MCP Inspector (below) to verify MCP protocol functionality
- **CI/CD**: Uses `uv` with caching. The PR, release and release-docker workflows and `build_mcpb.sh` pin `uv` to `0.11.28`. The Docker runtime image, the scheduled smoke workflow and the zizmor workflow use `0.8.15`.
- **Important**: When adding new test scripts, always wire them into `.github/workflows/pr-test.yml` so they run in CI. Tests that don't need ZenML credentials should run unconditionally (no `if: env.HAS_ZENML_CREDENTIALS == 'true'` guard).

### Debugging with MCP Inspector

The [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) is an interactive debugging tool for testing MCP servers. It provides a web UI to call tools, inspect responses, and debug issues.

**Quick start (using .env.local):**

1. Copy the example file and add your credentials:
   ```bash
   cp .env.local.example .env.local
   # Edit .env.local with your ZENML_STORE_URL and ZENML_STORE_API_KEY
   ```

2. Run the inspector with credentials loaded from `.env.local`:
   ```bash
   source .env.local && npx @modelcontextprotocol/inspector \
     -e ZENML_STORE_URL=$ZENML_STORE_URL \
     -e ZENML_STORE_API_KEY=$ZENML_STORE_API_KEY \
     -- uv run server/zenml_server.py
   ```

This opens a web UI (typically at `http://localhost:6274`) with your credentials pre-filled. Just click **"Connect"** and start testing!

**Alternative: inline credentials (for one-off testing):**

```bash
npx @modelcontextprotocol/inspector \
  -e ZENML_STORE_URL=https://your-server.zenml.io \
  -e ZENML_STORE_API_KEY=ZENKEY_... \
  -- uv run server/zenml_server.py
```

**Key syntax notes:**
- `-e key=value` flags pass environment variables to the server subprocess
- Place `-e` flags **before** the command (`uv`)
- Use `--` to separate inspector flags from server arguments

**Without pre-filled env vars:**

```bash
npx @modelcontextprotocol/inspector uv run server/zenml_server.py
```

Then manually add `ZENML_STORE_URL` and `ZENML_STORE_API_KEY` in the UI under **Environment Variables** before clicking **Connect**.

**What you can test:**
- **Tools tab**: Call any MCP tool and see JSON request/response
- **Resources tab**: Browse the resource catalog, per-operation schemas, the app list, recent runs and the two app HTML pages
- **Prompts tab**: View the `stack_components_analysis` and `recent_runs_analysis` prompts
- **History**: See all previous tool calls in the session

### Testing MCP Apps with Docker + Cloudflare Tunnel

MCP Apps (interactive HTML UIs rendered in sandboxed iframes) require Streamable HTTP transport and a publicly reachable URL. Use Docker + Cloudflare tunnel for local testing.

> **Note (last checked January 2026, re-verify):** At that time, Claude Desktop and Claude.ai do **not** render MCP Apps (the tool calls work, but the interactive iframe UI does not appear). MCP Apps currently work with third-party clients that support the MCP Apps specification. This testing workflow is primarily useful for development validation.

**1. Build the Docker image:**
```bash
docker build --no-cache -t mcp-zenml:test .
```

**2. Run the container:**
```bash
docker run --rm -d --name mcp-zenml-test -p 8001:8001 \
  -e ZENML_STORE_URL=https://your-server.zenml.io \
  -e ZENML_STORE_API_KEY=ZENKEY_... \
  -e ZENML_ACTIVE_PROJECT_ID=your-project-id \
  mcp-zenml:test --transport streamable-http --host 0.0.0.0 --port 8001 --disable-dns-rebinding-protection
```

**3. Start a Cloudflare tunnel:**
```bash
npx cloudflared tunnel --url http://localhost:8001
```
This prints a public URL like `https://random-words.trycloudflare.com`.

**4. Connect from an MCP client:**
- Add the tunnel URL with `/mcp` path as a Streamable HTTP MCP server: `https://random-words.trycloudflare.com/mcp`
- Ask the assistant to use the app (e.g., "open the run activity chart")
- If the app UI does not render (blank/no iframe), this is typically a client capability limitation rather than a server issue

**Gotchas:**
- **`ZENML_ACTIVE_PROJECT_ID` is required** — without an active project, any project-scoped call that does not pass `project_id` (including the ones the MCP Apps make) fails with "No project is currently set as active"
- **Port 8000 may be in use** — the MCP Inspector or other services often occupy 8000; use 8001+ for Docker
- **Tunnel URL changes on restart** — each `npx cloudflared tunnel` invocation gets a new random URL; update your MCP client configuration accordingly
- **Container logs are essential** — run `docker logs mcp-zenml-test` to see server errors (they won't appear in the browser/iframe)
- The Dockerfile copies the whole `server/ui/` directory, so new MCP App HTML files are included in the build (new Python modules are not; see "Adding a new `server/*.py` module")
- Binding to `0.0.0.0` requires `--disable-dns-rebinding-protection`; without it the server refuses a wildcard host because it cannot build a Host/Origin allowlist

### Project Structure

- `server/` - MCP server
  - `zenml_server.py` - entry point, all tools/prompts/resources
  - `zenml_tool_catalog.py` - which tools each profile and write policy advertises
  - `zenml_resource_registry.py` - static catalog of resource types, filters, mutation and action schemas
  - `zenml_resource_dispatch.py` - validation, project scoping, redaction and SDK calls for the generic tools
  - `zenml_mcp_analytics.py` - anonymous usage analytics
  - `ui/pipeline-runs/`, `ui/run-activity-chart/` - MCP App HTML (self-contained single-file apps)
- `scripts/` - tests (`test_*.py`), `fixtures/` (JSON contract fixtures used by the tests), and tooling: `format.sh`, `build_mcpb.sh`, `bump_version.py`, `generate_manifest_fields.py`, `check_pep723_requirements.py`, `run_disposable_resource_integration.sh`
- `assets/` - Project assets and images
- Root files: `VERSION` (version source of truth), `manifest.json` (MCPB manifest, version 0.4), `mcp-zenml.mcpb` + `mcpb-uv.lock` (Claude Desktop bundle and its dependency lock), `server.json` (MCP Registry entry), `Dockerfile`, `requirements.in` / `requirements.txt`, `RELEASE.md` (detailed release runbook)

### Type Checking with ty

The project uses [ty](https://docs.astral.sh/ty/) for static type checking - an extremely fast Python type checker from Astral (creators of uv and ruff).

**Configuration**: `pyproject.toml` under `[tool.ty]` (applies to files without a PEP 723 header; scripts carry their own `[tool.ty]` blocks in their header, see the note below)
- Python version: 3.12
- Extra paths: `server/` (allows `import zenml_mcp_analytics` to resolve)
- Include patterns: `server/**/*.py`, `scripts/**/*.py`
- Third-party imports: Ignored (since deps are installed on-the-fly via PEP 723)

**Running type checks**:
```bash
uvx --constraints requirements-dev.txt ty check             # Basic check (same pin as CI)
uvx --constraints requirements-dev.txt ty check --output-format=github  # For CI (annotations)
bash scripts/format.sh          # Runs ruff + ty together
```

**Suppressing false positives**: Use `# type: ignore[rule-name]` or `# ty: ignore[rule-name]` comments when needed (prefer rule-specific suppressions).

**CI Integration**: Type checking runs as a separate job in PR tests (`.github/workflows/pr-test.yml`).

**Note on third-party imports**: Since this project uses PEP 723 inline script metadata for dependencies (installed on-the-fly by `uv run`), ty runs in isolation and can't see them. Since ty 0.0.62, any file with a PEP 723 header is treated as its own project and takes its rules from that header, not from `pyproject.toml`. So every PEP 723 script carries a `[tool.ty.rules]` block with `unresolved-import = "ignore"`, and scripts that import from `server/` also carry `[tool.ty.environment]` with `extra-paths = ["../server"]` so first-party imports (like `zenml_mcp_analytics`) are still checked. Runtime scripts that install `mcp` also carry a `[tool.uv]` block with `exclude-newer-package = { mcp = "2026-09-08T00:00:00Z", "mcp-types" = "2026-09-08T00:00:00Z" }`, which exempts the pinned MCP SDK from the 7-day cooldown. When adding a new PEP 723 script, copy those blocks into its header. ty is pinned once in `requirements-dev.txt`, which CI and `scripts/format.sh` pass to `uvx --constraints`; Dependabot opens a PR when a new ty release is available.

### Important Implementation Details

- **Logging**: stderr only, and ZenML internal logging is suppressed, so nothing corrupts the JSON protocol on stdout
- **Error Handling**: tools use `@handle_tool_exceptions`; prompts/resources use `@handle_exceptions` (see Core Components)
- **Strict arguments**: unknown tool arguments are rejected (depends on the exact `mcp==2.2.0` pin)
- **No retries on writes**: the REST session has retries disabled; mutations report `completed` / `accepted` / `unknown`
- **Lazy Loading**: ZenML client initialized only when needed, and accessed under a lock
- **Environment Variables**: see the table under Architecture
- **Type Hints**: All public functions have type hints; type checking enforced in CI

### Supply Chain Security

The project applies multiple layers of supply chain protection:

- **Python package cooldown**: `exclude-newer = "7 days"` in `[tool.uv]` (`pyproject.toml`) prevents installing packages published within the last 7 days, giving time for compromised versions to be detected and yanked. Override for a single install: `uv add <pkg> --exclude-newer "0 days"`
- **Pinned + hashed requirements**: `requirements.in` holds human-editable constraints; `requirements.txt` is compiled with exact versions and SHA256 hashes. Docker builds enforce these hashes with `uv pip install --require-hashes ...`. PR CI also verifies the file with `uv pip install --dry-run --require-hashes --exclude-newer-package "mcp=2026-09-08T00:00:00Z" --exclude-newer-package "mcp-types=2026-09-08T00:00:00Z" -r requirements.txt` inside a throwaway Python 3.12 venv.
- **PEP 723 drift check**: Runtime `uv run` entry points mirror `requirements.in`, and `scripts/check_pep723_requirements.py` fails CI if those inline dependency blocks drift. It only checks the files listed in `RUNTIME_MIRROR_PEP723_FILES` (the server and five test scripts); the `test_resource_*` and `test_tool_profiles` scripts repeat the same dependencies but are not checked.
- **Pinned MCPB build**: `scripts/build_mcpb.sh` uses exact uv (0.11.28) and MCPB tool (`@anthropic-ai/mcpb@2.1.2`) versions, copies `mcpb-uv.lock`, updates only the local package version offline, and packages source instead of host-native vendored dependencies. Set `MCPB_REFRESH_LOCK=1` only for an intentional lock refresh.
- **Docker image digests**: Base images in the `Dockerfile` are pinned to `@sha256:` digests (not just tags) to prevent tag mutation attacks
- **GitHub Actions SHA pinning**: All third-party actions pinned to full commit SHAs with version comments; `persist-credentials: false` on all checkout steps. Every `astral-sh/setup-uv` step pins an explicit `uv` version; bundle and release validation use `0.11.28`.
- **Dependabot cooldown**: 7-day cooldown on grouped GitHub Actions updates and on `ty` bumps in `requirements-dev.txt` (`.github/dependabot.yml`). Dependabot does not touch the hashed `requirements.txt`.
- **MCP Registry publisher pinned**: `release-docker.yml` downloads the `mcp-publisher` release binary at a fixed version (`MCP_PUBLISHER_VERSION`, currently 1.8.1) and checks its SHA256 (`MCP_PUBLISHER_LINUX_AMD64_SHA256`) before running it. Bump both together. It logs in with GitHub OIDC (`mcp-publisher login github-oidc`), so no registry token is stored.
- **zizmor audit**: Security linting of workflow files runs in the dedicated `.github/workflows/zizmor.yml` workflow with minimal permissions, path filters, weekly scheduled runs, and manual dispatch.

**Recompiling requirements.txt** (after updating `requirements.in`):
```bash
uv pip compile --generate-hashes --exclude-newer "7 days" \\
  --exclude-newer-package "mcp=2026-09-08T00:00:00Z" \\
  --exclude-newer-package "mcp-types=2026-09-08T00:00:00Z" \\
  --python-version 3.12 requirements.in -o requirements.txt
```

**Checking PEP 723 runtime dependency drift locally**:
```bash
python scripts/check_pep723_requirements.py
```

**Validating requirements.txt hashes locally**:
```bash
uv pip install --dry-run --require-hashes \
  --exclude-newer-package "mcp=2026-09-08T00:00:00Z" \
  --exclude-newer-package "mcp-types=2026-09-08T00:00:00Z" \
  -r requirements.txt
```

If no virtual environment is active, create a temporary one first:
```bash
REQUIREMENTS_HASH_CHECK_ENV=$(mktemp -d)
uv venv --python 3.12 "${REQUIREMENTS_HASH_CHECK_ENV}"
source "${REQUIREMENTS_HASH_CHECK_ENV}/bin/activate"
uv pip install --dry-run --require-hashes \
  --exclude-newer-package "mcp=2026-09-08T00:00:00Z" \
  --exclude-newer-package "mcp-types=2026-09-08T00:00:00Z" \
  -r requirements.txt
```

**Running the workflow security scan locally**:
```bash
GH_TOKEN=$(gh auth token) uvx zizmor==1.25.2 --format=github --config=.github/zizmor.yml .github/workflows/
```

## Release Process

`RELEASE.md` is the detailed runbook; this is the summary.

### Triggering a Release

1. **In a PR**, bump the version and regenerate the manifest, then merge:
   ```bash
   python scripts/bump_version.py --version X.Y.Z
   uv run scripts/generate_manifest_fields.py
   ```
2. **Dry run** the release (builds and tests everything, publishes nothing):
   ```bash
   gh workflow run release.yml --repo zenml-io/mcp-zenml -f dry_run=true
   ```
3. **Release for real**:
   ```bash
   gh workflow run release.yml --repo zenml-io/mcp-zenml
   ```
   `version` is optional and read from `VERSION` when omitted; if you pass `-f version=X.Y.Z` it must match. Add `-f prerelease=true` for a prerelease.

What runs, in order:
1. **`release.yml` (Release Orchestrator)**:
   - `test-gate`: drift and manifest checks, tool-profile and transport tests, a smoke test that must run with real credentials (no skip), Docker and reproducible-MCPB checks
   - `live-integration`: the disposable ZenML server integration test
   - `prepare-candidate`: bumps version files, regenerates the manifest, builds the `.mcpb` once and stores it as a workflow artifact
   - `bundle-compatibility`: runs that exact `.mcpb` on Linux, macOS and Windows with Python 3.12, 3.13 and 3.14
   - `release` (GitHub environment `release`, uses `GH_RELEASE_PAT`): commits the six release files to main, creates the annotated `vX.Y.Z` tag, and creates the GitHub release with the `.mcpb` attached
2. **`release-docker.yml`** (triggered by the tag push, GitHub environment `docker`): builds and checks the image on amd64 and arm64, pushes `zenmldocker/mcp-zenml:X.Y.Z` and `:latest`, and publishes `server.json` to the MCP Registry

**Note**: The release will fail if tests don't pass. This prevents releasing broken builds.

### Version Files

These must stay in sync; `scripts/bump_version.py` updates all of them:
- `VERSION` - Source of truth
- `manifest.json` - MCPB manifest
- `server.json` - MCP Registry entry, including the version in the OCI image identifier (`docker.io/zenmldocker/mcp-zenml:X.Y.Z`)
- `pyproject.toml` - Project configuration

`bump_version.py` also rejects a `server.json` description that is empty or longer than 100 characters (the MCP Registry limit). `scripts/build_mcpb.sh` then updates the local package version in `mcpb-uv.lock`. The release commit contains exactly six files: the four above, `mcpb-uv.lock`, and `mcp-zenml.mcpb`.

### Debugging MCP Registry Schema Failures

The MCP Registry schema evolves frequently. If the "Publish to MCP Registry" step fails with a deprecated schema error:

1. **Find the current schema version** by checking the mcp-publisher source:
   ```bash
   curl -s https://raw.githubusercontent.com/modelcontextprotocol/registry/main/pkg/model/constants.go | grep CurrentSchemaVersion
   ```

2. **Verify the schema URL exists**:
   ```bash
   curl -sI "https://static.modelcontextprotocol.io/schemas/YYYY-MM-DD/server.schema.json" | head -1
   # Should return HTTP/2 200
   ```

3. **Update `server.json`** with the new schema URL

4. **Check the changelog** for breaking changes:
   https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/server-json/CHANGELOG.md

### Common Schema Migration Issues

- **snake_case → camelCase** (2025-09-16): Field names like `registry_type` became `registryType`
- **OCI identifier format** (2025-12-11): Removed `registryBaseUrl` and separate `version` fields; use canonical identifier instead: `docker.io/owner/image:version`
- **Removed fields**: `status` and `privacy_policies` are no longer valid

### Release Cleanup

Try the least destructive fix first:

- **Before anything else, use `dry_run=true`** to catch problems without tagging.
- **Rerunning `release.yml` is safe** if the tag already exists, points at the current main commit, and the candidate does not change any release file: it reuses the tag and re-uploads the `.mcpb` (`gh release upload --clobber`). It only fails if the tag points at a different commit.
- **Docker job failed after tagging**: rerun that workflow run (`gh run rerun <run-id>`).
- **Only the MCP Registry publish failed** (e.g. schema or description errors): fix `server.json` on main (`VERSION` must still equal `X.Y.Z`), then run the recovery job, which re-publishes to the registry without rebuilding Docker:
  ```bash
  gh workflow run release-docker.yml --repo zenml-io/mcp-zenml -f release_tag=vX.Y.Z
  ```
  This job reads `server.json` from main, not from the tag, so metadata fixes apply without re-tagging. It checks that the tag exists.
- **The code at the tag itself is wrong**: delete the release and tag, fix main, and release again:
  ```bash
  gh release delete vX.Y.Z --repo zenml-io/mcp-zenml --yes
  git push origin --delete vX.Y.Z
  gh workflow run release.yml --repo zenml-io/mcp-zenml
  ```

**Important**: The tag-triggered Docker job in `release-docker.yml` builds from the code **at the tag**, not from main. A fix pushed to main does not reach the Docker image until the tag is recreated.
