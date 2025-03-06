# MCP-ZenML

Assumes that your local MCP settings will look something like this:

```json
{
    "mcpServers": {
        "zenml": {
            "command": "/usr/local/bin/uv",
            "args": ["run", "path/to/zenml_server.py"],
            "env": {
                "LOGLEVEL": "INFO",
                "NO_COLOR": "1",
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "UTF-8",
                "ZENML_STORE_URL": "https://your-zenml-server-goes-here.com",
                "ZENML_STORE_API_KEY": "your-api-key-here"
            }
        }
    }
}
```

# steps

- install uv
- find the global path to your uv
- specify that path in the config
- get your store URL and API key from the ZenML server
- add the server to your config (including your store URL and API key)

## Logging

The server logs are written to a file in your home directory:
- Location: `~/.zenml-mcp/logs/zenml_server.log`
- Log level can be controlled with the `LOGLEVEL` environment variable (e.g., "DEBUG", "INFO", "WARNING", "ERROR")
