"""Intermediate representation (IR) for connectors.

A Connector is pure data: everything the generic runtime needs to call an API.
Ingesters (OpenAPI today, LLM-read docs later) produce it; the runtime consumes it.
Because it's data rather than code, it can be validated, diffed and repaired.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

IR_VERSION = "1"

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class Param(BaseModel):
    name: str
    location: Literal["path", "query", "header"]
    required: bool = False
    json_schema: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    # How arrays are sent (OpenAPI `style` / `explode`). Defaults: query arrays repeat the
    # key (?id=1&id=2); explode=False joins them (?id=1,2); path/header arrays are joined.
    style: str | None = None
    explode: bool | None = None


class Pagination(BaseModel):
    # The runtime always prefers an RFC 8288 `Link: <...>; rel="next"` header when the
    # response has one; "link_header" means that header is the *only* way forward.
    style: Literal["none", "cursor", "offset", "page", "link_header"] = "none"
    # Request side
    request_param: str | None = None  # cursor / offset / page query param
    limit_param: str | None = None  # page-size query param, if any
    # Response side
    items_path: str | None = None  # dotted path to the item array; None = body is the array
    response_cursor_path: str | None = None  # dotted path to the next cursor (may be a URL)
    cursor_from_item_field: str | None = None  # e.g. Stripe: next cursor = last item's "id"
    has_more_path: str | None = None  # dotted path to a boolean "has more" flag


def pagination_problem(p: Pagination, query_params: set[str] | None = None) -> str | None:
    """Why this pagination setup can't work, or None. query_params: the action's
    query parameter names, when known (checks the referenced params exist)."""
    if p.style == "none":
        return None
    if query_params is not None:
        bad = [n for n in (p.request_param, p.limit_param) if n and n not in query_params]
        if bad:
            return f"unknown query parameter(s) {bad}"
    if p.style in ("cursor", "offset", "page") and not p.request_param:
        return f"style '{p.style}' needs a request_param"
    if p.style == "cursor" and not (p.response_cursor_path or p.cursor_from_item_field):
        return "cursor style needs response_cursor_path or cursor_from_item_field"
    return None


class OAuthFlow(BaseModel):
    """One OAuth2 flow from the spec (authorization code or client credentials)."""

    flow: Literal["authorization_code", "client_credentials"]
    authorization_url: str | None = None  # authorization_code only
    token_url: str
    refresh_url: str | None = None
    scopes: dict[str, str] = Field(default_factory=dict)  # scope -> description


class AuthScheme(BaseModel):
    id: str
    type: Literal["api_key", "bearer", "basic", "oauth2", "unsupported"]
    param_name: str | None = None  # api_key: header/query/cookie name
    location: Literal["header", "query", "cookie"] | None = None
    description: str = ""
    oauth_flows: list[OAuthFlow] = Field(default_factory=list)  # oauth2 only


class McpServerConfig(BaseModel):
    """How to reach an MCP server. Holds no secrets: values for `env_passthrough`
    names, and auth tokens, are supplied at runtime."""

    transport: Literal["stdio", "http"]
    # stdio: launch a local process
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)  # non-secret static values only
    env_passthrough: list[str] = Field(default_factory=list)  # names forwarded at runtime
    cwd: str | None = None
    # http: Streamable HTTP endpoint
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)  # non-secret static headers only

    def secret_problems(self) -> list[str]:
        """Places where this config would store something that looks like a secret.
        Connector files are meant to be shareable, so these are refused at import."""
        from .runtime.urls import looks_like_secret_value as looks_secret_command
        from .runtime.urls import secret_args, secret_env, secret_header_names, secret_query_names

        problems = []
        if names := secret_header_names(self.headers):
            problems.append(f"headers {', '.join(names)}")
        if self.url and (names := secret_query_names(self.url)):
            problems.append(f"URL query {', '.join(names)}")
        if names := secret_args(self.args):
            problems.append(f"command arguments {', '.join(names)}")
        if names := secret_env(self.env):
            problems.append(f"env values {', '.join(names)}")
        if self.command and looks_secret_command(self.command):
            problems.append("the command itself")
        return problems

    def describe(self) -> str:
        if self.transport == "stdio":
            return " ".join([self.command or "", *self.args]).strip()
        return self.url or ""


class Action(BaseModel):
    name: str
    operation_id: str | None = None
    summary: str = ""
    description: str = ""
    method: HttpMethod
    path: str
    params: list[Param] = Field(default_factory=list)
    body_schema: dict[str, Any] | None = None
    body_required: bool = False
    body_encoding: Literal["json", "form"] = "json"
    output_schema: dict[str, Any] | None = None
    pagination: Pagination = Field(default_factory=Pagination)
    destructive: bool = False
    tags: list[str] = Field(default_factory=list)
    # Things the deterministic pass couldn't settle; a later LLM enrichment pass reads these.
    notes: list[str] = Field(default_factory=list)
    # Where non-spec values came from, e.g. {"pagination": "llm:claude-sonnet-5-5"}.
    provenance: dict[str, str] = Field(default_factory=dict)
    # MCP tools: the server's tool name and its argument schema (used instead of
    # params/body). For these, method is "GET" when the tool declares readOnlyHint,
    # else "POST", so `safe` keeps meaning "read-only"; path is unused.
    # Auth schemes (ids in Connector.auth) this action accepts, from the spec's
    # per-operation `security`. None = any of the connector's schemes; [] = no auth.
    security: list[str] | None = None
    mcp_tool: str | None = None
    arguments_schema: dict[str, Any] | None = None
    mcp_annotations: dict[str, Any] = Field(default_factory=dict)
    # Former names that still resolve to this action, so workflows built against an
    # older version keep working after an upstream rename.
    aliases: list[str] = Field(default_factory=list)
    # Fixes learned from the live API when it disagreed with its spec, kept so they can
    # be re-applied when the connector is regenerated (see repair/live.py).
    observed_output: dict[str, Any] | None = None   # {"shape": ..., "enum_values": [...]}
    live_patches: list[dict[str, Any]] = Field(default_factory=list)  # input patch operations

    @property
    def safe(self) -> bool:
        """Read-only: OK to call in live smoke tests."""
        return self.method == "GET" and not self.destructive

    def input_schema(self) -> dict[str, Any]:
        """One JSON Schema covering every input: params by name, plus `body`
        (or, for MCP tools, the tool's own argument schema)."""
        if self.arguments_schema is not None:
            schema = dict(self.arguments_schema)
            schema.setdefault("type", "object")
            return schema
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in self.params:
            schema = dict(p.json_schema)
            if p.description and "description" not in schema:
                schema["description"] = p.description
            properties[p.name] = schema
            if p.required:
                required.append(p.name)
        if self.body_schema is not None:
            properties["body"] = self.body_schema
            if self.body_required:
                required.append("body")
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = list(dict.fromkeys(required))
        return schema


class WebhookSignature(BaseModel):
    """How a webhook sender signs deliveries (HMAC over the raw body).

    format "plain": header holds `<prefix><digest>` (GitHub: X-Hub-Signature-256,
        prefix "sha256=", hex).
    format "stripe": header holds `t=<ts>,v1=<hex>`; signed payload is `<ts>.<body>`.
    """

    header: str
    algorithm: Literal["sha256", "sha1", "sha512"] = "sha256"
    encoding: Literal["hex", "base64"] = "hex"
    prefix: str = ""
    format: Literal["plain", "stripe"] = "plain"
    tolerance_seconds: int = 300  # replay window for timestamped formats

    @classmethod
    def github(cls) -> WebhookSignature:
        return cls(header="X-Hub-Signature-256", prefix="sha256=")

    @classmethod
    def stripe(cls) -> WebhookSignature:
        return cls(header="Stripe-Signature", format="stripe")


class Trigger(BaseModel):
    """Starts a workflow when something new happens.

    polling: call `source_action` on a schedule; emit items whose `dedup_key`
        hasn't been seen before. `inputs` are fixed parameters for that call.
    webhook: the API pushes events to a URL; `payload_schema` describes them.
        `source_action` is the action that registers the subscription, if known.
    """

    name: str
    kind: Literal["polling", "webhook"]
    description: str = ""
    # polling
    source_action: str | None = None
    dedup_key: str | None = None  # dotted path in each item; None = hash the whole item
    inputs: dict[str, Any] = Field(default_factory=dict)
    order: Literal["newest_first", "oldest_first", "unknown"] = "unknown"
    # webhook
    event: str | None = None
    payload_schema: dict[str, Any] | None = None
    signature: WebhookSignature | None = None
    delivery_id_header: str | None = None  # e.g. X-GitHub-Delivery, for dedup
    provenance: dict[str, str] = Field(default_factory=dict)


class Connector(BaseModel):
    ir_version: str = IR_VERSION
    name: str
    title: str = ""
    description: str = ""
    api_version: str = ""
    base_url: str = ""
    auth: list[AuthScheme] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    triggers: list[Trigger] = Field(default_factory=list)
    provenance: dict[str, str] = Field(default_factory=dict)  # e.g. {"auth": "llm:..."}
    mcp: McpServerConfig | None = None  # set when the actions are tools on an MCP server
    notes: list[str] = Field(default_factory=list)
    # Shared JSON Schemas, referenced from actions as {"$ref": "#/$defs/<Name>"}.
    schemas: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None
    spec_hash: str = ""

    @property
    def kind(self) -> Literal["http", "mcp"]:
        return "mcp" if self.mcp is not None else "http"

    def action(self, name: str) -> Action:
        for a in self.actions:
            if a.name == name:
                return a
        for a in self.actions:
            if name in a.aliases:
                return a
        available = ", ".join(a.name for a in self.actions) or "(none)"
        raise KeyError(f"No action named '{name}'. Available: {available}")

    def input_schema(self, action: str | Action, *, prune: bool = False) -> dict[str, Any]:
        """An action's input schema, self-contained (shared schemas attached as $defs).
        prune=True attaches only the schemas it references (for sending over the wire)."""
        act = action if isinstance(action, Action) else self.action(action)
        schema = act.input_schema()
        if self.schemas:
            defs = referenced_defs(schema, self.schemas) if prune else self.schemas
            if defs:
                schema["$defs"] = defs
        return schema

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2, exclude_defaults=False) + "\n")

    @classmethod
    def load(cls, path: str | Path) -> Connector:
        return cls.model_validate_json(Path(path).read_text())


def referenced_defs(schema: Any, defs: dict[str, Any]) -> dict[str, Any]:
    """The subset of `defs` reachable from `schema` through `#/$defs/...` refs."""
    out: dict[str, Any] = {}
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref[len("#/$defs/"):].split("/")[0].replace("~1", "/").replace("~0", "~")
                if name not in out and name in defs:
                    out[name] = defs[name]
                    stack.append(defs[name])
            stack.extend(v for k, v in node.items() if k != "$defs")
        elif isinstance(node, list):
            stack.extend(node)
    return out


def hash_spec(spec: Any) -> str:
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
