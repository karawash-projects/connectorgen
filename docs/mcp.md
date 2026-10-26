# MCP

MCP works in both directions, and MCP connectors use the same `Connector` file format and the
same `execute(action, inputs)` call as HTTP connectors.

## Use an MCP server as a connector

```bash
# Local server over stdio. --env names a secret the server needs; only the NAME is saved.
connectorgen from-mcp --command "npx -y @modelcontextprotocol/server-filesystem /data" -o fs.json
connectorgen from-mcp --command "npx -y some-mcp-server" --env NOTES_TOKEN -o notes.json

# Remote server over Streamable HTTP, with a bearer token from CONNECTORGEN_TOKEN at runtime
connectorgen from-mcp --url https://mcp.example.com/mcp --bearer -o remote.json

connectorgen inspect fs.json
connectorgen call fs.json read_text_file -i path=/data/notes.txt
connectorgen smoke fs.json --param path=/data       # runs read-only tools only
```

```python
from connectorgen.ir import McpServerConfig
from connectorgen.mcp import connector_from_mcp, McpClient, AsyncMcpClient

config = McpServerConfig(transport="stdio", command="npx",
                         args=["-y", "@modelcontextprotocol/server-everything"])
connector = connector_from_mcp(config)          # lists tools -> actions
connector.save("everything.json")

with McpClient(connector) as client:            # sync hosts
    client.execute("get_sum", a=19, b=23)

async with AsyncMcpClient(connector) as client: # async hosts
    await client.execute("get_sum", a=19, b=23)
```

- **Tools map onto the same safety flags as HTTP actions.** `readOnlyHint` tools count as safe,
  so smoke tests may call them. Write tools are destructive unless they declare
  `destructiveHint: false` (the MCP spec's default is destructive), so `call` requires
  `--allow-destructive` for them.
- **Results are plain data:** structured content when the tool provides it, otherwise parsed
  JSON text, plain text, or a list of content blocks. A tool's `isError` raises `McpToolError`.
- **Inputs are validated** against the tool's schema before the call.
- **No secrets in connector files.** Stdio servers get a minimal environment plus only the
  variables listed in `env_passthrough` (values from `env=` or your environment). HTTP servers
  get credentials through the connector's auth schemes. Importing refuses a config that would
  save something that looks like a secret: credential-like headers (by name or value), tokens
  or `user:password@` in the URL, `--token X` / `NAME=value` arguments, connection strings with
  passwords, webhook URLs, or secret static env values. Environment placeholders like
  `${TOKEN}` are fine. It's a heuristic: an unlabeled value (`-t abc123`, a bare password as
  an argument) can't be told apart from an ordinary one, so keep secrets in `--env`; and if it
  flags something you know isn't secret, pass `--allow-secret-like-values`.
- `spec_hash` covers the tool list, so a changed tool set is detectable.

Tested against the official reference servers (`server-everything`, `server-filesystem`):
every tool imports, passes contract tests, and calls work. The filesystem server's own sandbox
refusals come back as clear errors.

## Serve any connector as an MCP server

```bash
connectorgen serve github.json --tag issues                  # stdio, for Claude Desktop etc.
connectorgen serve stripe.json --read-only -a "get_*"        # globs
connectorgen serve acme.json --transport http --port 8000    # Streamable HTTP on 127.0.0.1
```

- **Safe defaults:** destructive actions are excluded unless you pass `--allow-destructive`, and
  `--read-only` exposes only safe actions. Tool annotations (`readOnlyHint`,
  `destructiveHint`) let MCP clients ask the user before writes.
- **Small tool listings:** each tool's schema carries only the shared schemas it references,
  so even Stripe (1,500+ shared schemas) stays compact. Past ~128 tools you get a warning; narrow
  the set with `-a`/`--tag`.
- **Localhost by default:** HTTP binds to `127.0.0.1` with the SDK's DNS-rebinding
  protection. Put an authenticating proxy in front before exposing it beyond your machine.

[← Back to the README](../README.md)
