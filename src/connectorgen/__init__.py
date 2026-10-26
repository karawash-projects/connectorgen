"""connectorgen: connectors for HTTP APIs and MCP servers, as data, with one runtime."""

from .errors import (
    ActionHTTPError,
    ConnectionFailedError,
    ConnectorError,
    InputValidationError,
    McpToolError,
    RateLimitError,
    ResponseSchemaWarning,
    ResponseValidationError,
    UnsupportedSpecError,
)
from .ingest.openapi import connector_from_openapi, load_spec
from .ir import Action, AuthScheme, Connector, Pagination, Param, Trigger
from .runtime.auth import Credentials
from .runtime.client import ConnectorClient
from .runtime.retry import RetryPolicy

__all__ = [
    "Action",
    "ActionHTTPError",
    "AuthScheme",
    "ConnectionFailedError",
    "Connector",
    "ConnectorClient",
    "ConnectorError",
    "Credentials",
    "InputValidationError",
    "McpToolError",
    "Pagination",
    "Param",
    "RateLimitError",
    "ResponseSchemaWarning",
    "ResponseValidationError",
    "RetryPolicy",
    "Trigger",
    "UnsupportedSpecError",
    "connector_from_openapi",
    "load_spec",
]

__version__ = "0.9.0"
