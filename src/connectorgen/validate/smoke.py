"""Live smoke tests: call the real API with real credentials, read-only.

Safety rules (not configurable):
- Only GET actions that aren't flagged destructive are ever called. For MCP
  connectors that means tools annotated readOnlyHint.
- One small page per action (page size 2 when the action has a page-size param).
- Stops everything on 401 (bad credentials) or when rate limited.

Required parameters are filled from, in order: per-action inputs you pass,
global params you pass (e.g. owner=octocat), enum/default values in the
schema, then values seen in earlier list responses. For instance `get_pet`
gets its `petId` from the first item `list_pets` returned, because
/pets is a prefix of /pets/{petId}.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import httpx

from ..errors import ActionHTTPError, ConnectorError, RateLimitError
from ..ir import Action, Connector
from ..runtime.auth import Credentials
from ..runtime.client import ConnectorClient, get_path
from ..runtime.retry import RetryPolicy
from ..limits import BoundedValidator, SchemaTooLarge

Status = Literal["ok", "drift", "http_error", "error", "skipped"]


@dataclass
class SmokeResult:
    action: str
    status: Status
    http_status: int | None = None
    ms: float = 0.0
    detail: str = ""
    # Evidence for repair (never response values; see repair/observe.py):
    observed: dict[str, Any] | None = None       # drift: shape of the response
    enum_values: list[dict[str, Any]] = field(default_factory=list)  # drift: codes outside enums
    request_params: list[str] = field(default_factory=list)  # which inputs were sent (names only)
    error_body: str = ""                         # http_error / error: what the API said


@dataclass
class SmokeReport:
    connector: str
    results: list[SmokeResult] = field(default_factory=list)
    aborted: str | None = None

    def count(self, status: Status) -> int:
        return sum(r.status == status for r in self.results)

    @property
    def ok(self) -> bool:
        return not self.aborted and not (self.count("http_error") or self.count("error"))

    def to_dict(self) -> dict[str, Any]:
        return {"connector": self.connector, "aborted": self.aborted,
                "results": [asdict(r) for r in self.results]}

    def to_text(self, verbose: bool = False) -> str:
        marks = {"ok": "OK  ", "drift": "DRIFT", "http_error": "HTTP ", "error": "ERR ",
                 "skipped": "SKIP"}
        lines = []
        for r in self.results:
            if verbose or r.status != "ok":
                code = f" {r.http_status}" if r.http_status else ""
                lines.append(f"{marks[r.status]} {r.action}{code} ({r.ms:.0f} ms)"
                             + (f"\n      {r.detail}" if r.detail else ""))
        if self.aborted:
            lines.append(f"ABORTED: {self.aborted}")
        lines.append(f"\n{self.connector}: {self.count('ok')} ok, {self.count('drift')} drift, "
                     f"{self.count('http_error')} http errors, {self.count('error')} errors, "
                     f"{self.count('skipped')} skipped")
        return "\n".join(lines)


def _is_param(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _prefix_of(list_path: str, detail_path: str) -> bool:
    a = list_path.rstrip("/").split("/")
    b = detail_path.rstrip("/").split("/")
    return len(a) < len(b) and all(x == y or (_is_param(x) and _is_param(y)) for x, y in zip(a, b))


class _ValuePool:
    """Values observed in list responses, for filling later required params."""

    def __init__(self) -> None:
        self.by_path: list[tuple[str, dict[str, Any]]] = []  # (list path, first item)
        self.by_field: dict[str, Any] = {}

    def add(self, action: Action, items: list[Any]) -> None:
        first = next((i for i in items if isinstance(i, dict)), None)
        if first is None:
            return
        self.by_path.append((action.path, first))
        for k, v in first.items():
            if isinstance(v, (str, int)) and not isinstance(v, bool):
                self.by_field.setdefault(k, v)

    def lookup(self, action: Action, name: str) -> Any:
        # A list endpoint one level up: /pets -> /pets/{petId}
        for path, item in reversed(self.by_path):
            if _prefix_of(path, action.path):
                if name in item:
                    return item[name]
                if name.lower().endswith("id") and "id" in item:
                    return item["id"]
        return self.by_field.get(name)


def _fill_mcp(action: Action, inputs: dict[str, Any], global_params: dict[str, Any],
              pool: _ValuePool) -> tuple[dict[str, Any] | None, str]:
    schema = action.arguments_schema or {}
    props = schema.get("properties") or {}
    values = dict(inputs)
    for name in props:
        if name not in values and name in global_params:
            values[name] = global_params[name]
    for name in schema.get("required") or []:
        if name in values:
            continue
        prop = props.get(name) or {}
        if "default" in prop:
            values[name] = prop["default"]
        elif prop.get("enum"):
            values[name] = next(v for v in prop["enum"] if v is not None)
        elif pool.by_field.get(name) is not None:
            values[name] = pool.by_field[name]
        else:
            return None, f"needs required argument '{name}' (pass --param {name}=...)"
    return values, ""


def _fill(action: Action, inputs: dict[str, Any], global_params: dict[str, Any],
          pool: _ValuePool) -> tuple[dict[str, Any] | None, str]:
    if action.mcp_tool:
        return _fill_mcp(action, inputs, global_params, pool)
    values = dict(inputs)
    for p in action.params:
        if p.name in values:
            continue
        if p.name in global_params:
            values[p.name] = global_params[p.name]
            continue
        if not p.required:
            continue
        schema = p.json_schema
        if "default" in schema:
            values[p.name] = schema["default"]
        elif schema.get("enum"):
            values[p.name] = next(v for v in schema["enum"] if v is not None)
        else:
            found = pool.lookup(action, p.name)
            if found is None:
                return None, f"needs required {p.location} param '{p.name}' (pass --param {p.name}=...)"
            values[p.name] = found
    if action.body_required:
        return None, "needs a request body"
    p = action.pagination
    if p.limit_param and p.limit_param not in values:
        values[p.limit_param] = 2
    return values, ""


def run_smoke_tests(
    connector: Connector,
    credentials: Credentials | None,
    *,
    actions: list[str] | None = None,
    inputs: dict[str, dict[str, Any]] | None = None,
    global_params: dict[str, Any] | None = None,
    base_url: str | None = None,
    max_actions: int = 50,
    delay: float = 0.2,
    http: httpx.Client | None = None,
    retry: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    env: dict[str, str] | None = None,
) -> SmokeReport:
    report = SmokeReport(connector.name)
    if actions:
        chosen = [connector.action(a) for a in actions]
        unsafe = [a.name for a in chosen if not a.safe]
        if unsafe:
            raise ConnectorError(f"Smoke tests only run read-only GET actions; refusing {unsafe}.")
    else:
        chosen = [a for a in connector.actions if a.safe]
    # Parameterless actions first: their results feed the ones that need ids.
    chosen.sort(key=lambda a: (sum(p.required for p in a.params), a.path.count("{")))
    chosen = chosen[:max_actions]

    inputs = inputs or {}
    global_params = global_params or {}
    pool = _ValuePool()
    if connector.mcp is not None:
        from ..mcp.client import McpClient

        client: Any = McpClient(connector, credentials, env=env)
    else:
        client = ConnectorClient(connector, credentials, base_url=base_url, http=http,
                                 retry=retry or RetryPolicy(max_attempts=2, max_wait=10,
                                                            sleep=sleep))
    try:
        for i, act in enumerate(chosen):
            values, why = _fill(act, inputs.get(act.name, {}), global_params, pool)
            if values is not None and act.security and hasattr(client, "scheme_for") \
                    and client.scheme_for(act) is None:
                values, why = None, f"needs {' or '.join(act.security)} credentials"
            if values is None:
                report.results.append(SmokeResult(act.name, "skipped", detail=why))
                continue
            if i:
                sleep(delay)
            start = time.perf_counter()
            try:
                data = client.execute(act, values)
            except RateLimitError as e:
                report.results.append(SmokeResult(act.name, "http_error", e.status_code,
                                                  _ms(start), "rate limited"))
                report.aborted = "rate limited; stopped to avoid making it worse"
                break
            except ActionHTTPError as e:
                report.results.append(SmokeResult(act.name, "http_error", e.status_code,
                                                  _ms(start), _short(e.body),
                                                  request_params=sorted(values),
                                                  error_body=_clip(e.body)))
                if e.status_code == 401 and not any(r.status in ("ok", "drift") for r in report.results):
                    report.aborted = "credentials rejected (401); check the key or token"
                    break
                continue
            except ConnectorError as e:
                report.results.append(SmokeResult(act.name, "error", None, _ms(start), str(e),
                                                  request_params=sorted(values),
                                                  error_body=_clip(str(e))))
                continue
            elapsed = _ms(start)

            items = get_path(data, act.pagination.items_path)
            if isinstance(items, list):
                pool.add(act, items)
            elif isinstance(data, list):
                pool.add(act, data)

            drift = _drift(connector, act, data)
            result = SmokeResult(act.name, "drift" if drift else "ok", 200, elapsed, drift,
                                 request_params=sorted(values))
            if drift:
                from ..repair.observe import enum_findings, infer_shape

                result.observed = infer_shape(data)
                result.enum_values = enum_findings(_output_schema(connector, act), data)
            report.results.append(result)
    finally:
        client.close()
    return report


def _output_schema(connector: Connector, act: Action) -> dict[str, Any]:
    return {**(act.output_schema or {}), **({"$defs": connector.schemas} if connector.schemas else {})}


def _clip(body: Any, limit: int = 1500) -> str:
    s = body if isinstance(body, str) else json.dumps(body, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def _drift(connector: Connector, act: Action, data: Any) -> str:
    if act.output_schema is None or data is None:
        return ""
    schema = _output_schema(connector, act)
    try:
        error = BoundedValidator(schema).best_error(data)
    except SchemaTooLarge:
        return ""  # can't check this schema in reasonable time; not evidence of drift
    if error is None:
        return ""
    where = ".".join(map(str, error.path)) or "(root)"
    return f"response doesn't match schema at {where}: {describe_error(error)}"


def describe_error(error: Any) -> str:
    """A validation error described without quoting the offending value (responses can
    hold personal data, and smoke reports get saved and shared)."""
    from ..repair.observe import _type

    v, expected = error.validator, error.validator_value
    if v == "type":
        return f"expected {expected}, got {_type(error.instance)}"
    if v == "required":
        missing = [r for r in expected if isinstance(error.instance, dict) and r not in error.instance]
        return f"missing required field(s) {missing}"
    if v == "enum":
        return f"value outside the allowed set ({len(expected)} values)"
    if v == "additionalProperties" and isinstance(error.instance, dict):
        allowed = set(error.schema.get("properties") or {})
        return f"unexpected field(s) {sorted(k for k in error.instance if k not in allowed)[:5]}"
    if v in ("anyOf", "oneOf"):
        return f"matched none of the {len(expected)} allowed shapes (got {_type(error.instance)})"
    return f"failed the '{v}' constraint"


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _short(body: Any) -> str:
    s = str(body)
    return s if len(s) <= 160 else s[:160] + "…"
