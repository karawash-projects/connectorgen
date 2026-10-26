"""Connector -> MCP server, plus full round trips (in-process and over real HTTP)."""

import asyncio
import json
import socket
import threading
import time

import mcp.types as t
import pytest
import respx
from mcp import Client

from connectorgen import Credentials
from connectorgen.ir import AuthScheme, McpServerConfig
from connectorgen.mcp import McpClient, build_mcp_server, connector_from_mcp, select_actions

BASE = "https://us.api.petstore.test/v1"


def call(server, fn):
    async def run():
        async with Client(server) as c:
            return await fn(c)
    return asyncio.run(run())


def tools_of(server):
    return {tool.name: tool for tool in call(server, lambda c: c.list_tools()).tools}


@pytest.fixture
def server(petstore):
    s = build_mcp_server(petstore, Credentials(api_key="k"))
    yield s
    s.connectorgen_client.close()


def test_tools_and_annotations(server):
    tools = tools_of(server)
    assert "delete_pet" not in tools                      # destructive: opt-in only
    assert {"list_pets", "get_pet", "create_pet"} <= set(tools)
    get_pet = tools["get_pet"]
    assert get_pet.annotations.read_only_hint is True
    assert get_pet.annotations.destructive_hint is False
    assert tools["create_pet"].annotations.read_only_hint is False
    assert get_pet.input_schema["required"] == ["petId"]


def test_input_schemas_carry_only_referenced_defs(server):
    tools = tools_of(server)
    assert set(tools["create_pet"].input_schema["$defs"]) == {"Pet"}
    assert "$defs" not in tools["get_pet"].input_schema
    assert tools["get_pet"].output_schema["$ref"] == "#/$defs/Pet"   # object output -> structured


def test_filters(petstore):
    names = lambda **kw: [a.name for a in select_actions(petstore, **kw)]
    assert "delete_pet" in names(allow_destructive=True)
    assert set(names(read_only=True)) == {a.name for a in petstore.actions if a.safe}
    assert names(actions=["list_*"]) == ["list_pets", "list_customers", "list_categories", "list_events"]
    assert names(tags=["pets"]) == ["list_pets"]


@respx.mock
def test_call_tool_runs_the_action(server):
    route = respx.get(f"{BASE}/pets/7").respond(json={"id": 7, "name": "Rex"})
    result = call(server, lambda c: c.call_tool("get_pet", {"petId": 7}))
    assert not result.is_error
    assert result.structured_content == {"id": 7, "name": "Rex"}
    assert json.loads(result.content[0].text) == {"id": 7, "name": "Rex"}
    assert route.calls.last.request.headers["X-API-Key"] == "k"


@respx.mock
def test_errors_come_back_as_tool_errors(server):
    respx.get(f"{BASE}/pets/9").respond(404, json={"error": "not found"})
    result = call(server, lambda c: c.call_tool("get_pet", {"petId": 9}))
    assert result.is_error and "HTTP 404" in result.content[0].text
    result = call(server, lambda c: c.call_tool("get_pet", {"petId": "x"}))
    assert result.is_error and "is not of type 'integer'" in result.content[0].text
    result = call(server, lambda c: c.call_tool("delete_pet", {"petId": 1}))
    assert result.is_error and "Unknown tool" in result.content[0].text   # not exposed


@respx.mock
def test_round_trip_http_connector_to_mcp_and_back(petstore, server):
    """HTTP connector -> served as MCP -> imported as an MCP connector -> executed."""
    respx.get(f"{BASE}/pets/3").respond(json={"id": 3, "name": "Max"})
    mirrored = connector_from_mcp(McpServerConfig(transport="stdio", command="unused"),
                                  server=server)
    assert {a.name for a in mirrored.actions} == {tool for tool in tools_of(server)}
    assert mirrored.action("get_pet").safe and not mirrored.action("create_pet").safe
    with McpClient(mirrored, server=server) as client:
        assert client.execute("get_pet", petId=3) == {"id": 3, "name": "Max"}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_streamable_http_round_trip_with_bearer_auth():
    """Real sockets: serve an MCP server over Streamable HTTP that requires a bearer
    token, import it with connector_from_mcp, call a tool through McpClient."""
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse
    from mcp.server.lowlevel import Server

    async def on_list_tools(ctx, params):
        return t.ListToolsResult(tools=[t.Tool(
            name="ping", description="Pong.", input_schema={"type": "object", "properties": {}},
            annotations=t.ToolAnnotations(read_only_hint=True))])

    async def on_call_tool(ctx, params):
        return t.CallToolResult(content=[t.TextContent(type="text", text='{"pong": true}')])

    app = Server("remote", version="2", on_list_tools=on_list_tools,
                 on_call_tool=on_call_tool).streamable_http_app()

    class RequireToken(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.headers.get("authorization") != "Bearer tok123":
                return PlainTextResponse("unauthorized", status_code=401)
            return await call_next(request)

    app.add_middleware(RequireToken)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        config = McpServerConfig(transport="http", url=f"http://127.0.0.1:{port}/mcp")
        auth = [AuthScheme(id="bearer", type="bearer")]
        remote = connector_from_mcp(config, auth=auth, credentials=Credentials(token="tok123"))
        assert [a.name for a in remote.actions] == ["ping"]
        with McpClient(remote, Credentials(token="tok123")) as client:
            assert client.execute("ping") == {"pong": True}
        with pytest.raises(Exception, match="Couldn't connect"):
            McpClient(remote, Credentials(token="wrong"))
    finally:
        server.should_exit = True
        thread.join(10)
