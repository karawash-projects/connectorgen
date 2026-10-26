from __future__ import annotations

from typing import Any


class ConnectorError(Exception):
    """Base class for all connectorgen errors."""


class UnsupportedSpecError(ConnectorError):
    """The source spec uses something this version can't handle."""


class InputValidationError(ConnectorError):
    """Inputs passed to an action don't match its input schema."""

    def __init__(self, action: str, message: str, path: list[Any] | None = None):
        self.action = action
        self.path = path or []
        where = f" at '{'.'.join(map(str, self.path))}'" if self.path else ""
        super().__init__(f"Invalid input for '{action}'{where}: {message}")


class ActionHTTPError(ConnectorError):
    """The upstream API returned a non-2xx response."""

    def __init__(self, action: str, status_code: int, body: Any, url: str):
        self.action = action
        self.status_code = status_code
        self.body = body
        self.url = url
        preview = str(body)
        if len(preview) > 300:
            preview = preview[:300] + "..."
        super().__init__(f"'{action}' failed with HTTP {status_code} ({url}): {preview}")


class RateLimitError(ActionHTTPError):
    """Rate limited, and retries were exhausted or the required wait exceeded max_wait."""

    def __init__(self, action: str, status_code: int, body: Any, url: str,
                 retry_after: float | None):
        self.retry_after = retry_after
        super().__init__(action, status_code, body, url)
        if retry_after is not None:
            self.args = (f"{self.args[0]} [retry after {retry_after:.0f}s]",)


class ConnectionFailedError(ConnectorError):
    """The request couldn't be completed at the network level (after retries)."""

    def __init__(self, action: str, url: str, cause: Exception):
        self.action = action
        self.url = url
        from .runtime.urls import redact_text

        super().__init__(f"'{action}' could not reach {url}: {type(cause).__name__}: "
                         f"{redact_text(str(cause))}")


class ResponseValidationError(ConnectorError):
    """A response didn't match the action's output schema (possible API drift)."""

    def __init__(self, action: str, message: str, path: list[Any] | None = None):
        self.action = action
        self.path = path or []
        where = f" at '{'.'.join(map(str, self.path))}'" if self.path else ""
        super().__init__(f"Response from '{action}' doesn't match its schema{where}: {message}")


class ResponseSchemaWarning(UserWarning):
    """Emitted instead of ResponseValidationError when validate_responses='warn'."""


class McpToolError(ConnectorError):
    """An MCP tool ran but reported failure (isError), or the MCP call itself failed."""

    def __init__(self, action: str, message: str):
        self.action = action
        super().__init__(f"MCP tool '{action}' failed: {message}")
