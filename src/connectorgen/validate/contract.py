"""Contract tests: exercise every action against a mock server built from its schemas.

No network and no credentials. For each action this:
1. checks its schemas are valid JSON Schema,
2. synthesizes the smallest valid input,
3. runs it through the real ConnectorClient against a mock transport that answers
   with a response synthesized from the output schema,
4. checks the request (method, path params filled, required params sent, auth,
   body) and the response handling (parsed intact, pagination fields resolvable).

It catches problems in the connector and runtime — wrong pagination paths,
unfillable paths, broken schemas — before anyone points it at a live API.
"""

from __future__ import annotations

import json
import os
import re
import time
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ..errors import ConnectorError
from ..ir import Action, Connector
from ..runtime.auth import Credentials
from ..runtime.client import ConnectorClient, _joined, get_path
from ..runtime.retry import RetryPolicy
from ..limits import BoundedValidator, SchemaTooLarge, check_schema_work
from .synth import Synthesizer

_PLACEHOLDER = re.compile(r"\{[^}/]+\}")
_MISSING = object()
_TEST_CREDS = Credentials(api_key="test-api-key", token="test-token",
                          username="test-user", password="test-pass")


@dataclass
class CheckResult:
    action: str
    status: Literal["pass", "fail", "skip"] = "pass"
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status != "fail"

    def summary(self) -> str:
        lines = [f"{self.status.upper():<4} {self.action}"]
        lines += [f"     - {f}" for f in self.failures]
        lines += [f"     ~ {w}" for w in self.warnings]
        return "\n".join(lines)


@dataclass
class ContractReport:
    connector: str
    results: list[CheckResult]
    schema_errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def _count(self, status: str) -> int:
        return sum(r.status == status for r in self.results)

    @property
    def passed(self) -> int:
        return self._count("pass")

    @property
    def failed(self) -> int:
        return self._count("fail")

    @property
    def skipped(self) -> int:
        return self._count("skip")

    @property
    def ok(self) -> bool:
        return self.failed == 0 and not self.schema_errors

    def to_text(self, verbose: bool = False) -> str:
        lines = []
        for err in self.schema_errors:
            lines.append(f"SCHEMA {err}")
        for r in self.results:
            if verbose or r.status != "pass" or r.warnings:
                lines.append(r.summary())
        lines.append(f"\n{self.connector}: {self.passed} passed, {self.failed} failed, "
                     f"{self.skipped} skipped ({len(self.results)} actions, {self.seconds:.1f}s)")
        return "\n".join(lines)


# --------------------------------------------------------------------------- core


@lru_cache(maxsize=4096)
def _schema_problem(canonical: str) -> str | None:
    """Metaschema check, cached: large specs repeat the same inline schemas many times."""
    try:
        Draft202012Validator.check_schema(json.loads(canonical))
    except SchemaError as e:
        return e.message
    return None


def check_action(connector: Connector, action: str | Action) -> CheckResult:
    act = action if isinstance(action, Action) else connector.action(action)
    result = CheckResult(act.name)
    fail, warn = result.failures.append, result.warnings.append

    # 1. schemas are valid JSON Schema (refs into $defs are checked once, in the report)
    for label, schema in (("input", act.input_schema()), ("output", act.output_schema)):
        if schema is None:
            continue
        problem = _schema_problem(json.dumps(schema, sort_keys=True, default=str))
        if problem:
            fail(f"{label} schema is not valid JSON Schema: {problem}")
    if result.failures:
        result.status = "fail"
        return result

    # 2. smallest valid input (schemas that would take unreasonable work are skipped, so a
    #    hostile spec can't hang `test` or `check`)
    input_schema = connector.input_schema(act)
    try:
        for schema in (input_schema, act.output_schema):
            if schema is not None:
                check_schema_work(schema, connector.schemas)
        inputs = Synthesizer(connector.schemas).example(input_schema)
    except SchemaTooLarge as e:
        result.status = "skip"
        warn(f"schema too large to test offline ({e})")
        return result
    try:
        error = BoundedValidator(input_schema).best_error(inputs)
    except SchemaTooLarge as e:
        result.status = "skip"
        warn(f"schema too large to test offline ({e})")
        return result
    if error is not None:
        result.status = "skip"
        where = ".".join(map(str, error.path)) or "(root)"
        warn(f"couldn't synthesize a valid input (at {where}: {error.message[:120]})")
        return result

    if act.mcp_tool:
        # MCP tools have no HTTP request to inspect; schema validity and a valid
        # synthesized input are what can be checked offline. `smoke` covers the rest.
        result.status = "pass"
        return result

    # 3. fake response from the output schema
    mock_body = None
    if act.output_schema is not None:
        try:
            mock_body = Synthesizer(connector.schemas, full=True).example(act.output_schema)
        except SchemaTooLarge as e:
            result.status = "skip"
            warn(f"response schema too large to test offline ({e})")
            return result
        out_schema = {**act.output_schema, "$defs": connector.schemas} if connector.schemas \
            else act.output_schema
        try:
            error = BoundedValidator(out_schema).best_error(mock_body)
        except SchemaTooLarge as e:
            result.status = "skip"
            warn(f"response schema too large to test offline ({e})")
            return result
        if error is not None:
            where = ".".join(map(str, error.path)) or "(root)"
            warn(f"synthesized response doesn't satisfy the output schema "
                 f"(at {where}: {error.message[:120]}); schema may be contradictory")

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if mock_body is None:
            return httpx.Response(204)
        return httpx.Response(200, json=mock_body)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    base_url = connector.base_url or "https://api.example.test"
    if not base_url.startswith(("http://", "https://")):  # relative server URL in the spec
        base_url = "https://api.example.test/" + base_url.lstrip("/")
    base_url = re.sub(r"\{[^}]+\}", "example", base_url)  # server variables without defaults
    try:
        client = ConnectorClient(connector, _TEST_CREDS, base_url=base_url, http=http,
                                 retry=RetryPolicy.disabled())
        returned = client.execute(act, inputs)
    except ConnectorError as e:
        if "uses auth connectorgen can't perform" in str(e):
            result.status = "skip"
            warn(str(e))
            return result
        fail(f"runtime raised {type(e).__name__}: {e}")
        result.status = "fail"
        return result
    finally:
        http.close()

    # 4a. request checks
    if len(captured) != 1:
        fail(f"expected exactly 1 request, saw {len(captured)}")
    else:
        _check_request(act, client, inputs, captured[0], fail)

    # 4b. response handling
    if mock_body is not None and returned != json.loads(json.dumps(mock_body)):
        fail("runtime returned different data than the server sent")
    if act.pagination.style != "none" and mock_body is not None:
        _check_pagination(act, mock_body, fail, warn)

    result.status = "fail" if result.failures else "pass"
    return result


def _check_request(act: Action, client: ConnectorClient, inputs: dict[str, Any],
                   req: httpx.Request, fail: Any) -> None:
    if req.method != act.method:
        fail(f"sent {req.method}, expected {act.method}")
    path = unquote(req.url.path)
    leftover = _PLACEHOLDER.findall(path)
    if leftover:
        fail(f"unfilled path placeholders {leftover} in {path}")
    for p in act.params:
        if not p.required:
            continue
        if p.location == "query" and p.name not in req.url.params:
            fail(f"required query param '{p.name}' not sent")
        elif p.location == "header" and p.name not in req.headers:
            fail(f"required header '{p.name}' not sent")
        elif p.location == "path" and _joined(inputs.get(p.name), ",") not in unquote(str(req.url)):
            fail(f"path param '{p.name}' value not in URL path")

    scheme = client.scheme_for(act)
    if scheme is not None:
        if scheme.type == "api_key":
            sent = (req.url.params.get(scheme.param_name) if scheme.location == "query"
                    else req.headers.get("cookie", "") if scheme.location == "cookie"
                    else req.headers.get(scheme.param_name or ""))
            if not sent or _TEST_CREDS.api_key not in sent:
                fail(f"api key not sent in {scheme.location} '{scheme.param_name}'")
        elif "authorization" not in req.headers:
            fail(f"{scheme.type} auth header not sent")

    if inputs.get("body") and act.body_schema is not None:
        if not req.content:
            fail("request body is empty")
        elif act.body_encoding == "json":
            try:
                if json.loads(req.content) != json.loads(json.dumps(inputs["body"])):
                    fail("JSON body sent differs from the input")
            except ValueError:
                fail("request body is not valid JSON")
            if "application/json" not in req.headers.get("content-type", ""):
                fail("JSON body sent without a JSON content-type")
        elif "x-www-form-urlencoded" not in req.headers.get("content-type", ""):
            fail("form body sent without a form content-type")


def _check_pagination(act: Action, body: Any, fail: Any, warn: Any) -> None:
    p = act.pagination
    items = get_path(body, p.items_path)
    if not isinstance(items, list):
        fail(f"pagination items_path '{p.items_path or '(body)'}' doesn't resolve to a list "
             f"in a schema-conforming response")
        return
    for label, path in (("response_cursor_path", p.response_cursor_path),
                        ("has_more_path", p.has_more_path)):
        if path and _lookup(body, path) is _MISSING:
            fail(f"pagination {label} '{path}' is absent from a schema-conforming response")
    if p.cursor_from_item_field and items and isinstance(items[0], dict) \
            and p.cursor_from_item_field not in items[0]:
        warn(f"items may lack the cursor field '{p.cursor_from_item_field}' "
             "(not required by the item schema)")


def _lookup(data: Any, path: str) -> Any:
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


# --------------------------------------------------------------------------- batch + emit


def run_contract_tests(connector: Connector, actions: list[str] | None = None) -> ContractReport:
    start = time.perf_counter()
    schema_errors: list[str] = []
    if connector.schemas:
        try:
            Draft202012Validator.check_schema({"$defs": connector.schemas})
        except SchemaError as e:
            schema_errors.append(f"shared schemas invalid at "
                                 f"{'/'.join(map(str, e.path))}: {e.message}")
    targets = [connector.action(a) for a in actions] if actions else connector.actions
    results = []
    for act in targets:
        try:
            results.append(check_action(connector, act))
        except Exception as e:  # a bug in the checker shouldn't hide other results
            results.append(CheckResult(act.name, "fail", [f"checker crashed: {type(e).__name__}: {e}"]))
    return ContractReport(connector.name, results, schema_errors, time.perf_counter() - start)


_PYTEST_TEMPLATE = '''\
# Generated by connectorgen. Regenerate with:
#   connectorgen test {connector_arg} --emit-pytest {out_arg}
from pathlib import Path

import pytest

from connectorgen import Connector
from connectorgen.validate import check_action

CONNECTOR = Connector.load(Path(__file__).parent / {rel_path!r})

ACTIONS = {actions!r}


@pytest.mark.parametrize("action", ACTIONS)
def test_contract(action):
    result = check_action(CONNECTOR, action)
    if result.status == "skip":
        pytest.skip("; ".join(result.warnings))
    assert result.ok, result.summary()
'''


def write_pytest(connector_path: str | Path, out_path: str | Path) -> Path:
    """Write a pytest file with one parametrized contract test per action."""
    connector_path, out_path = Path(connector_path), Path(out_path)
    connector = Connector.load(connector_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rel = os.path.relpath(connector_path.resolve(), out_path.parent.resolve())
    out_path.write_text(_PYTEST_TEMPLATE.format(
        connector_arg=connector_path, out_arg=out_path, rel_path=rel,
        actions=[a.name for a in connector.actions],
    ))
    return out_path
