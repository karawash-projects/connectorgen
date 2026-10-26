"""Retry policy: rate limits, transient server errors, and network failures.

Rules, in order of how safe they are to retry:
- Rate-limited responses (429, or 403 with X-RateLimit-Remaining: 0 as GitHub does)
  were rejected before any work happened, so every method is retried.
- Connection failures (never reached the server) are retried for every method.
- 5xx responses and read timeouts may have been partly processed, so only
  idempotent methods are retried, plus POSTs carrying an Idempotency-Key.
"""

from __future__ import annotations

import datetime
import email.utils
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

log = logging.getLogger("connectorgen")

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_MAYBE_SENT = (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError,
               httpx.RemoteProtocolError)


@dataclass
class RetryPolicy:
    max_attempts: int = 4  # total tries, including the first
    backoff_base: float = 0.5  # seconds; doubles each attempt
    backoff_max: float = 30.0
    max_wait: float = 60.0  # if the server asks us to wait longer, fail fast instead
    retry_statuses: frozenset[int] = frozenset({500, 502, 503, 504})
    retry_non_idempotent: bool = False  # retry POST/PATCH on 5xx (risk of double writes)
    jitter: bool = True
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time

    @classmethod
    def disabled(cls) -> RetryPolicy:
        return cls(max_attempts=1)

    # -- classification ------------------------------------------------------------

    @staticmethod
    def is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        return (response.status_code == 403
                and response.headers.get("x-ratelimit-remaining") == "0")

    def method_is_retryable(self, request: httpx.Request) -> bool:
        return (request.method in IDEMPOTENT_METHODS
                or self.retry_non_idempotent
                or "idempotency-key" in request.headers)

    def should_retry_response(self, request: httpx.Request, response: httpx.Response) -> bool:
        if self.is_rate_limited(response):
            return True
        return response.status_code in self.retry_statuses and self.method_is_retryable(request)

    def should_retry_error(self, request: httpx.Request, exc: Exception) -> bool:
        if isinstance(exc, _NEVER_SENT):
            return True
        if isinstance(exc, _MAYBE_SENT):
            return self.method_is_retryable(request)
        return False

    # -- timing --------------------------------------------------------------------

    def server_wait(self, response: httpx.Response) -> float | None:
        """Seconds the server asked us to wait, from Retry-After or rate-limit reset headers."""
        headers = response.headers
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    when = email.utils.parsedate_to_datetime(retry_after)
                    if when.tzinfo is None:  # "-0000" parses as naive; HTTP dates are UTC
                        when = when.replace(tzinfo=datetime.timezone.utc)
                    return max(0.0, when.timestamp() - self.clock())
                except (TypeError, ValueError):
                    pass
        if self.is_rate_limited(response):
            reset = headers.get("x-ratelimit-reset") or headers.get("ratelimit-reset")
            if reset:
                try:
                    value = float(reset)
                except ValueError:
                    return None
                # Large values are epoch timestamps (GitHub); small ones are deltas (IETF draft).
                return max(0.0, value - self.clock()) if value > 1e9 else max(0.0, value)
        return None

    def backoff(self, attempt: int) -> float:
        delay = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))
        return random.uniform(0, delay) if self.jitter else delay
