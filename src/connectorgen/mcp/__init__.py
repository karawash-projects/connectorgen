"""MCP support, both directions.

- Consume: an MCP server becomes a Connector (`connector_from_mcp`), callable with
  `McpClient` / `AsyncMcpClient`, or with `open_client` for any connector kind.
- Serve: any Connector becomes an MCP server (`build_mcp_server`, `serve_stdio`,
  `serve_http`).

Requires the `mcp` extra: pip install 'connectorgen[mcp]'.
"""

from .client import AsyncMcpClient, McpClient, open_client, tool_result_value
from .ingest import aconnector_from_mcp, connector_from_mcp
from .server import build_mcp_server, select_actions, serve_http, serve_stdio

__all__ = [
    "AsyncMcpClient",
    "McpClient",
    "aconnector_from_mcp",
    "build_mcp_server",
    "connector_from_mcp",
    "open_client",
    "select_actions",
    "serve_http",
    "serve_stdio",
    "tool_result_value",
]
