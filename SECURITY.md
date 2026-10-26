# Security Policy

## Reporting a vulnerability

Please **don't** open a public issue for security problems. Use GitHub's private reporting
instead: on the repository page, go to **Security → Report a vulnerability**. We aim to
acknowledge reports within 3 working days and to ship a fix or mitigation for confirmed issues
as quickly as their severity requires.

## Supported versions

Security fixes go into the latest released minor version.

## What's in scope

connectorgen handles credentials and talks to third-party APIs, LLMs and MCP servers, so
these areas matter most:

- **Credential handling:** tokens and keys leaking into connector files, logs, reports or error
  messages; OAuth token files written with loose permissions; credentials sent to the wrong
  host or to endpoints marked public (`security: []`).
- **Untrusted input:** prompt injection from API docs, specs or API error messages that changes
  what gets generated or executed; a docs-provided base URL redirecting credentials (we only
  accept one on the docs' own site); next-page links or redirects carrying credentials to
  another origin; malicious specs causing excessive resource use.
- **Webhooks:** signature verification bypasses, replay-window issues, duplicate-delivery handling.
- **Safety guarantees:** smoke tests calling non-read-only actions; destructive actions running
  without opt-in; repair proposals applied without approval or over newer edits.
- **MCP:** servers exposed beyond localhost by default; secrets passed to stdio servers beyond the
  declared `env_passthrough` list.

Out of scope: vulnerabilities in the third-party APIs or MCP servers you connect to, and
issues that require an attacker to already control your machine or your credentials.
