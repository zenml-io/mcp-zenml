# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Testing and Development
- **Run smoke tests**: `uv run scripts/test_mcp_server.py server/zenml_server.py`
- **Run analytics tests**: `uv run scripts/test_analytics.py --full-diagnostic`
- **Format code**: `./scripts/format.sh` (uses ruff for linting and formatting)
- **Run MCP server locally**: `uv run server/zenml_server.py`

### Code Quality
- **Format**: `bash scripts/format.sh`

## Development Workflow

**IMPORTANT: Always use feature branches and pull requests for changes.**

1. **Create a feature branch** for any changes:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** and ensure tests pass:
   ```bash
   uv run scripts/test_mcp_server.py server/zenml_server.py
   docker build -t mcp-zenml:test .  # Verify Docker build works
   ```

3. **Create a pull request** - never commit directly to main:
   ```bash
   git push -u origin feature/your-feature-name
   gh pr create --fill
   ```

4. **Wait for CI to pass** before merging - PR tests include:
   - MCP smoke tests (Python)
   - Analytics pipeline tests
   - Docker build verification
   - Format checks

5. **After merge, trigger release** if needed (see Release Process below)

**Why this matters**: Direct commits to main bypass CI checks and can result in broken releases (e.g., Docker images that fail to start). The PR workflow ensures all changes are validated before release.

## Architecture

### Core Components

The project is a Model Context Protocol (MCP) server that provides AI assistants with access to ZenML API functionality.

**Main Server File**: `server/zenml_server.py`
- Uses FastMCP framework for MCP protocol implementation
- Implements lazy initialization of ZenML client to avoid startup delays
- Provides comprehensive exception handling with the `@handle_exceptions` decorator
- Configures minimal logging to prevent JSON protocol interference

**Analytics Module**: `server/zenml_mcp_analytics.py`
- Anonymous usage tracking via the ZenML Analytics Server (opt-out available)
- Sends events to `https://analytics.zenml.io/batch` with `Source-Context: mcp-zenml`
- Tracks tool usage, session duration, and error rates
- Failure-safe: analytics errors never affect server functionality
- Environment variables: `ZENML_MCP_ANALYTICS_ENABLED`, `ZENML_MCP_ANALYTICS_DEV`

**Key Features**:
- Reads ZenML server configuration from environment variables (`ZENML_STORE_URL`, `ZENML_STORE_API_KEY`)
- Provides MCP tools for accessing ZenML entities (users, stacks, pipelines, runs, etc.)
- Supports triggering new pipeline runs via snapshots (preferred) or run templates (deprecated)
- Includes automated CI/CD testing with GitHub Actions

### Domain Model: Snapshots vs Run Templates

**Historical context:** ZenML underwent a significant evolution in its "runnable pipeline artifact" concepts:

- **2024-07-22**: Run Templates introduced, pointing to "pipeline deployments"
- **2025-07-22**: Pipeline Deployments renamed to **Snapshots**; Run Templates now reference snapshots via `source_snapshot_id`
- **Current**: Run Template API marked `deprecated=True`; SDK methods emit deprecation warnings

**What this means:**
- **Snapshots** = The core "frozen pipeline configuration" artifact (immutable, runnable, deployable)
- **Run Templates** = A legacy wrapper that just references a snapshot (effectively a named pointer)

**For contributors:**
- New development should be snapshot-first
- Run template tools (`get_run_template`, `list_run_templates`) are kept for backward compatibility but include deprecation warnings
- `trigger_pipeline` supports both `snapshot_name_or_id` (preferred) and `template_id` (deprecated)

### MCP Tool Taxonomy

Tools are organized by entity type in `server/zenml_server.py`:

| Category | Tools | Notes |
|----------|-------|-------|
| **Projects** | `get_active_project`, `get_project`, `list_projects` | New in v1.2 |
| **Snapshots** | `get_snapshot`, `list_snapshots` | Replaces run templates |
| **Deployments** | `get_deployment`, `list_deployments`, `get_deployment_logs` | New in v1.2 |
| **Tags** | `get_tag`, `list_tags` | New in v1.2 |
| **Builds** | `get_build`, `list_builds` | New in v1.2 |
| **Users** | `get_user`, `list_users`, `get_active_user` | |
| **Stacks** | `get_stack`, `list_stacks` | |
| **Components** | `get_stack_component`, `list_stack_components` | |
| **Flavors** | `get_flavor`, `list_flavors` | |
| **Pipelines** | `list_pipelines`, `get_pipeline_details` | |
| **Runs** | `get_pipeline_run`, `list_pipeline_runs` | |
| **Steps** | `get_run_step`, `list_run_steps`, `get_step_logs`, `get_step_code` | |
| **Schedules** | `get_schedule`, `list_schedules` | |
| **Services** | `get_service`, `list_services` | |
| **Connectors** | `get_service_connector`, `list_service_connectors` | |
| **Models** | `get_model`, `list_models`, `get_model_version`, `list_model_versions` | |
| **Artifacts** | `list_artifacts` | |
| **Secrets** | `list_secrets` | Names only |
| **Analysis** | `stack_components_analysis`, `recent_runs_analysis`, `most_recent_runs` | |
| **Execution** | `trigger_pipeline` | Prefer `snapshot_name_or_id` |
| **Deprecated** | `get_run_template`, `list_run_templates` | Use snapshot tools instead |

**When adding new tools:**
1. Add the tool to `server/zenml_server.py` following existing patterns
2. Update README.md tool inventory
3. If the tool is safe (read-only, no required IDs), add to `scripts/test_mcp_server.py` `safe_tools_to_test`
4. Run smoke tests: `uv run scripts/test_mcp_server.py server/zenml_server.py`

### Environment Setup

The server requires:
- Python 3.12+
- Dependencies managed via `uv` (preferred) or pip
- ZenML server URL and API key configured as environment variables

### Testing Infrastructure

- **PR Testing**: GitHub Actions runs tests on every PR (formatting checks + smoke tests)
- **Scheduled testing**: Comprehensive smoke tests run every 3 days with automated issue creation on failures
- **Manual testing**: Use the test script to verify MCP protocol functionality
- **CI/CD**: Uses UV with caching for fast dependency installation

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
- **Resources tab**: Browse exposed resources (none currently)
- **Prompts tab**: View prompt templates (none currently)
- **History**: See all previous tool calls in the session

### Project Structure

- `server/` - Main MCP server implementation
- `scripts/` - Development and testing utilities
- `assets/` - Project assets and images

- Root files include configuration for Desktop Extensions (DXT) support

### Important Implementation Details

- **Logging**: Configured to use stderr and suppress ZenML internal logging to prevent JSON protocol conflicts
- **Error Handling**: All tool functions wrapped with exception handling decorator
- **Lazy Loading**: ZenML client initialized only when needed to improve startup performance
- **Environment Variables**: Server configuration via `ZENML_STORE_URL` and
  `ZENML_STORE_API_KEY`

## Release Process

### Triggering a Release

Releases are done via GitHub Actions:

```bash
gh workflow run release.yml --repo zenml-io/mcp-zenml -f version=X.Y.Z
```

This triggers:
1. **Pre-release Tests**: Runs smoke tests and Docker build verification as a gate
2. **Release Orchestrator** (`release.yml`): Bumps version files, creates tag, builds `.mcpb` bundle
3. **Release Docker** (`release-docker.yml`): Triggered by `v*.*.*` tag push, builds Docker image, publishes to MCP Registry

**Note**: The release will fail if tests don't pass. This prevents releasing broken builds.

### Version Files

Three files must stay in sync (handled by `scripts/bump_version.py`):
- `VERSION` - Source of truth
- `manifest.json` - DXT/MCPB manifest
- `server.json` - MCP Registry server definition

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

If a release fails partway through, clean up before retrying:

```bash
# Delete failed release and tag
gh release delete vX.Y.Z --repo zenml-io/mcp-zenml --yes
git push origin --delete vX.Y.Z

# Then re-trigger with the corrected code
gh workflow run release.yml --repo zenml-io/mcp-zenml -f version=X.Y.Z
```

**Important**: The `release-docker.yml` workflow checks out code **at the tag**, not from HEAD. If you push a fix to main, you must delete and recreate the tag for the fix to take effect.
