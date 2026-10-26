"""Call an MCP server's tools through a Connector, with the same interface as HTTP.

    async with AsyncMcpClient(connector, creds) as client:   # async hosts
        await client.execute("add", a=2, b=3)

    with McpClient(connector, creds) as client:              # sync hosts
        client.execute("add", a=2, b=3)

    with open_client(connector, creds) as client:            # either kind of connector
        client.execute(action_name, inputs)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import threading
import warnings
from contextlib import AsyncExitStack
from typing import Any, Literal


from ..errors import (
    ConnectorError,
    InputValidationError,
    McpToolError,
    ResponseSchemaWarning,
    ResponseValidationError,
)
from ..ir import Action, Connector
from ..runtime.auth import Credentials, apply_auth, pick_scheme
from ..limits import BoundedValidator, SchemaTooLarge

CLIENT_NAME = "connectorgen"


def _require_sdk() -> Any:
    try:
        import mcp
    except ImportError as e:  # pragma: no cover
        raise ConnectorError("MCP support needs the SDK: pip install 'connectorgen[mcp]'") from e
    return mcp


def _target(connector: Connector, credentials: Credentials | None,
            env: dict[str, str] | None) -> tuple[Any, Any]:
    """(what to pass to mcp.Client, an http client to close afterwards or None)."""
    _require_sdk()
    cfg = connector.mcp
    if cfg is None:
        raise ConnectorError(f"Connector '{connector.name}' is not an MCP connector.")
    if cfg.transport == "stdio":
        from mcp.client.stdio import StdioServerParameters, get_default_environment

        if not cfg.command:
            raise ConnectorError("MCP stdio config has no command.")
        child_env = {**get_default_environment(), **cfg.env}
        supplied = env or {}
        missing = []
        for name in cfg.env_passthrough:
            value = supplied.get(name, os.environ.get(name))
            if value is None:
                missing.append(name)
            else:
                child_env[name] = value
        if missing:
            raise ConnectorError(f"MCP server needs environment variable(s): {', '.join(missing)}")
        return StdioServerParameters(command=cfg.command, args=cfg.args, env=child_env,
                                     cwd=cfg.cwd), None

    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    if not cfg.url:
        raise ConnectorError("MCP http config has no url.")
    headers = dict(cfg.headers)
    query: dict[str, Any] = {}
    cookies: dict[str, str] = {}
    apply_auth(pick_scheme(connector.auth, credentials), credentials, headers, query, cookies)
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    url = cfg.url
    if query:
        from urllib.parse import urlencode

        url += ("&" if "?" in url else "?") + urlencode(query)
    http = create_mcp_http_client(headers=headers)
    return streamable_http_client(url, http_client=http), http


def tool_result_value(action: str, result: Any) -> Any:
    """Turn a CallToolResult into plain data; raise McpToolError when isError."""
    texts = []
    values: list[Any] = []
    for block in result.content or []:
        kind = getattr(block, "type", "")
        if kind == "text":
            texts.append(block.text)
            try:
                values.append(json.loads(block.text))
            except (ValueError, TypeError):
                values.append(block.text)
        elif kind in ("image", "audio"):
            values.append({"type": kind, "mime_type": block.mime_type, "data": block.data})
        else:
            values.append(block.model_dump(mode="json", exclude_none=True))
    if result.is_error:
        raise McpToolError(action, " ".join(texts) or "tool reported an error")
    if result.structured_content is not None:
        return result.structured_content
    if not values:
        return None
    return values[0] if len(values) == 1 else values


class AsyncMcpClient:
    def __init__(
        self,
        connector: Connector,
        credentials: Credentials | None = None,
        *,
        env: dict[str, str] | None = None,
        server: Any = None,
        validate: bool = True,
        validate_responses: Literal["off", "warn", "raise"] = "off",
        timeout: float = 120.0,
    ):
        """
        env: values for the connector's `env_passthrough` names (stdio servers);
            falls back to the process environment.
        server: connect to this instead of the configured server (an in-process
            mcp Server/MCPServer or a Transport) - mainly for tests.
        """
        self.connector = connector
        self.credentials = credentials
        self.env = env
        self.server = server
        self.validate = validate
        self.validate_responses = validate_responses
        self.timeout = timeout
        self._stack: AsyncExitStack | None = None
        self._client: Any = None
        self._validators: dict[str, BoundedValidator] = {}

    async def __aenter__(self) -> AsyncMcpClient:
        mcp = _require_sdk()
        stack = AsyncExitStack()
        try:
            if self.server is not None:
                target, http = self.server, None
            else:
                target, http = _target(self.connector, self.credentials, self.env)
            if http is not None:
                await stack.enter_async_context(http)
            info = mcp.types.Implementation(name=CLIENT_NAME, version="0.9")
            self._client = await stack.enter_async_context(
                mcp.Client(target, client_info=info, read_timeout_seconds=self.timeout))
        except BaseException as e:
            await stack.aclose()
            # Cancellation (asyncio timeouts, task groups), KeyboardInterrupt and SystemExit
            # must propagate unchanged; only ordinary failures become ConnectorError.
            if isinstance(e, ConnectorError) or not isinstance(e, Exception):
                raise
            where = self.connector.mcp.describe() if self.connector.mcp else "in-process server"
            raise ConnectorError(f"Couldn't connect to MCP server ({where}): "
                                 f"{type(e).__name__}: {e}") from e
        self._stack = stack
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._stack is not None:
            stack, self._stack = self._stack, None
            await stack.aclose()

    @property
    def raw(self) -> Any:
        """The underlying mcp.Client, for anything this wrapper doesn't cover."""
        if self._client is None:
            raise ConnectorError("Not connected; use `async with`.")
        return self._client

    async def list_tools(self) -> list[Any]:
        tools, cursor = [], None
        while True:
            page = await self.raw.list_tools(cursor=cursor)
            tools += page.tools
            cursor = page.next_cursor
            if not cursor:
                return tools

    async def execute(self, action: str | Action, inputs: dict[str, Any] | None = None,
                      **kwargs: Any) -> Any:
        act = action if isinstance(action, Action) else self.connector.action(action)
        if not act.mcp_tool:
            raise ConnectorError(f"'{act.name}' is not an MCP tool action.")
        args = {**(inputs or {}), **kwargs}
        if self.validate:
            self._check(act, args, "input")
        try:
            result = await self.raw.call_tool(act.mcp_tool, args)
        except ConnectorError:
            raise
        except Exception as e:
            raise McpToolError(act.name, f"{type(e).__name__}: {e}") from e
        value = tool_result_value(act.name, result)
        if self.validate_responses != "off" and act.output_schema is not None and value is not None:
            self._check(act, value, "output")
        return value

    def _check(self, act: Action, data: Any, which: str) -> None:
        key = f"{which}:{act.name}"
        validator = self._validators.get(key)
        if validator is None:
            schema = self.connector.input_schema(act) if which == "input" else dict(act.output_schema or {})
            if which == "output" and self.connector.schemas:
                schema["$defs"] = self.connector.schemas
            validator = BoundedValidator(schema)
            self._validators[key] = validator
        try:
            error = validator.best_error(data)
        except SchemaTooLarge as e:
            if which == "input":
                raise ConnectorError(f"'{act.name}': its argument schema is too complex to "
                                     f"validate ({e}).") from None
            return
        if error is None:
            return
        if which == "input":
            raise InputValidationError(act.name, error.message, list(error.path))
        exc = ResponseValidationError(act.name, error.message, list(error.path))
        if self.validate_responses == "raise":
            raise exc
        warnings.warn(str(exc), ResponseSchemaWarning, stacklevel=3)


class McpClient:
    """Synchronous wrapper. The MCP session lives in one task on a private event-loop
    thread (the SDK requires its context to be entered and exited by the same task);
    calls are handed to that task and run one at a time."""

    def __init__(self, connector: Connector, credentials: Credentials | None = None, **kwargs: Any):
        self.connector = connector
        self._inner = AsyncMcpClient(connector, credentials, **kwargs)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="connectorgen-mcp",
                                        daemon=True)
        self._thread.start()
        self._queue: asyncio.Queue[Any] | None = None
        ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._task = asyncio.run_coroutine_threadsafe(self._serve(ready), self._loop)
        try:
            ready.result()
        except BaseException:
            self._stop_loop()
            raise

    async def _serve(self, ready: concurrent.futures.Future[None]) -> None:
        self._queue = asyncio.Queue()
        try:
            async with self._inner:
                ready.set_result(None)
                while True:
                    item = await self._queue.get()
                    if item is None:
                        return
                    fn, args, kwargs, fut = item
                    try:
                        fut.set_result(await fn(*args, **kwargs))
                    except BaseException as e:  # hand every failure back to the caller
                        fut.set_exception(e)
        except BaseException as e:
            if not ready.done():
                ready.set_exception(e)
            else:
                raise

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        if self._queue is None or self._task.done():
            raise ConnectorError("MCP client is closed.")
        fut: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (fn, args, kwargs, fut))
        return fut.result()

    def execute(self, action: str | Action, inputs: dict[str, Any] | None = None,
                **kwargs: Any) -> Any:
        return self._call(self._inner.execute, action, inputs, **kwargs)

    def list_tools(self) -> list[Any]:
        return self._call(self._inner.list_tools)

    def close(self) -> None:
        if self._queue is not None and not self._task.done():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            try:
                self._task.result(timeout=30)
            except Exception:
                pass
        self._stop_loop()

    def _stop_loop(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
        if not self._loop.is_running() and not self._loop.is_closed():
            self._loop.close()

    def __enter__(self) -> McpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_client(connector: Connector, credentials: Credentials | None = None, **kwargs: Any) -> Any:
    """A client for any connector: ConnectorClient for HTTP APIs, McpClient for MCP
    servers. Both offer execute(action, inputs) and work as context managers."""
    if connector.mcp is not None:
        return McpClient(connector, credentials, **kwargs)
    from ..runtime.client import ConnectorClient

    return ConnectorClient(connector, credentials, **kwargs)
