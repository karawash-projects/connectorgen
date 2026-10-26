# Contributing to connectorgen

Thanks for helping. Bug reports, spec quirks, new providers and docs fixes are all welcome.

## Setup

```bash
git clone https://github.com/karawash-projects/connectorgen && cd connectorgen
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests scripts
```

Python 3.10–3.13 are supported, and CI runs them all on Linux, plus 3.13 on macOS.

## Tests

```bash
pytest                         # everything: unit, integration and the end-to-end system test
pytest -m "not e2e"            # skip the system test
pytest -m e2e                  # only the system test
python scripts/corpus_check.py path/to/specs --jobs 8   # robustness over many real specs
```

| Layer | What it covers |
|---|---|
| Unit and integration (`tests/test_*.py`) | Every module with mocked HTTP; real MCP servers as subprocesses; real Streamable HTTP with auth; real OAuth loopback sign-in. `test_robustness.py` pins real-world spec quirks; `test_security.py` pins security fixes (credential handling, hostile specs, secret detection) |
| System test (`tests/e2e`) | The CLI against a real local API ("Taskly") over real sockets, no mocks: generate from a live spec URL, contract and smoke tests, calls, cursor and `Link` pagination, rate-limit retry, OAuth client credentials with token expiry, polling triggers, signed webhooks, serving over MCP and calling through it, an upstream v2 (`check` → `apply`), and live drift found and fixed |
| Real-world corpus (`scripts/corpus_check.py`) | Generate plus contract-test any folder of specs, each in its own subprocess with a time limit; failures grouped by cause |

## Before you open a pull request

- Add a test for the behavior you changed. For a bug, add a test that fails without your fix.
- Run `pytest` and `ruff check src tests scripts`.
- If you changed spec handling, run the corpus check against real specs (below) and mention
  the before/after numbers in the PR.
- Update `CHANGELOG.md` under "Unreleased".

## Found a spec that doesn't work?

That's one of the most useful reports you can send. Run:

```bash
connectorgen generate path/to/spec.yaml -o out.json
connectorgen test out.json
```

Open an issue with the spec (or a link), the command and the output. If you can, reduce the
spec to the smallest piece that still fails. `tests/test_robustness.py` shows the style we use
to pin such quirks down.

### Corpus check

`scripts/corpus_check.py` generates and contract-tests every spec in a folder. Each spec runs
in its own subprocess with a time limit, and failures are grouped by cause:

```bash
python scripts/corpus_check.py path/to/specs --jobs 8 --json results.json
```

## Design principles

These hold across the codebase; please keep them:

1. **Connectors are data, not code.** Everything an action needs lives in the connector JSON
   (`ir.py`). The runtime is generic. Don't add per-API code paths.
2. **The model proposes, tests decide.** Anything an LLM produces is validated and contract-tested
   before it's kept, and records its provenance.
3. **The spec wins.** Values from a spec are never overwritten by guesses; enrichment fills gaps.
4. **Safe by default.** Smoke tests are read-only, destructive actions need explicit opt-in, and
   repair proposals need approval.
5. **No secrets in files, no response data in reports.** Connector files hold names of
   credentials, never values; smoke reports hold response shapes, never values.
6. **Explain failures.** Errors say what to do next: which credential is missing, which flag to
   pass.

## Where things live

All paths are under `src/connectorgen/`.

| Area | Module |
|---|---|
| Connector format | `ir.py` |
| OpenAPI / Swagger / docs ingestion | `ingest/openapi.py`, `ingest/swagger2.py`, `ingest/docs.py` |
| HTTP runtime, auth, retries, OAuth | `runtime/client.py`, `runtime/auth.py`, `runtime/retry.py`, `runtime/oauth.py` |
| URL safety and secret detection | `runtime/urls.py` |
| Limits for untrusted schemas (bounded validation) | `limits.py` |
| MCP client, MCP server → connector, connector → MCP server | `mcp/client.py`, `mcp/ingest.py`, `mcp/server.py` |
| LLM providers | `llm/` (add a provider by implementing the `LLM` protocol in `llm/base.py`) |
| Enrichment passes | `enrich/` |
| Contract / smoke tests, example data | `validate/contract.py`, `validate/smoke.py`, `validate/synth.py` |
| Triggers | `triggers/derive.py`, `triggers/polling.py`, `triggers/webhooks.py` |
| Repair loop and live fixes | `repair/check.py`, `repair/diff.py`, `repair/merge.py`, `repair/live.py`, `repair/observe.py` |
| CLI | `cli.py` |

## Implementation notes

- **Shared schemas stay as references.** Generated connectors keep `{"$ref": "#/$defs/Pet"}`
  and collect only the schemas actually used into `connector.schemas`. Inlining them explodes
  on large specs (Stripe's schemas reference each other heavily).
- **Unsettled things go into `action.notes`.** Anything the deterministic pass can't decide
  (an ambiguous pagination style, say) is recorded there and shown by `inspect`, so an
  enrichment pass knows exactly what to fill.
- **Third-party input is bounded.** Specs, docs and MCP schemas are untrusted: size, nesting
  and `$ref` expansion are capped at load, and validation against them uses
  `limits.BoundedValidator`. Use it for any new code that validates against a schema someone
  else wrote.

## Adding an LLM provider

Most providers already work through `OpenAICompatibleLLM` (add a preset to `PRESETS` in
`llm/openai_compat.py`). For a provider with its own API, implement the `LLM` protocol:
`model`, `usage`, and `complete_json(system=, prompt=, schema=, max_tokens=)`, returning a
dict that matches `schema`. Then add it to `llm_from_spec`. Test it against a local mock
server like `tests/test_openai_compat.py` does.

## Releasing

Releases are published to PyPI by `.github/workflows/release.yml` when a `v*` tag is pushed,
using PyPI trusted publishing (no API token is stored in GitHub). One-time setup: on
<https://pypi.org/manage/account/publishing/>, add a publisher for this repository with
workflow `release.yml` and environment `pypi`. Then, for each release: update the version in
`pyproject.toml` and `src/connectorgen/__init__.py`, move the "Unreleased" changelog entries
under the new version, and push a tag such as `v0.9.1`.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be kind.
