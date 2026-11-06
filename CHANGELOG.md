# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Before 1.0, minor versions may change the API.

## [Unreleased]

## [0.9.1] - 2026-11-06

Fixes from a pre-production review. Upgrading is recommended for everyone on 0.9.0.

### Security
- Webhooks: a trigger with no signing secret configured is refused unless you pass
  `allow_unsigned=True`.
- Credentials, OAuth client secrets and LLM API keys are no longer sent over plain `http://`
  except to this machine (`allow_insecure=True` / `--allow-insecure` to override).
- Spec downloads don't follow redirects to plain http or to private and internal addresses.
- `CONNECTORGEN_LLM_API_KEY` is only sent to custom base URLs, never to a provider's own API.
  `anthropic:` models accept a base URL for gateways.
- LLM clients no longer set their auth header on an HTTP client you pass in.
- Request logs from the MCP SDK's HTTP stack are redacted too.
- Live fixes can't add credential or hop-by-hop headers, and `check --verify-live` asks before
  calling the API (`--yes` to skip).
- Reports, messages and live fixes no longer keep response keys that are data (emails, ids,
  dates).
- `serve` warns when bound to an address other machines can reach.

### Fixed
- Object and array parameters are sent in their OpenAPI style (`deepObject`, `form`,
  delimited, `label`, `matrix`).
- Next-page URLs keep the request's method and body (search endpoints that page with POST).
- Operations that need several auth schemes at once send all of them; per-scheme keys with
  `CONNECTORGEN_API_KEY_<SCHEME>`. An operation naming an undefined scheme fails clearly.
- A parameter named `body` no longer collides with the request body (it becomes
  `body_param`).
- Every PUT and PATCH, and POSTs that delete, revoke, refund and the like, are now marked
  destructive in OpenAPI connectors, as in docs and MCP connectors.
- `check` and `apply`: a renamed MCP tool must also match in description and safety; a
  change in an action's safety or auth needs review; a spec moving its base URL is picked up
  unless you overrode it; destructive flags and local edits are kept; a proposal whose
  connector file changed since is refused.
- Connector files are written atomically, and an invalid file gives a clear error.
- Swagger 2: an operation's own body replaces a path-level one; `$ref`s into paths and
  response schemas resolve.
- Server variables without a default are left for you to fill in, instead of becoming empty.
- Query strings in path templates: placeholders become parameters, fixed pairs are sent.
- Schemas that include themselves through `allOf`, malformed spec fields, and patterns that
  merely start and end with `/` no longer break generation.
- Docs connectors: endpoints documented twice keep one set of parameter names, auth headers
  aren't inputs, and paths made only of placeholders must appear in the docs.
- OAuth: processes sharing a token file no longer reuse a rotated refresh token; sign-in flows
  that aren't supported say to supply a token.
- Pagination stops when an API returns the same page again; `call --paginate` reads at most
  100 pages by default.
- Responses are capped in size and total time; docs fetching has per-page and total
  deadlines; specs found while crawling docs are skipped.
- Polling with no sort order no longer re-emits items from a long unchanged list.
- Anthropic replies are validated against the schema; malformed replies from any provider
  raise `LLMError`.
- MCP: `serve` reports a response that doesn't match a tool's output schema instead of breaking
  the client; listing tools stops on a repeated cursor.
- Hostile YAML merge keys, huge regex repeats, and slow `pattern` checks are bounded.

### Changed
- `jsonschema` and `mcp` have upper version bounds.
## [0.9.0] - 2026-10-26

First public release.

### Added
- Generate connectors from OpenAPI 3.0/3.1 and Swagger 2.0 specs, from API docs pages (with an
  LLM), and from MCP servers. A connector is one JSON file: actions with input schemas, auth,
  pagination and triggers.
- One runtime for every connector: `execute`, `paginate`, retries and rate-limit handling, API
  key / bearer / basic / OAuth2 auth, and optional response validation.
- MCP both ways: use any MCP server as a connector, and serve any connector as an MCP server.
- Triggers: polling ("new X") from list actions, and webhooks with signature verification.
- Offline contract tests for every action, and live read-only smoke tests.
- Repair loop: `check`, `diff` and `apply` detect upstream changes, classify them by impact, keep
  local customizations, and propose fixes from live API evidence.
- Optional AI enrichment with any LLM: Anthropic, OpenAI, or any OpenAI-compatible API,
  including local models.
- A CLI covering all of the above.

[Unreleased]: https://github.com/karawash-projects/connectorgen/compare/v0.9.1...HEAD
[0.9.1]: https://github.com/karawash-projects/connectorgen/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/karawash-projects/connectorgen/releases/tag/v0.9.0
