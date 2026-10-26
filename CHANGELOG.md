# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Before 1.0, minor versions may change the API.

## [Unreleased]

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

[Unreleased]: https://github.com/karawash-projects/connectorgen/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/karawash-projects/connectorgen/releases/tag/v0.9.0
