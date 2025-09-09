# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# Install uv (pinned) from official distroless image so it's on PATH
COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=UTF-8 \
    PIP_NO_CACHE_DIR=1 \
    LOGLEVEL=WARNING \
    NO_COLOR=1 \
    ZENML_LOGGING_COLORS_DISABLED=true \
    ZENML_ENABLE_RICH_TRACEBACK=false \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1

# Optional but helpful: fresh CA certs for TLS reliability
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/
RUN uv pip install -r requirements.txt

# Security: non-root user
RUN useradd -m -u 10001 appuser
USER appuser

# Copy only what we need to run the server in stdio mode
COPY --chown=appuser:appuser server/zenml_server.py /app/server/zenml_server.py

# OCI labels (will be enriched/overridden by CI metadata)
LABEL org.opencontainers.image.title="ZenML MCP Server" \
      org.opencontainers.image.description="Model Context Protocol server for ZenML" \
      org.opencontainers.image.source="https://github.com/zenml-io/mcp-zenml" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["python", "-u", "server/zenml_server.py"]