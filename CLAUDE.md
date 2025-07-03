# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Testing and Development
- **Run smoke tests**: `uv run scripts/test_mcp_server.py server/zenml_server.py`
- **Format code**: `./scripts/format.sh` (uses ruff for linting and formatting)
- **Run MCP server locally**: `uv run server/zenml_server.py`

### Code Quality
- **Format**: `bash scripts/format.sh`

## Architecture

### Core Components

The project is a Model Context Protocol (MCP) server that provides AI assistants with access to ZenML API functionality.

**Main Server File**: `server/zenml_server.py`
- Uses FastMCP framework for MCP protocol implementation
- Implements lazy initialization of ZenML client to avoid startup delays
- Provides comprehensive exception handling with the `@handle_exceptions` decorator
- Configures minimal logging to prevent JSON protocol interference

**Key Features**:
- Reads ZenML server configuration from environment variables (`ZENML_STORE_URL`, `ZENML_STORE_API_KEY`)
- Provides MCP tools for accessing ZenML entities (users, stacks, pipelines, runs, etc.)
- Supports triggering new pipeline runs via run templates
- Includes automated CI/CD testing with GitHub Actions

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
