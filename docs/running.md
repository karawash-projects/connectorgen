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

## Response validation (drift detection)

`ConnectorClient(..., validate_responses="warn" | "raise")` checks every response against the
action's output schema and emits `ResponseSchemaWarning` or raises `ResponseValidationError`
with the failing path. It's off by default because real specs are often looser than their
APIs, but in production it's the early signal that an API has changed under a connector.

[← Back to the README](../README.md)
