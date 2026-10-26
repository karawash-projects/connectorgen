"""MCP server -> Connector: each tool becomes an action.

Tool annotations map onto the same safety flags HTTP actions use:
- readOnlyHint true            -> method "GET", so `safe` is true (smoke tests may call it)
- otherwise                    -> method "POST"
- destructive                  -> not read-only and destructiveHint isn't false
  (the MCP spec's default for destructiveHint is true, so unannotated write tools
  count as destructive and the CLI asks for --allow-destructive)
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..errors import ConnectorError
from ..ingest.openapi import normalize_schema, snake_case
from ..ir import Action, AuthScheme, Connector, McpServerConfig, hash_spec
from ..runtime.auth import Credentials
from .client import AsyncMcpClient


def action_from_tool(tool: Any, used: set[str]) -> Action:
    ann = tool.annotations
    read_only = bool(ann and ann.read_only_hint)
    destructive = (not read_only) and not (ann is not None and ann.destructive_hint is False)
    name, n = snake_case(tool.name), 2
    base = name
    while name in used:
        name, n = f"{base}_{n}", n + 1
    used.add(name)
    title = tool.title or (ann.title if ann and ann.title else "")
    description = (tool.description or "").strip()
    summary = title or description.split("\n")[0][:100]
    notes = []
    output = tool.output_schema
    if output is not None and not isinstance(output, dict):
        output = None
    hints = {}
    if ann is not None:
        hints = {k: v for k, v in ann.model_dump(exclude_none=True).items() if k != "title"}
    if not description:
        notes.append("Tool has no description.")
    return Action(
        name=name,
        operation_id=tool.name,
        summary=summary,
        description=description,
        method="GET" if read_only else "POST",
        path="",
        output_schema=normalize_schema(output) if output else None,
        destructive=destructive,
        mcp_tool=tool.name,
        arguments_schema=normalize_schema(dict(tool.input_schema or {"type": "object"})),
        mcp_annotations=hints,
        notes=notes,
        provenance={"action": "mcp"},
    )


async def aconnector_from_mcp(
    config: McpServerConfig,
    *,
    name: str | None = None,
    auth: list[AuthScheme] | None = None,
    credentials: Credentials | None = None,
    env: dict[str, str] | None = None,
    server: Any = None,
    allow_secret_like_values: bool = False,
) -> Connector:
    """Connect to an MCP server, list its tools, and describe them as a Connector.
    Refuses configs that would write a secret into the connector file (pass those at
    runtime instead: env_passthrough names, or `auth` + credentials). The check is a
    heuristic; if it flags a value you know isn't secret, pass allow_secret_like_values=True."""
    problems = config.secret_problems()
    if problems and not allow_secret_like_values:
        raise ConnectorError(
            "This MCP config would save what look like credentials into the connector file ("
            + "; ".join(problems) + "). Pass secrets at runtime instead: --env NAME for "
            "variables, --bearer or --api-key-header for HTTP auth. If these really aren't "
            "secrets, pass --allow-secret-like-values (allow_secret_like_values=True).")
    skeleton = Connector(name=name or "mcp", mcp=config, auth=auth or [])
    async with AsyncMcpClient(skeleton, credentials, env=env, server=server) as client:
        tools = await client.list_tools()
        info = client.raw.server_info
        instructions = client.raw.instructions
    used: set[str] = set()
    actions = [action_from_tool(t, used) for t in tools]
    server_name = (info.title or info.name) if info else ""
    return Connector(
        name=name or snake_case(server_name or "mcp_server"),
        title=server_name or (name or "MCP server"),
        description=(instructions or (info.description if info else "") or "").strip(),
        api_version=(info.version if info else "") or "",
        auth=auth or [],
        actions=actions,
        mcp=config,
        source=config.describe(),
        spec_hash=hash_spec([t.model_dump(mode="json", exclude_none=True) for t in tools]),
        provenance={"connector": "mcp"},
    )


def connector_from_mcp(config: McpServerConfig, **kwargs: Any) -> Connector:
    """Sync version of aconnector_from_mcp (don't call from inside a running event loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(aconnector_from_mcp(config, **kwargs))
    raise ConnectorError("connector_from_mcp() was called inside an event loop; "
                         "await aconnector_from_mcp() instead.")
