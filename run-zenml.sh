#!/bin/bash
cd path/to/mcp-zenml
source .venv/bin/activate

# FastMCP settings
export FASTMCP_DEBUG=true
export FASTMCP_LOG_LEVEL=DEBUG

# Disable all logging except warnings
export ZENML_LOGGING_VERBOSITY=WARN
export LOGLEVEL=WARN
export PYTHONWARNINGS=ignore

# Disable all colors and rich output
export NO_COLOR=1
export TERM=dumb
export FORCE_COLOR=0
export ZENML_DISABLE_RICH_LOGGING=1
export ZENML_LOGGING_COLORS_DISABLED=true
export ZENML_ANALYTICS_OPT_IN=false

# Ensure proper encoding and buffering
export PYTHONIOENCODING=UTF-8
export PYTHONUNBUFFERED=1

# Run server with stdout for JSON only
python zenml_server.py
