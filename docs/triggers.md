# Triggers

`generate` adds triggers automatically (`inspect` lists them):

- **Polling triggers** ("new X") for every read-only list action whose items have a stable id
  (`id`, `uuid`, `key`, `number`, `sha`…). If the action can sort by creation time descending
  (e.g. `sort=created&direction=desc`), the trigger asks for that, so polls stop at the first
  item they've already seen.
- **Webhook triggers** from the spec's `webhooks` (OpenAPI 3.1), `x-webhooks` (3.0 era, e.g.
  GitHub) and operation `callbacks`, with their payload schemas.

| Spec | Polling triggers | Webhook triggers |
|---|---|---|
| Stripe | 131 | 0 (not in the spec) |
| GitHub | 238 (33 newest-first) | 275 |

## Polling

```bash
connectorgen poll github.json new_issues_for_repo -i owner=octocat -i repo=hello-world
connectorgen poll github.json new_issues_for_repo -i owner=octocat -i repo=hello-world --watch --interval 300
```

```python
from connectorgen.triggers import Poller, FileStateStore

poller = Poller(client, "new_issues_for_repo", FileStateStore(),
                inputs={"owner": "octocat", "repo": "hello-world"})
result = poller.poll(commit=False)    # at-least-once: commit after processing
for issue in result.items:            # oldest first
    handle(issue)
poller.commit(result)
```

- **First run is a baseline**: existing items are recorded, not emitted, so switching a trigger
  on doesn't replay history (`--emit-existing` / `emit_on_first_run=True` to opt in).
- **Delivery**: `poll()` commits immediately (at-most-once). `poll(commit=False)` + `commit()`
  gives at-least-once: if processing crashes, the same items come back next poll.
- **Long lists**: newest-first lists stop at the first item already seen. Oldest-first lists
  resume each poll where the previous one stopped (offset, page, cursor or next-page URL), so
  items appended to a long list are still seen; they read at least two pages per poll. Lists
  with no known order are read in full up to `max_pages`/`max_items`; if they're longer, the
  result has `complete=False` and a warning says newer items may be missed.
- State is kept per trigger *and* per input set, capped at the last 5,000 ids.

## Webhooks

```python
from connectorgen.ir import WebhookSignature
from connectorgen.triggers import parse_webhook

trigger = connector_trigger            # e.g. c.triggers[...] named "on_issues_opened"
trigger.signature = WebhookSignature.github()       # or .stripe(), or your own
trigger.delivery_id_header = "X-GitHub-Delivery"

payload = parse_webhook(trigger, request.headers, request.body,   # raw bytes, unparsed
                        secret=WEBHOOK_SECRET, schemas=c.schemas, seen_ids=seen)
```

Signatures are HMAC-verified with constant-time comparison. Stripe-style timestamps are
checked against a 5-minute replay window. Repeat deliveries raise `DuplicateDeliveryError`, and
payloads are checked against the schema (warn by default). A delivery id is recorded only once
the delivery passes every check, so the sender's retry after a rejection is accepted. Specs
rarely say how webhooks are signed, so `signature` is set by you; presets cover GitHub and Stripe.

[← Back to the README](../README.md)
