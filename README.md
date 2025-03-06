# MCP-ZenML

Assumes that your local MCP settings will look something like this:

```json
{
    "mcpServers": {
        "zenml": {
            "command": "uv run path/to/zenml_server.py",
            "args": [],
            "env": {
                "ZENML_LOGGING_VERBOSITY": "WARN",
                "LOGLEVEL": "WARN",
                "PYTHONWARNINGS": "ignore",
                "NO_COLOR": "1",
                "TERM": "dumb",
                "FORCE_COLOR": "0",
                "ZENML_DISABLE_RICH_LOGGING": "1",
                "ZENML_LOGGING_COLORS_DISABLED": "true",
                "PYTHONIOENCODING": "UTF-8",
                "PYTHONUNBUFFERED": "1",
                "ZENML_STORE_URL": "https://your-zenml-server-goes-here.com",
                "ZENML_STORE_API_KEY": "your-api-key-here"
            }
        }
    }
}
```

# steps

- install uv
- get your store URL and API key from the ZenML server
- add the server to your config (including your store URL and API key)
