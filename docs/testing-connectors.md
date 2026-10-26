# Testing connectors

Two kinds of checks: offline contract tests that need no credentials, and live smoke tests that
call the real API read-only.

## Contract tests (offline)

`connectorgen test` exercises every action offline. For each one it synthesizes the smallest
valid input, runs it through the real runtime against a mock server that answers with a
response built from the output schema, and checks:

- schemas are valid JSON Schema
- the request: method, every path placeholder filled, required query/header params sent,
  auth applied, body encoded with the right content type
- the response: returned intact; pagination `items_path`, cursor and `has_more` paths
  actually exist in a schema-conforming response

```bash
connectorgen test stripe.json                       # summary + failures
connectorgen test stripe.json -a get_customers -v   # one action, show passes
connectorgen test stripe.json --emit-pytest tests/test_stripe_contract.py   # run in CI
```

Actions whose inputs can't be synthesized (e.g. regex features like backreferences) are
reported as skipped, not failed. Example values come from `Synthesizer`, which handles
formats, enums, bounds, `oneOf`/`anyOf`/`allOf`, recursive schemas, and generates strings
for the regex subset APIs actually use (`^sha256:[a-f0-9]{64}$` → `sha256:aaaa…`).

## Live smoke tests

```bash
export CONNECTORGEN_TOKEN=ghp_...
connectorgen smoke github.json --param owner=octocat --param repo=hello-world --json smoke.json
```

Calls the real API with real credentials and reports, per action: `ok`, `drift` (the response
doesn't match the spec's schema), `http_error`, `error`, or `skipped` (a required parameter
couldn't be filled).

- **Read-only, always.** Only `GET` actions not flagged destructive are called. Naming a write
  action explicitly is refused. This isn't configurable.
- One small page per action (page size 2), a short pause between calls, and a full stop on 401
  or when rate limited.
- **ID chaining:** required params are filled from `--param`, then schema defaults/enums, then
  values from earlier list responses. `get_pet` gets its `petId` from what `list_pets` returned.
- `--json` writes a machine-readable report, which the [repair loop](repair.md) can use as evidence.

[← Back to the README](../README.md)
