# MCP Server for ZenML
[![Trust Score](https://archestra.ai/mcp-catalog/api/badge/quality/zenml-io/mcp-zenml)](https://archestra.ai/mcp-catalog/zenml-io__mcp-zenml)

This project implements a [Model Context Protocol
(MCP)](https://modelcontextprotocol.io/introduction) server for interacting with
the [ZenML](https://zenml.io) API.

![ZenML MCP Server](assets/mcp-zenml.png)

## What is MCP?

The Model Context Protocol (MCP) is an open protocol that standardizes how
applications provide context to Large Language Models (LLMs). It acts like a
"USB-C port for AI applications" - providing a standardized way to connect AI
models to different data sources and tools.

MCP follows a client-server architecture where:
- **MCP Hosts**: Programs like Claude Desktop or IDEs that want to access data through MCP
- **MCP Clients**: Protocol clients that maintain 1:1 connections with servers
- **MCP Servers**: Lightweight programs that expose specific capabilities through the standardized protocol
- **Local Data Sources**: Your computer's files, databases, and services that MCP servers can securely access
- **Remote Services**: External systems available over the internet that MCP servers can connect to

## What is ZenML?

ZenML is an open-source platform for building and managing ML and AI pipelines.
It provides a unified interface for managing data, models, and experiments.

For more information, see the [ZenML website](https://zenml.io) and [our documentation](https://docs.zenml.io).

## Features

The server provides MCP tools to access core read functionality from the ZenML
server, providing a way to get live information about:

- Users
- Stacks
- Pipelines
- Pipeline runs
- Pipeline steps
- Services
- Stack components
- Flavors
- Pipeline run templates
- Schedules
- Artifacts (metadata about data artifacts, not the data itself)
- Service Connectors
- Step code
- Step logs (if the step was run on a cloud-based stack)

It also allows you to trigger new pipeline runs (if a run template is present).

*Note: We're continuously improving this integration based on user feedback.
Please join our [Slack community](https://zenml.io/slack) to share your experience
and help us make it even better!*

## Quick Setup via Dashboard (Recommended)

The easiest way to set up the ZenML MCP Server is through your ZenML dashboard's **MCP Settings page**.

![MCP Settings Page](assets/mcp-settings-page.gif)

Navigate to **Settings → MCP** in your ZenML dashboard to get:

- **Pre-configured snippets** for your specific server URL and credentials
- **One-click installation** via deep links for supported IDEs
- **Copy-paste configurations** for VS Code, Claude Desktop, Cursor, Claude Code, OpenAI Codex, and more
- **Docker and uv options** based on your preference

### ZenML Pro Users

The MCP Settings page lets you generate a Personal Access Token (PAT) with a single click. The token is automatically included in all generated configuration snippets.

### ZenML OSS Users

1. First create a service account token via **Settings → Service Accounts**
2. Paste the token into the MCP Settings page
3. Copy the generated configuration for your IDE

---

**Prefer manual setup?** See the detailed instructions below.

## Testing & Quality Assurance

This project includes automated testing to ensure the MCP server remains functional:

- **🔄 Automated Smoke Tests**: A comprehensive smoke test runs every 3 days via GitHub Actions
- **🚨 Issue Creation**: Failed tests automatically create GitHub issues with detailed debugging information
- **⚡ Fast CI**: Uses UV with caching for quick dependency installation and testing
- **🧪 Manual Testing**: You can run the smoke test locally using `uv run scripts/test_mcp_server.py server/zenml_server.py`

The automated tests verify:
- MCP protocol connection and handshake
- Server initialization and tool discovery  
- Basic tool functionality (when ZenML server is accessible)
- Resource and prompt enumeration

## Manual Setup

### Prerequisites

You will need to have access to a deployed ZenML server. If you don't have one,
you can sign up for a free trial at [ZenML Pro](https://cloud.zenml.io) and we'll manage the deployment for you.

> **Tip:** Once you have a ZenML server, check out the [MCP Settings page](#quick-setup-via-dashboard-recommended) in your dashboard for the easiest setup experience.

You will also (probably) need to have `uv` installed locally. For more information, see
the [`uv` documentation](https://docs.astral.sh/uv/getting-started/installation/).
We recommend installation via their installer script or via `brew` if using a
Mac. (Technically you don't *need* it, but it makes installation and setup easy.)

You will also need to clone this repository somewhere locally:

```bash
git clone https://github.com/zenml-io/mcp-zenml.git
```

### Your MCP config file

The MCP config file is a JSON file that tells the MCP client how to connect to
your MCP server. Different MCP clients will use or specify this differently. Two
commonly-used MCP clients are [Claude Desktop](https://claude.ai/download) and
[Cursor](https://www.cursor.com/), for which we provide installation instructions
below.

You will need to specify your ZenML MCP server in the following format:

```json
{
    "mcpServers": {
        "zenml": {
            "command": "/usr/local/bin/uv",
            "args": ["run", "path/to/server/zenml_server.py"],
            "env": {
                "LOGLEVEL": "WARNING",
                "NO_COLOR": "1",
                "ZENML_LOGGING_COLORS_DISABLED": "true",
                "ZENML_LOGGING_VERBOSITY": "WARN",
                "ZENML_ENABLE_RICH_TRACEBACK": "false",
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "UTF-8",
                "ZENML_STORE_URL": "https://your-zenml-server-goes-here.com",
                "ZENML_STORE_API_KEY": "your-api-key-here"
            }
        }
    }
}
```

There are four dummy values that you will need to replace:

- the path to your locally installed `uv` (the path listed above is where it
  would be on a Mac if you installed it via `brew`)
- the path to the `zenml_server.py` file (this is the file that will be run when
  you connect to the MCP server). This file is located inside this repository at
  the root. You will need to specify the exact full path to this file.
- the ZenML server URL (this is the URL of your ZenML server. You can find this
  in the ZenML Cloud UI). It will look something like `https://d534d987a-zenml.cloudinfra.zenml.io`.
- the ZenML server API key (this is the API key for your ZenML server. You can
  find this in the ZenML Cloud UI or [read these
  docs](https://docs.zenml.io/how-to/manage-zenml-server/connecting-to-zenml/connect-with-a-service-account)
  on how to create one. For the purposes of the ZenML MCP server we recommend
  using a service account.)

You are free to change the way you run the MCP server Python file, but using
`uv` will probably be the easiest option since it handles the environment and
dependency installation for you.


### Installation for use with Claude Desktop

> **Quick alternative:** Use the MCP Settings page in your ZenML dashboard (Settings → MCP) to get pre-configured installation instructions and deep links for Claude Desktop.

You will need to have the latest version of [Claude Desktop](https://claude.ai/download) installed.

You can simply open the Settings menu and drag the `mcp-zenml.mcpb` file from the
root of this repository onto the menu and it will guide you through the
installation and setup process. You'll need to add your ZenML server URL and API key.

Note: MCP bundles (`.mcpb`) replace the older Desktop Extensions (`.dxt`) format; existing `.dxt` files still work in Claude Desktop.

#### Optional: Improving ZenML Tool Output Display

For a better experience with ZenML tool results, you can configure Claude to
display the JSON responses in a more readable format. In Claude Desktop, go to
Settings → Profile, and in the "What personal preferences should Claude consider
in responses?" section, add something like the following (or use these exact
words!):

```markdown
When using zenml tools which return JSON strings and you're asked a question, you might want to consider using markdown tables to summarize the results or make them easier to view!
```

This will encourage Claude to format ZenML tool outputs as markdown tables,
making the information much easier to read and understand.

### Installation for use with Cursor

> **Quick alternative:** The MCP Settings page in your ZenML dashboard (Settings → MCP) can generate the exact `mcp.json` content with your credentials pre-filled.

You will need to have [Cursor](https://www.cursor.com/) installed.

Cursor works slightly differently to Claude Desktop in that you specify the
config file on a per-repository basis. This means that if you want to use the
ZenML MCP server in multiple repos, you will need to specify the config file in
each of them.

To set it up for a single repository, you will need to:

- create a `.cursor` folder in the root of your repository
- inside it, create a `mcp.json` file with the content above
- go into your Cursor settings and click on the ZenML server to 'enable' it.

In our experience, sometimes it shows a red error indicator even though it is
working. You can try it out by chatting in the Cursor chat window. It will let
you know if is able to access the ZenML tools or not.

## Docker Image

You can run the server as a Docker container. The process communicates over stdio, so it will wait for an MCP client connection. Pass your ZenML credentials via environment variables.

### Prebuilt Images (Docker Hub)

Pull the latest multi-arch image:

```bash
docker pull zenmldocker/mcp-zenml:latest
```

Versioned releases are tagged as `vX.Y.Z`:

```bash
docker pull zenmldocker/mcp-zenml:v1.0.0
```

Run with your ZenML credentials (stdio mode):

```bash
docker run -i --rm \
  -e ZENML_STORE_URL="https://your-zenml-server.example.com" \
  -e ZENML_STORE_API_KEY="your-api-key" \
  zenmldocker/mcp-zenml:latest
```

### Canonical MCP config using Docker

```json
{
  "mcpServers": {
    "zenml": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "ZENML_STORE_URL=https://...",
        "-e", "ZENML_STORE_API_KEY=ZENKEY_...",
        "-e", "ZENML_ACTIVE_PROJECT_ID=...",
        "-e", "LOGLEVEL=WARNING",
        "-e", "NO_COLOR=1",
        "-e", "ZENML_LOGGING_COLORS_DISABLED=true",
        "-e", "ZENML_LOGGING_VERBOSITY=WARN",
        "-e", "ZENML_ENABLE_RICH_TRACEBACK=false",
        "-e", "PYTHONUNBUFFERED=1",
        "-e", "PYTHONIOENCODING=UTF-8",
        "zenmldocker/mcp-zenml:latest"
      ]
    }
  }
}
```

### Build Locally

From the repository root:

```bash
docker build -t zenmldocker/mcp-zenml:local .
```

Run the locally built image:

```bash
docker run -i --rm \
  -e ZENML_STORE_URL="https://your-zenml-server.example.com" \
  -e ZENML_STORE_API_KEY="your-api-key" \
  zenmldocker/mcp-zenml:local
```

## MCP Bundles (.mcpb)

This project uses MCP Bundles (`.mcpb`) — the successor to Anthropic's Desktop Extensions (DXT). MCP Bundles package an entire MCP server (including dependencies) into a single file with user-friendly configuration.

Note on rename: MCP Bundles replace the older `.dxt` format. Claude Desktop remains backward‑compatible with existing `.dxt` files, but we now ship `mcp-zenml.mcpb` and recommend using it going forward.

The `mcp-zenml.mcpb` file in the repository root contains everything needed to run the ZenML MCP server, eliminating the need for complex manual installation steps. This makes powerful ZenML integrations accessible to users without requiring technical setup expertise.

When you drag and drop the `.mcpb` file into Claude Desktop's settings, it automatically handles:
- Runtime dependency installation
- Secure configuration management  
- Cross-platform compatibility
- User-friendly setup process

For more information, see Anthropic's announcement of Desktop Extensions (DXT) and related MCP bundle packaging guidance in their documentation: https://www.anthropic.com/engineering/desktop-extensions

## Published on the Anthropic MCP Registry

This MCP server is published to the official Anthropic MCP Registry and is discoverable by compatible hosts. On each **tagged release**, our CI updates the registry entry via the registry’s `mcp-publisher` CLI using GitHub OIDC, so you can install or discover the **ZenML MCP Server** directly wherever the registry is supported (e.g., Claude Desktop’s Extensions catalog).

- **Always up to date:** the registry entry is refreshed with every release from the tagged commit’s `manifest.json` and `server.json`.
- **Alternate install paths:** you can still install locally via the packaged `.mcpb` bundle (see above) or run the Docker image.

Learn more about the registry here:
- Anthropic MCP Registry (community repo): https://github.com/modelcontextprotocol/registry
