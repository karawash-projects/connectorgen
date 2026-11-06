# Running connectors

How the runtime behaves when calls fail or responses don't match the spec.

## Retries and rate limits

Every client retries by default with `RetryPolicy()`. What gets retried depends on whether
retrying could cause a duplicate write:

| Situation | Retried for | Wait |
|---|---|---|
| 429, or 403 with `X-RateLimit-Remaining: 0` (GitHub) | every method (request was rejected) | `Retry-After` (seconds or HTTP date), else `X-RateLimit-Reset` / `RateLimit-Reset`, else backoff |
| Connection failed (never reached the server) | every method | exponential backoff with jitter |
| 500 / 502 / 503 / 504, read timeouts | GET, PUT, DELETE, HEAD; POST only with an `Idempotency-Key` header | exponential backoff with jitter |

If the server asks for a wait longer than `max_wait` (default 60 s), the client fails fast with
`RateLimitError` (which carries `.retry_after`) instead of hanging. Network failures surface as
`ConnectionFailedError`.

```python
from connectorgen import RetryPolicy

ConnectorClient(c, creds, retry=RetryPolicy(max_attempts=6, max_wait=120))
ConnectorClient(c, creds, retry=RetryPolicy.disabled())
```

## Limits

| Limit | Default | Change with |
|---|---|---|
| One response's size | 50 MB | `ConnectorClient(..., max_response_bytes=...)` |
| One request's total time, redirects included | 300 s (`timeout` applies to each network read) | `max_request_seconds=...` |
| Pages read by `call --paginate` | 100 | `--max-pages` |

`paginate()` also stops when an API returns the same page twice (an API that ignores the page
or offset parameter would otherwise be read forever).

## Credentials

- Credentials go only to the API's own origin; next-page links and redirects can't carry them
  elsewhere.
- They aren't sent over plain `http://` except to this machine: a connector whose base URL is
  `http://api.example.com` fails with a clear error. Pass `allow_insecure=True` (or
  `--allow-insecure` on `call` and `serve`) if that's really what you want. OAuth token
  endpoints and LLM API keys follow the same rule.
- When an operation needs two schemes at once (an API key header plus an app id in the query,
  say), both are sent. Give each scheme its own key with `CONNECTORGEN_API_KEY_<SCHEME>`
  (`CONNECTORGEN_API_KEY_APP_ID=...`) or `Credentials(api_keys={"app_id": ...})`;
  `CONNECTORGEN_API_KEY` is used for any scheme without its own.
- Several processes can share one OAuth token file: refreshes are locked and re-read the file
  first, so a refresh token rotated by one process isn't reused by another.

## Response validation (drift detection)

`ConnectorClient(..., validate_responses="warn" | "raise")` checks every response against the
action's output schema and emits `ResponseSchemaWarning` or raises `ResponseValidationError`
with the failing path. It's off by default because real specs are often looser than their
APIs, but in production it's the early signal that an API has changed under a connector.

[← Back to the README](../README.md)
