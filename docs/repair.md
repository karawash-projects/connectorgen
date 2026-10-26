# Repair loop: keeping connectors current

APIs and MCP servers change. `check` re-reads a connector's source, regenerates it, and writes
a proposal for a person to approve. It never applies breaking changes on its own.

```bash
connectorgen check github.json --used issues_list_for_repo --used issues_create
#   exit 0  = no change
#   exit 10 = safe update (additive only, contract tests no worse)
#   exit 20 = needs review (breaking/safety changes, live drift, or unverified docs change)
cat repair/summary.txt
connectorgen apply repair/ github.json          # after review; keeps a timestamped backup

connectorgen check stripe.json --auto-apply     # scheduled jobs: auto-apply only safe updates
connectorgen check github.json --smoke-report smoke.json   # use live drift as evidence
connectorgen diff old.json new.json             # compare any two connector files
```

**What it checks.** OpenAPI connectors re-read their spec (`--spec` overrides the recorded
path/URL). MCP connectors reconnect and re-list tools. Docs connectors re-fetch the pages; if they
changed, regenerating needs a model (`--model`). A `smoke --json` report adds live evidence: a
response that no longer matches the spec is flagged even when the spec itself didn't change.

**How changes are classified**, from the point of view of workflows already using the connector:

| Severity | Examples |
|---|---|
| breaking | action removed; new required input; input field removed; type changed; accepted values narrowed; new enum, pattern or format on an input; limit tightened; response field removed or retyped (at any depth); auth scheme removed; trigger removed |
| review | endpoint moved to another path or method; anything that changes where credentials go (OAuth token/authorization/refresh URL, API-key name or location, a new auth scheme); became destructive / no longer read-only; base URL changed; response field can now also be another type or return new enum values; schema alternatives both added and removed; fixes from the live API |
| additive | new action/trigger; new optional input; accepted values widened; limit loosened; new response field |
| info | path placeholder renamed (`{id}` → `{itemId}`); pagination changed; upstream rename covered by an alias |

**What it preserves.** The regenerated connector is the truth about the API. From your current
connector it keeps what was added locally:
- **Published action names.** If upstream renames an operation or MCP tool, the action keeps
  its name and the new name becomes an alias (`connector.action("new_name")` works too).
  Workflows don't break.
- **Enrichment results**: descriptions, pagination and auth filled in by enrichment, where
  the new spec still leaves gaps. The spec wins wherever it now has an answer.
- **Local config**: your base URL override, MCP launch config, trigger names, webhook signing
  settings, and polling inputs.

**Applying safely.** `apply` refuses if the connector file changed after the check (so newer
edits can't be overwritten), writes atomically, and keeps `connector.json.<timestamp>.bak`.

**Impact on users.** `--used` (or `used_actions=` in Python) lists the actions your workflows
depend on. The proposal names exactly which of them a change affects, which is what a host tool
needs to notify workflow owners.

**Tested on real releases:** checking a connector built from Stripe's spec v2441 against v2591
(150 releases later) found 13 breaking changes (e.g. `payment_method_types`
removed from payment-intent creation, `billing_cycle_anchor` changing from string to object,
narrowed enums), 17 to review, 189 additive and 21 info. All 612 actions in the updated connector
pass contract tests.

## When the live API disagrees with an unchanged spec

Specs go stale: an API starts returning a string where its spec says integer, or starts
requiring a parameter the spec doesn't mention. `smoke` detects this, and `check --fix-live`
proposes fixes from that evidence:

```bash
connectorgen smoke github.json --param owner=octocat --json smoke.json
connectorgen check github.json --smoke-report smoke.json --fix-live --verify-live \
    --param owner=octocat --model openai:gpt-6-luna
connectorgen apply repair/ github.json
```

| Evidence | Fix | Model? | Verified by |
|---|---|---|---|
| Response didn't match the output schema | Reconcile the action's output schema with the observed response shape: widen types, allow null, add new fields, stop requiring absent fields, add new enum codes | No | Offline (fixed schema accepts the observed shape), plus live with `--verify-live` |
| Request rejected (HTTP 400/422, or an MCP tool error) | The model reads the error message and the action's definition, then proposes input operations: add/remove/rename a parameter, change required, type, or allowed values | Yes (`--model`) | Contract tests, plus live with `--verify-live` (a fix that still fails is dropped) |

Guardrails:
- **No response data is kept.** Smoke reports record the *shape* of a drifting response
  (field names and types), never values; validation messages are worded so they don't quote
  values either. The only values kept are short code-like enum strings (e.g.
  `pending_review`), because those codes are the fix.
- **Different shapes are refused.** If a response looks like something else entirely (an
  array where the spec says object, or most required fields missing, as with an error
  envelope sent with 200), no automatic fix is made; a person needs to look.
- **Shared schemas stay untouched.** Only the affected action's schema changes, so other
  actions sharing that definition aren't loosened.
- **Always reviewed.** Live fixes get the verdict `live_fix` (exit 20) and are never
  auto-applied. Model proposals that are low-confidence, malformed, or fail contract tests
  are dropped.
- **Fixes survive updates.** Each fix is stored on its action (`observed_output`,
  `live_patches`) and re-applied when the connector is regenerated from a newer spec, wherever
  the new spec still needs it.

Tested against the official MCP `server-everything`: with a deliberately stale output schema
(wrong type, plus a required field the server never sends), smoke flagged the drift, the fix
was verified with a live call, and after `apply` smoke passed. Request fixes were tested against
a real MCP server whose tool gained a required argument.

```python
from connectorgen.repair import check_for_drift, apply_proposal

proposal = check_for_drift(connector, used_actions=["get_customers"], current_path="stripe.json")
if proposal.verdict == "safe_update":
    proposal.save("repair/"); apply_proposal("repair/", "stripe.json")
elif proposal.has_update:
    notify(proposal.to_text(), proposal.affected_used)
```

[← Back to the README](../README.md)
