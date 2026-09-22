# Release Process

The manually dispatched **Release Orchestrator** uses `VERSION` as the release
version, validates the candidate, builds the MCP bundle, and publishes only
when `dry_run` is false.

## Preparing a release

1. Update the candidate version with
   `python scripts/bump_version.py --version X.Y.Z`. This validates SemVer and
   updates `VERSION`, `manifest.json`, `server.json`, the OCI image identifier,
   and `pyproject.toml`.
2. Regenerate and check the compact manifest with
   `python scripts/generate_manifest_fields.py` and
   `python scripts/generate_manifest_fields.py --check`.
3. Run the credential-free tests from `.github/workflows/pr-test.yml`.
4. Run `bash scripts/run_disposable_resource_integration.sh`. PR and release CI
   use this command to start a fresh loopback-only ZenML 0.96.4 OSS server, run
   persisted CRUD and same-name project-isolation receipts, and remove the
   temporary server and database. No repository environment, self-hosted
   runner, or ZenML credential is required.
5. To collect separate trigger, deployment, wait-condition, and
   resource-request evidence, use a compatible externally provisioned target,
   set `ZENML_MCP_ACTION_INTEGRATION=1` and supply the exact disposable fixture
   UUIDs in `ZENML_MCP_ACTION_FIXTURE`. A skipped gate is an incomplete receipt.
6. To collect separate restricted-access evidence, set
   `ZENML_MCP_RESTRICTED_INTEGRATION=1` with
   `ZENML_MCP_RESTRICTED_API_KEY` to prove the restricted read through a
   separately credentialed MCP process. ZenML's local OSS server disables
   authentication and its SQL store cannot execute replay or external
   deployments, so these two opt-in receipts are not part of the default
   release gate. Set `ZENML_MCP_REQUIRE_COMPLETE_INTEGRATION=1` only when an
   external target supplies every prerequisite.
7. Build and verify both distributions. CI initializes MCP inside the Docker
   image and inside an unpacked `mcp-zenml.mcpb`, then checks the exact compact
   inventory, packaged modules, dependency versions, and Python requirements.
   The tag workflow runs both amd64 and arm64 Docker candidates before pushing.
   The release workflow builds the candidate once, then the matrix resolves and
   runs those exact bytes on Linux, macOS, and Windows with Python 3.12, 3.13,
   and 3.14. The release job publishes that same candidate.

The MCPB uses manifest 0.4 and the UV runtime. It contains source and a small
`pyproject.toml`; UV resolves the pinned MCP 2.2.0 and ZenML 0.96.4 environment
for macOS, Windows, or Linux at first installation. The bundle does not contain
host-specific native Python extensions. `mcpb-uv.lock` is the committed source
for its full dependency graph, so ordinary builds resolve Python dependencies
offline and produce the same lock. Use `MCPB_REFRESH_LOCK=1` only for an
intentional dependency refresh.

## Running the orchestrator

Open GitHub Actions, select **Release Orchestrator**, and choose **Run
workflow**. The inputs are:

- `version`: an optional exact SemVer; otherwise the workflow reads `VERSION`.
- `prerelease`: marks the GitHub release as a prerelease.
- `dry_run`: builds and validates the candidate without committing, pushing,
  tagging, creating a GitHub release, publishing images, or updating the
  registry. The workflow uses a seven-day internal artifact to pass the exact
  candidate between validation jobs.

With `dry_run` disabled, the workflow commits `VERSION`, `manifest.json`,
`server.json`, `pyproject.toml`, `mcpb-uv.lock`, and `mcp-zenml.mcpb`, pushes
`main`, creates an annotated `vX.Y.Z` tag, and creates or updates the GitHub
release. Downstream workflows publish the `latest` and versioned Docker images
and update the MCP registry from the tagged commit.

`GH_RELEASE_PAT` must allow pushes to `main`, tags, GitHub releases, and
downstream workflow triggers. The registry publisher uses GitHub OIDC.
The disposable OSS integration job runs on GitHub-hosted Ubuntu and provisions
its own loopback ZenML target. It requires no `release-integration` environment,
self-hosted runner, or ZenML secrets. The release gate fails if that server does
not start or if the persisted CRUD or project-isolation receipt fails.

## Version 2.0.0 candidate receipt

- Runtime targets: MCP Python SDK 2.2.0, ZenML SDK and server 0.96.4, Python
  3.12 through 3.14.
- Default capability: compact/read-write, 16 tools. Optional inventories:
  compact/read-only 11, legacy/read-write 57, legacy/read-only 52.
- Credential-free evidence: legacy contracts, SDK bindings, registry coverage,
  generic reads and writes, finite actions, write policy, stdio and real
  localhost HTTP, timeout and cancellation outcomes, Apps browser flows,
  manifest consistency, and packaged discovery.
- Live OSS evidence required before release: disposable CRUD and same-name
  project isolation against the exact ZenML 0.96.4 server started by CI.
- Optional external evidence: restricted-credential rejection and
  feature-enabled actions. Record the target version, fixture IDs, commands,
  and cleanup result when collecting either opt-in receipt; do not describe a
  skipped gate as passed.
- Excluded capabilities: ZenML Cloud control-plane and Resource Manager
  administration, user and credential administration, secret-value CRUD,
  connector login and verification, raw webhook events, and aggregate debugging
  or lineage tools.
- Remote HTTP limitation: the server does not authenticate MCP callers. A
  remotely reachable deployment requires an authenticated perimeter, and its
  release receipt must show that an unauthenticated request is rejected before
  reaching MCP.

## Recovery

Use a dry run to reproduce version propagation, manifest generation, bundle
packing, and distribution checks without external changes. If a downstream
publisher fails after a tag exists, rerun the matching workflow for that tag.
Attach a rebuilt bundle manually only after its version and discovery receipt
match the tagged commit.

Try the least destructive option first:

- Rerunning `release.yml` is safe when `vX.Y.Z` already exists, points at the
  current main commit, and the candidate changes no release file. It reuses
  the tag and re-uploads the bundle with `gh release upload --clobber`. It
  fails if the tag points at a different commit.
- If the Docker job failed after tagging, rerun that workflow run with
  `gh run rerun <run-id>`.
- If only the MCP Registry publish failed, for example on a schema or
  description error, fix `server.json` on main while `VERSION` still equals
  `X.Y.Z`, then run the registry-only recovery job:
  `gh workflow run release-docker.yml --repo zenml-io/mcp-zenml -f release_tag=vX.Y.Z`.
  It reads `server.json` from main rather than the tag, checks that the tag
  exists, and does not rebuild or push Docker images.
- If the code at the tag itself is wrong, delete the release and the tag
  (`gh release delete vX.Y.Z --repo zenml-io/mcp-zenml --yes` and
  `git push origin --delete vX.Y.Z`), fix main, and run the orchestrator
  again. The tag-triggered Docker job builds from the tag, so a fix on main
  does not reach the image until the tag is recreated.

Published artifacts are the `mcp-zenml.mcpb` GitHub release asset,
`zenmldocker/mcp-zenml:latest`, `zenmldocker/mcp-zenml:X.Y.Z`, and the MCP
registry entry derived from `manifest.json` and `server.json`.
