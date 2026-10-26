# connectorgen

[![CI](https://github.com/karawash-projects/connectorgen/actions/workflows/ci.yml/badge.svg)](https://github.com/karawash-projects/connectorgen/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/karawash-projects/connectorgen/blob/main/LICENSE)
![Python 3.10–3.13](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue.svg)

**Connectors for any HTTP API or MCP server, generated from what already exists, kept working as
APIs change.**

Point connectorgen at an OpenAPI or Swagger spec, an API's documentation pages, or an MCP
server, and it generates a connector: actions with input schemas, auth, pagination and triggers.
Your app runs any connector the same way, with one `execute(action, inputs)` call. When the API
changes, connectorgen detects it, regenerates the connector, keeps your customizations, and
proposes the update for approval.

A connector is **data, not code** (one JSON file), so it can be validated, diffed, tested and
repaired. It's built as a library for workflow and integration tools, with a CLI on top.

```
OpenAPI / Swagger 2.0 ─┐
API docs (+ LLM) ──────┼─► Connector (JSON) ─► one runtime: execute · paginate · triggers · OAuth
MCP server ────────────┘         │                └─► or serve it as an MCP server
                                 └─► contract tests · live smoke tests · check/diff/apply when APIs change
```

Tested on 740 real-world API specs (99.8% of 43,000 generated actions pass their contract tests),
plus the full Stripe and GitHub APIs and the official MCP reference servers.

**Works with any LLM, and needs none.** Generating, running, testing and repairing connectors
use no model. The optional AI features (filling gaps, reading docs, diagnosing rejected requests)
work with Claude, OpenAI, or any OpenAI-compatible model, including local ones via Ollama.

## Install

```bash
pip install connectorgen              # core: generate, run, test, repair
pip install "connectorgen[mcp]"       # + MCP client/server
pip install "connectorgen[ai]"        # + the Anthropic SDK (other LLM providers need no extra)
```

## Quick start

### CLI

```bash
# 1. Generate a connector from an OpenAPI 3.x or Swagger 2.0 spec (URL or file)
connectorgen generate https://example.com/openapi.yaml -o example.json
connectorgen convert swagger.json -o openapi.yaml     # or just convert Swagger 2.0 to OpenAPI 3

# 2. See what it found
connectorgen inspect example.json

# 3. Call an action (credentials come from env vars)
export CONNECTORGEN_API_KEY=...        # or CONNECTORGEN_TOKEN / _USERNAME + _PASSWORD
connectorgen call example.json get_pet -i petId=42
connectorgen call example.json list_pets -i limit=50 --paginate --max-pages 3
connectorgen call example.json create_pet --body pet.json
connectorgen call example.json delete_pet -i petId=42 --allow-destructive

# 4. Test it offline, and keep it current
connectorgen test example.json
connectorgen check example.json        # re-reads the spec; proposes an update if it changed
```

### Python

```python
from connectorgen import connector_from_openapi, ConnectorClient, Credentials

connector = connector_from_openapi("openapi.yaml")
connector.save("petstore.json")           # store / version / diff this

with ConnectorClient(connector, Credentials(api_key="...")) as client:
    pet = client.execute("get_pet", petId=42)
    client.execute("create_pet", body={"name": "Rex"})
    for pet in client.paginate("list_pets", page_size=100):
        print(pet["name"])
```

Inputs are validated against each action's JSON Schema before any request goes out
(`InputValidationError`); non-2xx responses raise `ActionHTTPError` with status and body.

### MCP, both ways

```bash
# Use an MCP server as a connector (--env names a secret the server needs; only the name is saved)
connectorgen from-mcp --command "npx -y @modelcontextprotocol/server-filesystem /data" -o fs.json
connectorgen call fs.json read_text_file -i path=/data/notes.txt

# Serve any connector as an MCP server (destructive actions are left out unless allowed)
connectorgen serve github.json --tag issues
```

## Embedding in a workflow tool

Everything a host needs comes from the connector file, whatever its source:

| Host need | API |
|---|---|
| Store a connector | `connector.save(path)` / `Connector.load(path)`: one JSON format for HTTP and MCP |
| Build the step's input form | `connector.input_schema(action, prune=True)`: self-contained JSON Schema |
| Show the step | `action.summary`, `action.description`, `action.safe`, `action.destructive` |
| Run a step | `with open_client(connector, creds) as c: c.execute(action, inputs)` |
| "New item" triggers | `connector.triggers` + `Poller` (HTTP), `parse_webhook` (webhooks) |
| Async runtime | `AsyncMcpClient` for MCP (the HTTP client is sync; run it in a worker thread) |
| Expose steps to AI agents | `build_mcp_server(connector, creds)` |
| Health checks | `run_contract_tests` (offline), `run_smoke_tests` (live, read-only) |
| Keep connectors current | `check_for_drift(connector)`, then `apply_proposal(...)` after approval |

Errors share one base class, `ConnectorError`, with specific subclasses
(`InputValidationError`, `ActionHTTPError`, `RateLimitError`, `McpToolError`, …) so a host can
map them to step failures, retries, or user-facing messages.

## What's supported

| Area | Supported |
|---|---|
| Sources | OpenAPI 3.0 / 3.1, Swagger 2.0 (converted automatically), API docs pages (with an LLM), MCP servers (stdio or Streamable HTTP) |
| Specs | JSON or YAML (1.2), URL or file; local `$ref`s (recursive, percent-encoded, into lists); tolerant of common spec mistakes (dates in YAML, boolean `exclusiveMinimum`, JS-style regex, relative server URLs, duplicate params, query strings in paths) |
| Auth | API key (header / query / cookie), bearer, basic, OAuth2 (authorization code + PKCE, client credentials, refresh); per-operation `security`, so each action uses the credentials it accepts |
| Inputs and bodies | path, query and header params; JSON bodies; form-encoded bodies with nested objects in bracket notation (`metadata[plan]=pro`) |
| Pagination | cursor (incl. nested cursor fields), cursor-as-URL, Stripe-style `starting_after` + `has_more`, offset, page, RFC 8288 `Link` headers (GitHub style) |
| Triggers | polling ("new X") from list actions, with dedup and resume; webhooks from the spec, with HMAC signature checks (GitHub and Stripe presets) |
| Reliability | retries for rate limits, transient 5xx and network failures; optional response validation |
| Testing | offline contract tests for every action; live, read-only smoke tests |
| Keeping current | `check` / `diff` / `apply`: changes classified as breaking, review, additive or info; local customizations kept; fixes from live API evidence |
| Credentials | sent only to the API's own origin (next-page links and redirects can't carry them elsewhere); never in connector files, `repr()`, error messages or request logs |
| Safety | destructive actions flagged and opt-in (`--allow-destructive`); smoke tests read-only; updates need approval unless purely additive |

On the full Stripe (612 actions) and GitHub (1,242 actions) specs, every action passes its
contract test, and pagination is detected for 133 and 274 actions respectively.

## Documentation

- [MCP](https://github.com/karawash-projects/connectorgen/blob/main/docs/mcp.md): use MCP servers as connectors, and serve connectors as MCP servers
- [Running connectors](https://github.com/karawash-projects/connectorgen/blob/main/docs/running.md): retries, rate limits, response validation
- [Testing connectors](https://github.com/karawash-projects/connectorgen/blob/main/docs/testing-connectors.md): offline contract tests and live smoke tests
- [Triggers](https://github.com/karawash-projects/connectorgen/blob/main/docs/triggers.md): polling and webhooks
- [OAuth2](https://github.com/karawash-projects/connectorgen/blob/main/docs/oauth.md): sign-in, token storage and refresh
- [AI enrichment and docs ingestion](https://github.com/karawash-projects/connectorgen/blob/main/docs/ai.md): any LLM, guardrails, building connectors from docs
- [Repair loop](https://github.com/karawash-projects/connectorgen/blob/main/docs/repair.md): keeping connectors current as APIs change

## Contributing

Contributions are welcome, especially reports of specs that don't work. See
[CONTRIBUTING.md](https://github.com/karawash-projects/connectorgen/blob/main/CONTRIBUTING.md) for setup, tests and design principles, and
[SECURITY.md](https://github.com/karawash-projects/connectorgen/blob/main/SECURITY.md) for reporting vulnerabilities privately. Notable changes are listed in
[CHANGELOG.md](https://github.com/karawash-projects/connectorgen/blob/main/CHANGELOG.md).

## License

[Apache License 2.0](https://github.com/karawash-projects/connectorgen/blob/main/LICENSE). Copyright 2026 Ahmad Karawash.
