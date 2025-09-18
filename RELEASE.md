# Release Process

This repository ships releases via a single "Release Orchestrator" GitHub Actions workflow. It treats the root `VERSION` file as the single source of truth, auto-updates related files, builds the MCP bundle (`mcp-zenml.mcpb`), creates a tag and GitHub Release, and triggers downstream publishers.

## Quick Start

- Prerequisites
  - A GitHub PAT stored as repository secret `GH_RELEASE_PAT` with permissions to:
    - push to `main`
    - create tags and releases
    - trigger other workflows (use as the checkout token)
- Run the orchestrator
  1) Go to GitHub → Actions → "Release Orchestrator"
  2) Click "Run workflow"
  3) Inputs (all optional unless noted):
     - `version` (string): If provided, sets the exact version (SemVer). If omitted, the workflow reads from the `VERSION` file.
     - `prerelease` (boolean): Mark the GitHub Release as a prerelease. Default: false.
     - `dry_run` (boolean): Perform all steps except committing/pushing and tagging. Default: false.
  4) Click "Run workflow"

A weekly cron (Monday 09:00 UTC) also triggers the workflow as a reminder, but it won't push/tag if `dry_run` is set.

## How It Works

- Single source of truth: `VERSION`
  - If `version` input is provided, the workflow sets `VERSION` to that value (with validation).
  - Otherwise, it reads the version from the `VERSION` file.
- Version propagation
  - Runs `python scripts/bump_version.py [--version X.Y.Z]` to validate SemVer and update:
    - `manifest.json.version`
    - `server.json.version`
    - `server.json.packages[0].version`
- Manifest regeneration
  - Runs `python scripts/generate_manifest_fields.py` to analyze `server/zenml_server.py` and regenerate the `tools` and `prompts` arrays in `manifest.json`.
- Bundle build
  - Runs `bash scripts/build_mcpb.sh` to install dependencies deterministically and pack the MCP bundle:
    - Produces `mcp-zenml.mcpb` at the repo root.
- Commit, push, and tag (skipped if `dry_run: true`)
  - Commits `VERSION`, `manifest.json`, `server.json`, and `mcp-zenml.mcpb` with message:
    - `chore(release): vX.Y.Z`
  - Pushes to `main`.
  - Creates an annotated tag `vX.Y.Z` and pushes it.
- GitHub Release
  - Always runs `softprops/action-gh-release@v2` to attach `mcp-zenml.mcpb`.
  - Uses the resolved version (input or `VERSION`) for `tag_name` and `release_name`.
  - Sets `prerelease` from the input and `generate_release_notes: true`.
  - If the tag already exists, the step is tolerant and won't fail the workflow.

## Downstream Workflows

- `docker-publish.yml` (on push to main)
  - Builds and pushes the latest development Docker image(s) for the server.
  - Publishes `zenmldocker/mcp-zenml:latest` (and any other branch-based tags configured).
- `release-docker.yml` (on tag `v*`)
  - Builds and pushes versioned Docker images:
    - `zenmldocker/mcp-zenml:vX.Y.Z`
  - Uploads/attaches `mcp-zenml.mcpb` to the corresponding GitHub Release.
  - If configured, publishes to the MCP Registry based on `manifest.json`/`server.json`.

## Manual Recovery

If something goes wrong:

- Orchestrator dry-run
  - Re-run the orchestrator with `dry_run: true` to preview version propagation, manifest regeneration, and bundle build without pushing or tagging.
- Re-run downstream workflows
  - Manually dispatch `docker-publish.yml` on `main` if images didn't publish.
  - Re-run `release-docker.yml` on the `vX.Y.Z` tag to rebuild images and reattach assets.
- Build locally as a fallback
  - From repo root:
    - `bash scripts/build_mcpb.sh`
  - Then attach the bundle to the GitHub Release:
    - Using GitHub UI or `gh` CLI:
      - `gh release upload vX.Y.Z mcp-zenml.mcpb --clobber`

## Artifacts Matrix

- GitHub Release (per tag)
  - Asset: `mcp-zenml.mcpb`
  - Tag: `vX.Y.Z`
- Docker Images
  - `zenmldocker/mcp-zenml:latest` (main)
  - `zenmldocker/mcp-zenml:vX.Y.Z` (tag)
- MCP Registry (if enabled)
  - Publishes based on `manifest.json`/`server.json` from the tagged commit

## Notes

- Versioning: Strict SemVer is enforced by `scripts/bump_version.py`.
- Reproducibility: `scripts/build_mcpb.sh` installs dependencies into `server/lib` for deterministic packaging.
- Security: Use `GH_RELEASE_PAT` for the checkout token to ensure push/tag operations and triggering downstream workflows work correctly.


