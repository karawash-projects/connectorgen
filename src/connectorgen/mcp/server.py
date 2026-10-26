"""Connector -> MCP server: expose actions as tools for Claude and other agents.

Safety defaults:
- Destructive actions are left out unless allow_destructive=True.
- read_only=True exposes only safe (read-only) actions.
- Tool annotations tell clients which tools only read (readOnlyHint) and which
  can destroy data (destructiveHint), so they can ask the user first.
Each tool's input schema carries only the shared schemas it references, so
listing tools for a large API (Stripe has 1,500+ shared schemas) stays small.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from typing import Any

from ..errors import ConnectorError
from ..ir import Action, Connector, referenced_defs
from ..runtime.auth import Credentials

log = logging.getLogger("connectorgen")
MAX_DESCRIPTION = 1024
MANY_TOOLS = 128


def select_actions(connector: Connector, *, actions: list[str] | None = None,
                   tags: list[str] | None = None, read_only: bool = False,
                   allow_destructive: bool = False) -> list[Action]:
    """Pick actions to expose. `actions` accepts glob patterns (e.g. "list_*")."""
    chosen = []
    for a in connector.actions:
        if actions and not any(fnmatch.fnmatchcase(a.name, pat) for pat in actions):
            continue
        if tags and not set(tags) & set(a.tags):
            continue
        if read_only and not a.safe:
            continue
        if a.destructive and not allow_destructive:
            continue
        chosen.append(a)
    return chosen


def tool_for_action(connector: Connector, action: Action) -> Any:
    import mcp.types as t

    text = action.summary
    if action.description and action.description != action.summary:
        text = f"{text}\n\n{action.description}" if text else action.description
    if len(text) > MAX_DESCRIPTION:
        text = text[:MAX_DESCRIPTION] + "…"

    output = None
    if action.output_schema is not None:
        out = dict(action.output_schema)
        defs = referenced_defs(out, connector.schemas)
        resolved = connector.schemas.get(out["$ref"][len("#/$defs/"):]) \
            if isinstance(out.get("$ref"), str) and out["$ref"].startswith("#/$defs/") else out
        if isinstance(resolved, dict) and resolved.get("type") == "object":
            output = {**out, **({"$defs": defs} if defs else {})}

    return t.Tool(
        name=action.name,
        title=action.summary or None,
        description=text or action.name.replace("_", " "),
        input_schema=connector.input_schema(action, prune=True),
        output_schema=output,
        annotations=t.ToolAnnotations(
            title=action.summary or None,
            read_only_hint=action.safe,
            destructive_hint=action.destructive,
            idempotent_hint=action.method in ("GET", "PUT", "DELETE"),
            open_world_hint=True,
        ),
    )


def build_mcp_server(
    connector: Connector,
    credentials: Credentials | None = None,
    *,
    actions: list[str] | None = None,
    tags: list[str] | None = None,
    read_only: bool = False,
    allow_destructive: bool = False,
    client: Any = None,
    base_url: str | None = None,
) -> Any:
    """An mcp low-level Server whose tools are the connector's actions.
    `client` defaults to open_client(connector, credentials)."""
    import anyio
    import mcp.types as t
    from mcp.server.lowlevel import Server

    from .client import open_client

    exposed = select_actions(connector, actions=actions, tags=tags, read_only=read_only,
                             allow_destructive=allow_destructive)
    if not exposed:
        raise ConnectorError("No actions left to expose after filtering.")
    if len(exposed) > MANY_TOOLS:
        log.warning("Exposing %d tools; many MCP clients work poorly past ~%d. "
                    "Narrow with actions=/tags= filters.", len(exposed), MANY_TOOLS)
    by_name = {a.name: a for a in exposed}
    tools = [tool_for_action(connector, a) for a in exposed]
    structured = {tool.name: tool.output_schema is not None for tool in tools}
    if client is None:
        extra = {"base_url": base_url} if base_url and connector.mcp is None else {}
        client = open_client(connector, credentials, **extra)

    async def on_list_tools(ctx: Any, params: Any) -> Any:
        return t.ListToolsResult(tools=tools)

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        action = by_name.get(params.name)
        if action is None:
            return _error(f"Unknown tool '{params.name}'.")
        try:
            result = await anyio.to_thread.run_sync(
                lambda: client.execute(action, dict(params.arguments or {})))
        except ConnectorError as e:
            return _error(str(e))
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        return t.CallToolResult(
            content=[t.TextContent(type="text", text=text)],
            structured_content=result if structured[action.name] and isinstance(result, dict) else None,
        )

    def _error(message: str) -> Any:
        return t.CallToolResult(content=[t.TextContent(type="text", text=message)], is_error=True)

    server = Server(
        connector.name,
        version=connector.api_version or "0",
        title=connector.title or None,
        instructions=(connector.description or None) and connector.description[:2000],
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    server.connectorgen_client = client  # type: ignore[attr-defined]  # caller may close it
    return server


async def serve_stdio(server: Any) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def serve_http(server: Any, *, host: str = "127.0.0.1", port: int = 8000,
               path: str = "/mcp") -> None:
    """Streamable HTTP. Binds to localhost by default (with the SDK's DNS-rebinding
    protection); put an authenticating proxy in front before exposing it further."""
    import uvicorn

    app = server.streamable_http_app(streamable_http_path=path, host=host)
    uvicorn.run(app, host=host, port=port, log_level="warning")
