"""Deterministic OpenAPI 3.x -> Connector IR.

No LLM here. Anything this pass can't decide confidently is recorded in
`Action.notes` so a later enrichment pass (LLM or human) can resolve it.

Schema references (`#/components/schemas/X`) are kept as references and the
referenced schemas are collected into `Connector.schemas` (exposed to the
validator as `$defs`). Inlining them instead blows up exponentially on large
real-world specs such as Stripe's.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin

import httpx
import yaml

from ..errors import UnsupportedSpecError
from ..ir import Action, AuthScheme, Connector, OAuthFlow, Pagination, Param, Trigger, hash_spec
from ..limits import _canonical

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
SCHEMA_REF_PREFIX = "#/components/schemas/"
DEFS_PREFIX = "#/$defs/"

CURSOR_PARAMS = {
    "cursor", "page_token", "pagetoken", "next_token", "nexttoken",
    "continuation_token", "continuationtoken", "after", "starting_after",
}
ITEM_CURSOR_PARAMS = {"starting_after"}  # cursor = id of the last item (Stripe style)
OFFSET_PARAMS = {"offset", "skip", "start"}
PAGE_PARAMS = {"page", "page_number", "pagenumber"}
LIMIT_PARAMS = {
    "limit", "per_page", "perpage", "page_size", "pagesize",
    "max_results", "maxresults", "count", "size",
}
RESPONSE_CURSOR_FIELDS = {
    "next_cursor", "nextcursor", "next_page_token", "nextpagetoken",
    "next_token", "nexttoken", "cursor", "next",
}
HAS_MORE_FIELDS = {"has_more", "hasmore", "has_next", "hasnext"}
ITEM_ARRAY_PREFERENCE = ("data", "items", "results", "records", "entries", "values")
DESTRUCTIVE_WORDS = re.compile(r"(^|_)(delete|remove|destroy|purge|cancel|revoke|erase|wipe|truncate|terminate)(_|$)")

# Keys whose values are maps of name -> schema (so the names are NOT keywords).
_SCHEMA_MAP_KEYS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
# Keys whose values are literal data, not schemas.
_LITERAL_KEYS = {"enum", "const", "default", "examples", "required", "example"}


# --------------------------------------------------------------------------- loading


_Base = getattr(yaml, "CSafeLoader", yaml.SafeLoader)  # C parser when available: ~10x faster


class _SpecLoader(_Base):  # type: ignore[misc, valid-type]
    """Safe YAML loading for API specs: dates stay strings (`example: 2024-01-01` must
    not become a datetime), and unknown tags are read as plain values."""


# OpenAPI requires YAML 1.2, whose core schema differs from PyYAML's YAML 1.1 rules:
# only true/false are booleans (not on/yes/no/off), no 1_000 or 0777 numbers, no
# sexagesimal 1:20, and no timestamps (dates stay strings).
_YAML11_ONLY = {"tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:bool",
                "tag:yaml.org,2002:int", "tag:yaml.org,2002:float"}


def _plain(loader: Any, suffix: str, node: Any) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


def _configure(cls: Any, base: Any) -> None:
    cls.yaml_implicit_resolvers = {
        ch: [(tag, rx) for tag, rx in resolvers if tag not in _YAML11_ONLY]
        for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    cls.add_implicit_resolver(
        "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
    cls.add_implicit_resolver(
        "tag:yaml.org,2002:int", re.compile(r"^[-+]?[0-9]+$"), list("-+0123456789"))
    cls.add_constructor(  # decimal, as YAML 1.2 says (1.1 would read 0777 as octal 511)
        "tag:yaml.org,2002:int", lambda loader, node: int(loader.construct_scalar(node)))
    cls.add_implicit_resolver(
        "tag:yaml.org,2002:float",
        re.compile(r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
                   r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
        list("-+0123456789."))

    def construct_mapping(self: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
        # Keys exactly as written: `18_24:` isn't the int 1824, `200:` isn't an int, `on:` isn't True.
        for key_node, _ in node.value:
            if isinstance(key_node, yaml.ScalarNode) and key_node.tag in (
                    "tag:yaml.org,2002:int", "tag:yaml.org,2002:float", "tag:yaml.org,2002:bool",
                    "tag:yaml.org,2002:null"):
                key_node.tag = "tag:yaml.org,2002:str"
        # Merge keys (`<<: *anchor`) copy every key of the anchor into this mapping while it's
        # built, so a small file can expand to gigabytes; count entries as they're made.
        if hasattr(self, "flatten_mapping"):
            self.flatten_mapping(node)
        self._built_entries = getattr(self, "_built_entries", 0) + len(node.value)
        if self._built_entries > MAX_SPEC_NODES:
            raise UnsupportedSpecError(
                f"Spec expands to more than {MAX_SPEC_NODES:,} values (YAML aliases or merge "
                "keys that fan out); refusing to load it.")
        return base.construct_mapping(self, node, deep=deep)

    cls.construct_mapping = construct_mapping
    cls.add_multi_constructor("", _plain)


_configure(_SpecLoader, _Base)


def _yaml_depth_ok(text: str) -> None:
    """Refuse YAML nested deeper than MAX_SPEC_DEPTH before building it. Building recurses
    (in C, with the fast loader, where very deep input crashes the process instead of
    raising), but the event stream is produced iteratively, so depth is measured on events
    from the real parser -- no guessing about brackets, quotes, comments or "- - -" lists."""
    depth = 0
    starts = (yaml.MappingStartEvent, yaml.SequenceStartEvent)
    ends = (yaml.MappingEndEvent, yaml.SequenceEndEvent)
    for event in yaml.parse(text, Loader=_Base):
        if isinstance(event, starts):
            depth += 1
            if depth > MAX_SPEC_DEPTH:
                raise UnsupportedSpecError(f"Spec is nested more than {MAX_SPEC_DEPTH} levels deep.")
        elif isinstance(event, ends):
            depth -= 1


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _string_keys(node: Any) -> Any:
    """YAML turns `200:` into an int key and `on:` into a bool; JSON Schema wants strings."""
    if isinstance(node, dict):
        return {(str(k).lower() if isinstance(k, bool) else str(k)): _string_keys(v)
                for k, v in node.items()}
    if isinstance(node, list):
        return [_string_keys(v) for v in node]
    return node


# Limits for untrusted specs. The largest real specs (GitHub, Stripe, Azure) are far below
# these: ~11 MB, a few million nodes, nesting in the tens.
MAX_SPEC_BYTES = 100_000_000
MAX_SPEC_NODES = 5_000_000    # after expanding YAML aliases and inlined $refs (GitHub: ~0.3M)
MAX_SPEC_DEPTH = 300
SPEC_FETCH_TIMEOUT = 120.0    # seconds for the whole download


def check_spec_size(data: Any) -> None:
    """Refuse documents that would expand past the limits. YAML aliases make shared
    objects, so `a: &x [...]`, `b: [*x, *x, ...]` is tiny to parse but exponential to walk
    ("billion laughs"); counting stops as soon as a limit is passed, so this is cheap."""
    nodes = 0
    stack: list[tuple[Any, int]] = [(data, 0)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_SPEC_NODES:
            raise UnsupportedSpecError(
                f"Spec expands to more than {MAX_SPEC_NODES:,} values (YAML aliases or $refs "
                "that fan out); refusing to load it.")
        if depth > MAX_SPEC_DEPTH:
            raise UnsupportedSpecError(f"Spec is nested more than {MAX_SPEC_DEPTH} levels deep.")
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)


def parse_spec_text(text: str) -> Any:
    if len(text) > MAX_SPEC_BYTES:
        raise UnsupportedSpecError(f"Spec is larger than {MAX_SPEC_BYTES // 1_000_000} MB.")
    try:
        data = _parse(text)
    except RecursionError:
        raise UnsupportedSpecError("Spec is nested too deeply to parse.") from None
    check_spec_size(data)
    return _string_keys(data)


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        _yaml_depth_ok(text)
        return yaml.load(text, Loader=_SpecLoader)  # noqa: S506 - safe loader
    except yaml.reader.ReaderError:
        text = _CONTROL.sub("", text)
        _yaml_depth_ok(text)
        return yaml.load(text, Loader=_SpecLoader)  # noqa: S506
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        problem = getattr(e, "problem", None) or str(e).splitlines()[0]
        raise UnsupportedSpecError(f"Spec isn't valid JSON or YAML{where}: {problem}") from None


def fetch_spec_text(url: str, *, max_bytes: int | None = None,
                    timeout: float = SPEC_FETCH_TIMEOUT) -> str:
    """Download a spec with a size cap and an overall deadline (not just per-read)."""
    import time

    limit = max_bytes or MAX_SPEC_BYTES
    deadline = time.monotonic() + timeout
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=min(60.0, timeout)) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise UnsupportedSpecError(f"Spec at {url} is larger than {limit // 1_000_000} MB.")
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise UnsupportedSpecError(f"Spec at {url} is larger than {limit // 1_000_000} MB.")
                if time.monotonic() > deadline:
                    raise UnsupportedSpecError(f"Downloading {url} took longer than {timeout:.0f}s.")
                chunks.append(chunk)
            return b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
    except httpx.HTTPError as e:
        raise UnsupportedSpecError(f"Couldn't download {url}: {type(e).__name__}: {e}") from None


def load_spec(source: str | Path) -> dict[str, Any]:
    """Load an OpenAPI document from a URL or local path (JSON or YAML)."""
    src = str(source)
    if src.startswith(("http://", "https://")):
        text = fetch_spec_text(src)
    else:
        path = Path(src)
        if path.is_file() and path.stat().st_size > MAX_SPEC_BYTES:
            raise UnsupportedSpecError(f"Spec is larger than {MAX_SPEC_BYTES // 1_000_000} MB.")
        text = path.read_text(encoding="utf-8", errors="replace")
    spec = parse_spec_text(text)
    if not isinstance(spec, dict):
        raise UnsupportedSpecError("Spec did not parse to an object.")
    if "swagger" in spec:
        from .swagger2 import convert_swagger2

        spec = convert_swagger2(spec)
    if not str(spec.get("openapi", "")).startswith("3."):
        raise UnsupportedSpecError("Expected an OpenAPI 3.x document ('openapi: 3.x').")
    return spec


def normalize_schema(schema: Any) -> Any:
    """Turn OpenAPI 3.0 schema quirks into plain JSON Schema (e.g. nullable -> null type)."""
    if isinstance(schema, list):
        return [normalize_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in ("example", "xml", "externalDocs"):
            continue
        if k in _SCHEMA_MAP_KEYS and isinstance(v, dict):
            out[k] = {name: normalize_schema(sub) for name, sub in v.items()}
        elif k in _LITERAL_KEYS:
            out[k] = v
        else:
            out[k] = normalize_schema(v)
    for excl, bound in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
        if isinstance(out.get(excl), bool):  # OpenAPI 3.0 / draft-4 form
            if out.pop(excl) and bound in out:
                out[excl] = out.pop(bound)
    for key in ("enum", "required"):
        if isinstance(out.get(key), list):
            seen: set[str] = set()
            unique: list[Any] = []
            for v in out[key]:  # linear: enums can be tens of thousands of values long
                k = _canonical(v)
                if k not in seen:
                    seen.add(k)
                    unique.append(v)
            out[key] = unique
    js = re.fullmatch(r"/(.*)/[a-z]*", out["pattern"]) if isinstance(out.get("pattern"), str) else None
    if js:  # a JavaScript regex literal: '/^[a-z]+$/' -> '^[a-z]+$'
        out["pattern"] = js.group(1)
    if isinstance(out.get("items"), list):
        branches = out.pop("items")
        out["items"] = branches[0] if len(branches) == 1 else {"anyOf": branches}
    t = out.get("type")
    if isinstance(t, str) and t not in _JSON_TYPES:
        out["x-type"] = out.pop("type")  # e.g. 'application/json': not a type; don't enforce it
    elif isinstance(t, list) and not set(t) <= _JSON_TYPES:
        out["type"] = [x for x in t if x in _JSON_TYPES] or out.pop("type")
    if isinstance(out.get("pattern"), str):
        try:
            re.compile(out["pattern"])
        except re.error:  # e.g. \p{L} (ECMA/PCRE); keep it for docs, don't validate with it
            out["x-pattern"] = out.pop("pattern")
    if out.pop("nullable", False):
        if "$ref" in out:
            ref = out.pop("$ref")
            return {**out, "anyOf": [{"$ref": ref}, {"type": "null"}]}
        t = out.get("type")
        if isinstance(t, str):
            out["type"] = [t, "null"]
        elif isinstance(t, list) and "null" not in t:
            out["type"] = [*t, "null"]
        if isinstance(out.get("enum"), list) and None not in out["enum"]:
            out["enum"] = [*out["enum"], None]
    return out


def _unescape(part: str) -> str:
    """One JSON Pointer segment from a URI fragment: percent-decode, then ~1 and ~0."""
    return unquote(part).replace("~1", "/").replace("~0", "~")


def _escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


class _Resolver:
    """Inlines non-schema $refs (parameters, bodies, responses) and rewrites schema
    $refs to `#/$defs/...`, collecting only the schemas that are actually used."""

    def __init__(self, spec: dict[str, Any], budget: int | None = None):
        self.spec = spec
        self.raw_schemas: dict[str, Any] = (spec.get("components") or {}).get("schemas") or {}
        self.defs: dict[str, Any] = {}
        self._building: set[str] = set()
        # values we may still produce: inlined $refs can fan out
        self._budget = MAX_SPEC_NODES if budget is None else budget

    def _lookup(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            raise UnsupportedSpecError(f"External $ref not supported yet: {ref}")
        node: Any = self.spec
        for part in ref[2:].split("/"):
            try:
                key = _unescape(part)
                node = node[int(key)] if isinstance(node, list) else node[key]
            except (KeyError, TypeError, IndexError, ValueError):
                raise UnsupportedSpecError(f"Unresolvable $ref: {ref}") from None
        return node

    def _ensure_def(self, name: str) -> None:
        if name in self.defs or name in self._building:
            return
        if name not in self.raw_schemas:
            raise UnsupportedSpecError(f"Unresolvable $ref: {SCHEMA_REF_PREFIX}{name}")
        self._building.add(name)
        self.defs[name] = normalize_schema(self.resolve(self.raw_schemas[name]))
        self._building.discard(name)

    def resolve(self, node: Any, stack: frozenset[str] = frozenset()) -> Any:
        self._budget -= 1
        if self._budget < 0:
            raise UnsupportedSpecError(
                f"Spec expands to more than {MAX_SPEC_NODES:,} values once its $refs are "
                "inlined (refs that fan out); refusing to load it.")
        if len(stack) > MAX_SPEC_DEPTH:
            raise UnsupportedSpecError(f"$refs nest more than {MAX_SPEC_DEPTH} levels deep.")
        if isinstance(node, list):
            return [self.resolve(x, stack) for x in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if not isinstance(ref, str):
            return {k: self.resolve(v, stack) for k, v in node.items()}

        siblings = {k: self.resolve(v, stack) for k, v in node.items() if k != "$ref"}
        if ref.startswith(SCHEMA_REF_PREFIX):
            segments = [_unescape(p) for p in ref[len(SCHEMA_REF_PREFIX):].split("/")]
            self._ensure_def(segments[0])
            # re-encode without percent-escapes, so every consumer only handles ~0/~1
            return {"$ref": DEFS_PREFIX + "/".join(_escape(p) for p in segments), **siblings}
        if ref in stack:
            return {"description": f"(recursive reference to {ref})", **siblings}
        resolved = self.resolve(self._lookup(ref), stack | {ref})
        if siblings and isinstance(resolved, dict):
            return {**resolved, **siblings}
        return resolved

    def deref(self, schema: Any) -> Any:
        """Follow `#/$defs/...` refs until reaching a concrete schema (for inspection only)."""
        for _ in range(32):
            if not (isinstance(schema, dict) and isinstance(schema.get("$ref"), str)):
                return schema
            ref = schema["$ref"]
            if not ref.startswith(DEFS_PREFIX):
                return schema
            parts = [_unescape(p) for p in ref[len(DEFS_PREFIX):].split("/")]
            node: Any = self.defs.get(parts[0])
            for p in parts[1:]:
                node = node.get(p) if isinstance(node, dict) else None
            siblings = {k: v for k, v in schema.items() if k != "$ref"}
            schema = {**(node if isinstance(node, dict) else {}), **siblings}
        return schema


# --------------------------------------------------------------------------- helpers


def _valid_path(path: Any) -> bool:
    """A path the runtime appends to the base URL: must start with '/', with no backslashes
    or control characters (anything else, like "@evil.test/x", could change the host)."""
    return (isinstance(path, str) and path.startswith("/")
            and "\\" not in path and not any(ord(ch) < 0x21 for ch in path))


def snake_case(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    return text.strip("_").lower() or "action"


def _action_name(method: str, path: str, op: dict[str, Any]) -> str:
    if op.get("operationId"):
        return snake_case(op["operationId"])
    parts = []
    for seg in path.strip("/").split("/"):
        if seg.startswith("{") and seg.endswith("}"):
            parts.append("by_" + seg[1:-1])
        elif seg:
            parts.append(seg)
    return snake_case(f"{method}_{'_'.join(parts) or 'root'}")


def _base_url(spec: dict[str, Any], source: str | None) -> str:
    servers = spec.get("servers") or []
    if not servers:
        return ""
    server = servers[0]
    url = server.get("url", "")
    for var, meta in (server.get("variables") or {}).items():
        url = url.replace("{" + var + "}", str(meta.get("default", "")))
    if url and not url.startswith(("http://", "https://")) and source and source.startswith("http"):
        url = urljoin(source, url)
    return url.rstrip("/")


def _auth_schemes(spec: dict[str, Any]) -> list[AuthScheme]:
    schemes = (spec.get("components") or {}).get("securitySchemes") or {}
    out: list[AuthScheme] = []
    for sid, s in schemes.items():
        stype = s.get("type")
        desc = s.get("description", "")
        scheme = str(s.get("scheme", "")).lower()
        if stype == "apiKey":
            out.append(AuthScheme(id=sid, type="api_key", param_name=s.get("name"),
                                  location=s.get("in", "header"), description=desc))
        elif stype == "http" and scheme == "bearer":
            out.append(AuthScheme(id=sid, type="bearer", description=desc))
        elif stype == "http" and scheme == "basic":
            out.append(AuthScheme(id=sid, type="basic", description=desc))
        elif stype in ("oauth2", "openIdConnect"):
            out.append(AuthScheme(id=sid, type="oauth2", description=desc,
                                  oauth_flows=_oauth_flows(s.get("flows") or {})))
        else:
            kind = f"{stype} {s.get('scheme')}".strip() if stype == "http" else str(stype)
            out.append(AuthScheme(id=sid, type="unsupported",
                                  description=f"unsupported auth type: {kind}" + (f". {desc}" if desc else "")))
    return out


def _oauth_flows(flows: dict[str, Any]) -> list[OAuthFlow]:
    out: list[OAuthFlow] = []
    for key, kind in (("authorizationCode", "authorization_code"),
                      ("clientCredentials", "client_credentials")):
        f = flows.get(key)
        if isinstance(f, dict) and f.get("tokenUrl"):
            out.append(OAuthFlow(
                flow=kind,
                authorization_url=f.get("authorizationUrl"),
                token_url=f["tokenUrl"],
                refresh_url=f.get("refreshUrl"),
                scopes={str(k): str(v) for k, v in (f.get("scopes") or {}).items()},
            ))
    return out


def _webhook_trigger(name: str, item: dict[str, Any], used: set[str],
                     register_action: str | None) -> Trigger | None:
    op = next((item[m] for m in HTTP_METHODS if isinstance(item.get(m), dict)), None)
    if op is None:
        return None
    req = op.get("requestBody") if isinstance(op.get("requestBody"), dict) else {}
    schema, _ = _media(req.get("content"))
    tname, n = snake_case(f"on_{name}"), 2
    base = tname
    while tname in used:
        tname, n = f"{base}_{n}", n + 1
    used.add(tname)
    return Trigger(
        name=tname,
        kind="webhook",
        event=name,
        description=op.get("summary") or op.get("description", "")[:200],
        source_action=register_action,
        payload_schema=normalize_schema(schema) if schema is not None else None,
        provenance={"trigger": "openapi"},
    )


def _media(content: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    """Pick the JSON (or form) media type from a content map. Returns (schema, encoding)."""
    if not content:
        return None, None
    for mt, media in content.items():
        base = mt.split(";")[0].strip().lower()
        if base == "application/json" or base.endswith("+json"):
            return (media or {}).get("schema") or {}, "json"
    for mt, media in content.items():
        if mt.split(";")[0].strip().lower() == "application/x-www-form-urlencoded":
            return (media or {}).get("schema") or {}, "form"
    return None, None


class _Detector:
    """Response-shape heuristics (pagination), able to see through $defs refs."""

    def __init__(self, resolver: _Resolver):
        self.r = resolver

    def properties(self, schema: Any) -> dict[str, Any]:
        schema = self.r.deref(schema)
        if not isinstance(schema, dict):
            return {}
        props = dict(schema.get("properties") or {})
        for sub in schema.get("allOf") or []:
            props.update(self.properties(sub))
        return props

    def find_field(self, schema: Any, names: set[str], depth: int = 2) -> str | None:
        props = self.properties(schema)
        for key in props:
            if key.lower() in names:
                return key
        if depth > 1:
            for key, sub in props.items():
                found = self.find_field(sub, names, depth - 1)
                if found:
                    return f"{key}.{found}"
        return None

    def items_path(self, schema: Any) -> tuple[bool, str | None]:
        """(found, path). found with path None means the body itself is the array."""
        schema = self.r.deref(schema)
        if not isinstance(schema, dict):
            return False, None
        if schema.get("type") == "array" or ("items" in schema and "properties" not in schema):
            return True, None
        props = self.properties(schema)
        arrays = [k for k, v in props.items()
                  if isinstance(self.r.deref(v), dict) and self.r.deref(v).get("type") == "array"]
        for pref in ITEM_ARRAY_PREFERENCE:
            if pref in arrays:
                return True, pref
        if len(arrays) == 1:
            return True, arrays[0]
        return False, None

    def pagination(self, method: str, params: list[Param], output_schema: Any,
                   notes: list[str]) -> Pagination:
        if method != "GET":
            return Pagination()
        query = {p.name.lower(): p.name for p in params if p.location == "query"}
        limit = next((query[n] for n in query if n in LIMIT_PARAMS), None)
        cursor = next((query[n] for n in query if n in CURSOR_PARAMS), None)
        offset = next((query[n] for n in query if n in OFFSET_PARAMS), None)
        page = next((query[n] for n in query if n in PAGE_PARAMS), None)
        if not (cursor or offset or page):
            return Pagination()

        found, items = self.items_path(output_schema)
        if not found:
            notes.append("Looks paginated, but the item array in the response couldn't be located.")
            return Pagination()
        has_more = self.find_field(output_schema, HAS_MORE_FIELDS)

        if cursor:
            if cursor.lower() in ITEM_CURSOR_PARAMS:
                return Pagination(style="cursor", request_param=cursor, limit_param=limit,
                                  items_path=items, cursor_from_item_field="id",
                                  has_more_path=has_more)
            resp_cursor = self.find_field(output_schema, RESPONSE_CURSOR_FIELDS)
            if resp_cursor:
                return Pagination(style="cursor", request_param=cursor, limit_param=limit,
                                  items_path=items, response_cursor_path=resp_cursor,
                                  has_more_path=has_more)
            # No cursor in the body: most such APIs (e.g. GitHub) put it in a Link header.
            notes.append(f"Cursor param '{cursor}' has no matching response field; assuming "
                         "Link-header pagination (verify).")
            return Pagination(style="link_header", request_param=cursor, limit_param=limit,
                              items_path=items)
        if offset:
            return Pagination(style="offset", request_param=offset, limit_param=limit,
                              items_path=items)
        return Pagination(style="page", request_param=page, limit_param=limit, items_path=items)


def _security(requirements: Any, known: set[str]) -> list[str] | None:
    """Scheme ids an operation accepts. None when the spec says nothing."""
    if requirements is None:
        return None
    if requirements == []:
        return []  # `security: []` = public endpoint, overriding any global requirement
    ids: list[str] = []
    for req in requirements or []:
        if isinstance(req, dict):
            ids += [k for k in req if k in known and k not in ids]
    if not ids and any(isinstance(r, dict) and not r for r in requirements or []):
        return []  # explicitly anonymous
    return ids or None


_JSON_TYPES = {"string", "number", "integer", "boolean", "array", "object", "null"}
_RUNTIME_HEADERS = {"content-type", "content-length", "accept-encoding", "host", "connection"}


def _split_query_template(path: str, params: list[Param],
                          notes: list[str]) -> tuple[str, list[Param]]:
    """Move `?key={param}` pairs out of a path template into real query params."""
    base, _, query = path.partition("?")
    static: list[str] = []
    by_name = {p.name: p for p in params}
    for pair in filter(None, query.split("&")):
        key, _, value = pair.partition("=")
        m = re.fullmatch(r"\{([^}]+)\}", value)
        target = by_name.get(m.group(1)) if m else None
        if target is not None:
            target.location = "query"
            target.name = key or target.name
            target.required = target.required
        else:
            static.append(pair)
    notes.append("Path template contained a query string; moved its parameters to the query.")
    return base + (("?" + "&".join(static)) if static else ""), params


def _merge_params(path_level: list[dict], op_level: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for p in [*path_level, *op_level]:
        if isinstance(p, dict):
            merged[(p.get("name", ""), p.get("in", ""))] = p
    return list(merged.values())


# --------------------------------------------------------------------------- main entry


def connector_from_openapi(
    source: str | Path | dict[str, Any],
    *,
    name: str | None = None,
    base_url: str | None = None,
    derive_triggers: bool = True,
) -> Connector:
    """Build a Connector from an OpenAPI 3.x spec (URL, path, or already-loaded dict).
    Webhooks/callbacks in the spec become webhook triggers; with derive_triggers,
    list actions with stable item ids also become polling triggers."""
    try:
        return _connector_from_openapi(source, name=name, base_url=base_url,
                                       derive_triggers=derive_triggers)
    except RecursionError:
        raise UnsupportedSpecError("Spec is nested too deeply to process.") from None


def _connector_from_openapi(
    source: str | Path | dict[str, Any],
    *,
    name: str | None,
    base_url: str | None,
    derive_triggers: bool,
) -> Connector:
    if isinstance(source, dict):
        check_spec_size(source)
        spec, source_str = source, None
        if "swagger" in spec:
            from .swagger2 import convert_swagger2

            spec = convert_swagger2(spec)
    else:
        source_str = str(source)
        spec = load_spec(source_str)

    resolver = _Resolver(spec)
    detector = _Detector(resolver)
    scheme_ids = set(((spec.get("components") or {}).get("securitySchemes") or {}).keys())
    global_security = spec.get("security")
    info = spec.get("info") or {}
    title = info.get("title") or "api"

    actions: list[Action] = []
    used: set[str] = set()
    triggers: list[Trigger] = []
    trigger_names: set[str] = set()

    bad_paths: list[str] = []
    for path, raw_item in (spec.get("paths") or {}).items():
        if not _valid_path(path):  # e.g. "@evil.test/x" would change the host after base_url
            bad_paths.append(path)
            continue
        item = resolver.resolve(raw_item)
        if not isinstance(item, dict):
            continue
        path_params = item.get("parameters") or []
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            notes: list[str] = []

            params: list[Param] = []
            for p in _merge_params(path_params, op.get("parameters") or []):
                loc = p.get("in")
                if loc not in ("path", "query", "header") or not p.get("name"):
                    if loc == "cookie":
                        notes.append(f"Cookie parameter '{p.get('name')}' ignored.")
                    continue
                if loc == "header" and p["name"].lower() in _RUNTIME_HEADERS:
                    continue  # set by the runtime from the body encoding / connection
                if loc == "header" and p["name"].lower() == "authorization" and scheme_ids:
                    notes.append("'Authorization' header parameter dropped: auth comes from "
                                 "the connector's auth schemes.")
                    continue
                schema = p.get("schema")
                if schema is None:
                    schema, _ = _media(p.get("content"))
                params.append(Param(
                    name=p["name"],
                    location=loc,
                    required=bool(p.get("required")) or loc == "path",
                    json_schema=normalize_schema(schema or {}),
                    description=p.get("description", ""),
                    style=p.get("style"),
                    explode=p.get("explode") if isinstance(p.get("explode"), bool) else None,
                ))

            declared = {x.name for x in params if x.location == "path"}
            for placeholder in re.findall(r"\{([^}?/]+)\}", path.split("?")[0]):
                if placeholder not in declared:
                    params.append(Param(name=placeholder, location="path", required=True,
                                        json_schema={"type": "string"}))
                    notes.append(f"Path placeholder '{{{placeholder}}}' wasn't declared; added as a "
                                 "required string.")
            by_name: dict[str, Param] = {}
            for prm in params:
                kept = by_name.get(prm.name)
                if kept is None or (prm.location == "path" and kept.location != "path"):
                    by_name[prm.name] = prm
            if len(by_name) < len(params):
                dropped = [f"{x.location} '{x.name}'" for x in params if by_name[x.name] is not x]
                notes.append(f"Duplicate parameter names; ignored {', '.join(dropped)}.")
                params = [x for x in params if by_name[x.name] is x]
            op_path = path
            if "?" in path:  # query string inside the path template: /export?filename={filename}
                op_path, params = _split_query_template(path, params, notes)

            body_schema, body_encoding, body_required = None, "json", False
            req = op.get("requestBody")
            if isinstance(req, dict):
                schema, enc = _media(req.get("content"))
                if schema is None and req.get("content"):
                    notes.append("Request body uses an unsupported media type "
                                 f"({', '.join(req['content'])}); body not modeled.")
                elif schema is not None:
                    body_schema = normalize_schema(schema)
                    body_encoding = enc or "json"
                    body_required = bool(req.get("required"))

            output_schema = None
            for code, resp in sorted((op.get("responses") or {}).items(), key=lambda kv: str(kv[0])):
                if str(code).startswith("2") and isinstance(resp, dict):
                    schema, _ = _media(resp.get("content"))
                    if schema is not None:
                        output_schema = normalize_schema(schema)
                        break

            action_name = _action_name(method, path, op)
            base_name, n = action_name, 2
            while action_name in used:
                action_name, n = f"{base_name}_{n}", n + 1
            used.add(action_name)

            for cb_name, cb in (op.get("callbacks") or {}).items():
                for cb_item in (cb or {}).values() if isinstance(cb, dict) else []:
                    if isinstance(cb_item, dict):
                        t = _webhook_trigger(cb_name, cb_item, trigger_names, action_name)
                        if t:
                            triggers.append(t)

            upper = method.upper()
            actions.append(Action(
                name=action_name,
                operation_id=op.get("operationId"),
                summary=op.get("summary", ""),
                description=op.get("description", ""),
                method=upper,
                path=op_path,
                params=params,
                body_schema=body_schema,
                body_required=body_required,
                body_encoding=body_encoding,
                output_schema=output_schema,
                pagination=detector.pagination(upper, params, output_schema, notes),
                destructive=upper == "DELETE" or bool(DESTRUCTIVE_WORDS.search(action_name)),
                tags=list(op.get("tags") or []),
                notes=notes,
                security=_security(op.get("security", global_security), scheme_ids),
            ))

    webhooks = {**(spec.get("x-webhooks") or {}), **(spec.get("webhooks") or {})}
    for event, raw in webhooks.items():  # OpenAPI 3.1 `webhooks`, or the 3.0-era `x-webhooks`
        item = resolver.resolve(raw)
        if isinstance(item, dict):
            t = _webhook_trigger(event, item, trigger_names, None)
            if t:
                triggers.append(t)

    spec_base = _base_url(spec, source_str).rstrip("/")
    resolved_base = (base_url or spec_base).rstrip("/")
    connector_notes = []
    if bad_paths:
        connector_notes.append(f"Skipped {len(bad_paths)} path(s) that don't start with '/' "
                               f"(or contain backslashes): {', '.join(repr(x) for x in bad_paths[:5])}.")
    if spec.get("x-converted-from") == "swagger 2.0":
        connector_notes.append("Converted from Swagger 2.0.")
    unfilled = re.findall(r"\{[^}]+\}", resolved_base)
    if unfilled:
        connector_notes.append(f"The spec's server URL '{resolved_base}' has placeholders "
                               f"{', '.join(unfilled)} with no default; pass the full base URL "
                               "when calling.")
    elif resolved_base and not resolved_base.startswith(("http://", "https://")):
        connector_notes.append(f"The spec's server URL '{resolved_base}' is relative; pass the "
                               "full base URL (e.g. https://your-host{0}) when calling.".format(resolved_base))
    connector = Connector(
        name=name or snake_case(title),
        title=title,
        notes=connector_notes,
        description=info.get("description", ""),
        api_version=str(info.get("version", "")),
        base_url=resolved_base,
        auth=_auth_schemes(spec),
        actions=actions,
        triggers=triggers,
        schemas=resolver.defs,
        source=source_str,
        spec_hash=hash_spec(spec),
    )
    if base_url and spec_base and spec_base != resolved_base:
        connector.provenance["spec_base_url"] = spec_base  # so `check` doesn't flag the override
    if derive_triggers:
        from ..triggers.derive import derive_polling_triggers

        connector.triggers += derive_polling_triggers(connector)
    return connector
