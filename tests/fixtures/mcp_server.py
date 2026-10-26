"""A small real MCP server (stdio) used by the tests: `python mcp_server.py`."""

import json
import os

import anyio
import mcp.types as t
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

TOOLS = [
    t.Tool(
        name="add",
        title="Add numbers",
        description="Add two integers.",
        input_schema={"type": "object", "required": ["a", "b"],
                      "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}},
        output_schema={"type": "object", "required": ["sum"],
                       "properties": {"sum": {"type": "integer"}}},
        annotations=t.ToolAnnotations(read_only_hint=True),
    ),
    t.Tool(
        name="list-notes",
        description="List notes, optionally filtered by tag.",
        input_schema={"type": "object", "properties": {
            "tag": {"type": "string", "enum": ["home", "work"]}}},
        annotations=t.ToolAnnotations(read_only_hint=True),
    ),
    t.Tool(
        name="createNote",
        description="Create a note.",
        input_schema={"type": "object", "required": ["text"],
                      "properties": {"text": {"type": "string"}}},
        annotations=t.ToolAnnotations(read_only_hint=False, destructive_hint=False),
    ),
    t.Tool(
        name="delete_note",
        description="Delete a note forever.",
        input_schema={"type": "object", "required": ["id"],
                      "properties": {"id": {"type": "string"}}},
        # no annotations: per the MCP spec this counts as a destructive write
    ),
    t.Tool(
        name="whoami",
        description="Report whether the secret env var reached the server.",
        input_schema={"type": "object", "properties": {}},
        annotations=t.ToolAnnotations(read_only_hint=True),
    ),
    t.Tool(
        name="explode",
        description="Always fails.",
        input_schema={"type": "object", "properties": {}},
        annotations=t.ToolAnnotations(read_only_hint=True),
    ),
]


if os.environ.get("NOTES_VARIANT") == "v2":
    # A newer server version: one tool renamed, one removed, one added, one gains a required arg.
    _by = {tool.name: tool for tool in TOOLS}
    _by["whoami"] = _by["whoami"].model_copy(update={"name": "who-am-i"})
    _by.pop("explode")
    _add = _by["add"]
    _by["add"] = _add.model_copy(update={"input_schema": {
        **_add.input_schema, "required": ["a", "b", "precision"],
        "properties": {**_add.input_schema["properties"], "precision": {"type": "integer"}}}})
    _by["archive_note"] = t.Tool(name="archive_note", description="Archive a note.",
                                 input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
                                 annotations=t.ToolAnnotations(read_only_hint=False,
                                                               destructive_hint=False))
    TOOLS = list(_by.values())


def text(value):
    return [t.TextContent(type="text", text=value if isinstance(value, str) else json.dumps(value))]


async def on_list_tools(ctx, params):
    return t.ListToolsResult(tools=TOOLS)


async def on_call_tool(ctx, params):
    args = params.arguments or {}
    if params.name == "add":
        if os.environ.get("NOTES_VARIANT") == "v2" and "precision" not in args:
            return t.CallToolResult(content=text("invalid arguments: missing required argument 'precision' (integer)"),
                                    is_error=True)
        result = {"sum": args["a"] + args["b"]}
        return t.CallToolResult(content=text(result), structured_content=result)
    if params.name == "list-notes":
        notes = [{"id": "n1", "text": "milk", "tag": "home"}, {"id": "n2", "text": "deck", "tag": "work"}]
        if "tag" in args:
            notes = [n for n in notes if n["tag"] == args["tag"]]
        return t.CallToolResult(content=text(notes))
    if params.name == "createNote":
        return t.CallToolResult(content=text(f"created: {args['text']}"))
    if params.name == "delete_note":
        return t.CallToolResult(content=text({"deleted": args["id"]}))
    if params.name in ("whoami", "who-am-i"):
        return t.CallToolResult(content=text({"has_secret": os.environ.get("NOTES_TOKEN") == "s3cret",
                                              "leaked": "OTHER_SECRET" in os.environ}))
    if params.name == "explode":
        return t.CallToolResult(content=text("database is on fire"), is_error=True)
    return t.CallToolResult(content=text(f"unknown tool {params.name}"), is_error=True)


server = Server("notes-server", version="1.2.0", title="Notes",
                instructions="A tiny notes service for tests.",
                on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
