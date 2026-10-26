"""Build example values from JSON Schemas: inputs to send, and fake responses to mock."""

from __future__ import annotations

import re
from typing import Any

DEFS_PREFIX = "#/$defs/"
_CYCLE = object()  # returned when a recursive schema loops back on itself

_FORMAT_EXAMPLES = {
    "date-time": "2026-01-01T00:00:00Z",
    "date": "2026-01-01",
    "time": "12:00:00Z",
    "email": "user@example.com",
    "uri": "https://example.com/resource",
    "url": "https://example.com/resource",
    "uri-reference": "/resource",
    "hostname": "example.com",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "uuid": "123e4567-e89b-42d3-a456-426614174000",
    "byte": "ZXhhbXBsZQ==",
    "binary": "example",
    "password": "example-password",
}


class Synthesizer:
    """Deterministic example generator.

    full=False: only required properties (what a minimal valid request needs).
    full=True: every property down to `full_depth` (fake responses that exercise
    pagination fields, cursors and nested objects); required-only below that.
    """

    def __init__(self, defs: dict[str, Any] | None = None, *, full: bool = False,
                 full_depth: int = 3, max_depth: int = 12, max_nodes: int = 200_000):
        self.defs = defs or {}
        self.full = full
        self.full_depth = full_depth
        self.max_depth = max_depth
        self.max_nodes = max_nodes  # schemas that fan out through $refs can demand huge values
        self._left = max_nodes

    def example(self, schema: Any) -> Any:
        """A value for `schema`. Raises SchemaTooLarge if a valid value would need more
        than `max_nodes` parts (e.g. required properties that fan out through $refs)."""
        self._left = self.max_nodes
        value = self._value(schema, 0, "", frozenset())
        return {} if value is _CYCLE else value

    # ------------------------------------------------------------------ internals

    def _deref(self, schema: Any, seen: frozenset[str]) -> tuple[Any, frozenset[str]]:
        while isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
            ref = schema["$ref"]
            if not ref.startswith(DEFS_PREFIX):
                return {}, seen
            if ref in seen:
                return _CYCLE, seen
            seen = seen | {ref}
            parts = [p.replace("~1", "/").replace("~0", "~")
                     for p in ref[len(DEFS_PREFIX):].split("/")]
            node: Any = self.defs.get(parts[0])
            for p in parts[1:]:
                node = node.get(p) if isinstance(node, dict) else None
            siblings = {k: v for k, v in schema.items() if k != "$ref"}
            schema = {**(node if isinstance(node, dict) else {}), **siblings}
        return schema, seen

    def _value(self, schema: Any, depth: int, name: str, seen: frozenset[str]) -> Any:
        self._left -= 1
        if self._left < 0:
            from ..limits import SchemaTooLarge

            raise SchemaTooLarge(f"an example value would need more than {self.max_nodes:,} parts")
        schema, seen = self._deref(schema, seen)
        if schema is _CYCLE:
            return _CYCLE
        if not isinstance(schema, dict):
            return "example"
        if "const" in schema:
            return schema["const"]
        if isinstance(schema.get("enum"), list) and schema["enum"]:
            fitting = [v for v in schema["enum"] if v is not None and _fits(v, schema)]
            if fitting:
                return fitting[0]
            if schema.get("type") == "array":  # enum misplaced on the array instead of items
                return [schema["enum"][0]]
        if "default" in schema and schema["default"] is not None and _fits(schema["default"], schema):
            return schema["default"]
        if isinstance(schema.get("examples"), list) and schema["examples"] and _fits(schema["examples"][0], schema):
            return schema["examples"][0]

        for key in ("oneOf", "anyOf"):
            branches = schema.get(key)
            if isinstance(branches, list) and branches:
                rest = {k: v for k, v in schema.items() if k != key}
                choice, branch_seen = self._deref(self._pick_branch(branches, seen), seen)
                if choice is _CYCLE:
                    return _CYCLE
                if not isinstance(choice, dict):
                    return self._value(rest, depth, name, seen) if rest else "example"
                merged = {**rest, **choice}
                if rest.get("properties") or rest.get("required"):
                    merged["properties"] = {**(rest.get("properties") or {}),
                                            **(choice.get("properties") or {})}
                    merged["required"] = list(dict.fromkeys(
                        [*(rest.get("required") or []), *(choice.get("required") or [])]))
                return self._value(merged, depth, name, branch_seen)

        if isinstance(schema.get("allOf"), list):
            merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
            props: dict[str, Any] = dict(merged.get("properties") or {})
            required: list[str] = list(merged.get("required") or [])
            for sub in schema["allOf"]:
                sub, _ = self._deref(sub, seen)
                if isinstance(sub, dict):  # (skips _CYCLE)
                    props.update(sub.get("properties") or {})
                    required += sub.get("required") or []
                    for k, v in sub.items():
                        if k not in ("properties", "required"):
                            merged.setdefault(k, v)
            merged["properties"], merged["required"] = props, required
            merged.setdefault("type", "object")
            return self._value(merged, depth, name, seen)

        stype = schema.get("type")
        if isinstance(stype, list):
            stype = next((t for t in stype if t != "null"), "null")
        if stype is None:
            if "properties" in schema or "additionalProperties" in schema:
                stype = "object"
            elif "items" in schema:
                stype = "array"
            else:
                return "example"

        if stype == "null":
            return None
        if stype == "boolean":
            return True
        if stype in ("integer", "number"):
            return self._number(schema, stype == "integer")
        if stype == "string":
            return self._string(schema, name)
        if stype == "array":
            if depth >= self.max_depth:
                return []
            count = max(1, int(schema.get("minItems", 1)))
            if "maxItems" in schema:
                count = min(count, int(schema["maxItems"]))
            item = self._value(schema.get("items", {}), depth + 1, name, seen)
            if item is _CYCLE:
                return []  # recursion: an empty list is always a valid way to stop
            if schema.get("uniqueItems") and count > 1:
                return [item]  # can't make distinct values in general; one is safest
            return [item] * count
        if stype == "object":
            return self._object(schema, depth, seen)
        return "example"

    def _pick_branch(self, branches: list[Any], seen: frozenset[str]) -> Any:
        """Prefer a non-null branch; among those, the simplest (Stripe's expandable
        fields are anyOf [id-string, full object]; the string keeps examples small)."""
        candidates = []
        for b in branches:
            resolved, _ = self._deref(b, seen)
            if isinstance(resolved, dict) and resolved.get("type") == "null":
                continue
            candidates.append(b)
        if not candidates:
            return branches[0]
        scalar = [b for b in candidates
                  if isinstance(self._deref(b, seen)[0], dict)
                  and self._deref(b, seen)[0].get("type") in ("string", "integer", "number", "boolean")]
        return scalar[0] if scalar else candidates[0]

    def _object(self, schema: dict[str, Any], depth: int, seen: frozenset[str]) -> dict[str, Any]:
        props: dict[str, Any] = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        include_all = self.full and depth < self.full_depth and depth < self.max_depth
        out: dict[str, Any] = {}
        for key, sub in props.items():
            if key in required or include_all:
                if depth >= self.max_depth and key not in required:
                    continue
                value = self._value(sub, depth + 1, key, seen)
                if value is _CYCLE:
                    if key not in required:
                        continue
                    value = {}
                out[key] = value
        for key in required - out.keys():  # required but not described in properties
            out[key] = "example"
        min_props = int(schema.get("minProperties", 0))
        for key, sub in props.items():  # fill minProperties with declared props first
            if len(out) >= min_props:
                break
            if key not in out:
                value = self._value(sub, depth + 1, key, seen)
                if value is not _CYCLE:
                    out[key] = value
        extra = schema.get("additionalProperties")
        i = 0
        while len(out) < min_props and extra is not False:
            out[f"key{i}"] = self._value(extra if isinstance(extra, dict) else {}, depth + 1, "", seen)
            i += 1
        return out

    @staticmethod
    def _number(schema: dict[str, Any], integer: bool) -> int | float:
        lo = schema.get("minimum")
        hi = schema.get("maximum")
        ex_lo = schema.get("exclusiveMinimum")
        ex_hi = schema.get("exclusiveMaximum")
        if isinstance(ex_lo, (int, float)) and not isinstance(ex_lo, bool):
            lo = ex_lo + (1 if integer else 0.5)
        if isinstance(ex_hi, (int, float)) and not isinstance(ex_hi, bool):
            hi = ex_hi - (1 if integer else 0.5)
        value: float = 1 if lo is None else lo
        if hi is not None and value > hi:
            value = hi
        multiple = schema.get("multipleOf")
        if multiple:
            value = (int(value / multiple) or 1) * multiple
            if lo is not None and value < lo:
                value += multiple
        if integer:
            import math
            value = math.ceil(value) if lo is not None else int(value)
            return int(value)
        return float(value)

    @staticmethod
    def _string(schema: dict[str, Any], name: str) -> str:
        fmt = schema.get("format")
        value = _FORMAT_EXAMPLES.get(fmt) if fmt else None
        if value is None:
            value = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "example"
            if name.lower() in ("email", "email_address"):
                value = _FORMAT_EXAMPLES["email"]
            elif name.lower().endswith(("url", "uri")):
                value = _FORMAT_EXAMPLES["uri"]
        min_len = int(schema.get("minLength", 0))
        max_len = schema.get("maxLength")
        if len(value) < min_len:
            value = value + "x" * (min_len - len(value))
        if max_len is not None and len(value) > int(max_len):
            value = value[: int(max_len)] or ""
        pattern = schema.get("pattern")
        if pattern and not _search(pattern, value):
            generated = regex_example(pattern)
            if generated is not None:
                value = generated
        return value


_PY_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
             "array": list, "object": dict}


def _fits(value: Any, schema: dict[str, Any]) -> bool:
    """Does a spec-supplied default/example match the declared type (specs often get this wrong)?"""
    t = schema.get("type")
    types = t if isinstance(t, list) else ([t] if t else [])
    if not types:
        return True
    for name in types:
        if name == "null" and value is None:
            return True
        py = _PY_TYPES.get(name)
        if py and isinstance(value, py) and not (name in ("integer", "number") and isinstance(value, bool)):
            if isinstance(value, dict) and schema.get("additionalProperties") is False:
                return set(value) <= set(schema.get("properties") or {})
            return True
    return False


def _search(pattern: str, value: str) -> bool:
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return True  # not a Python-compatible regex; don't fight it


try:  # Python 3.11+
    from re import _constants as _C
    from re import _parser as _sre_parse
except ImportError:  # pragma: no cover
    import sre_constants as _C  # type: ignore[no-redef]
    import sre_parse as _sre_parse  # type: ignore[no-redef]


class _Unsupported(Exception):
    pass


def regex_example(pattern: str) -> str | None:
    """A short string matching `pattern`, for the regex subset APIs actually use:
    literals, classes, ranges, quantifiers, groups, alternation, anchors."""
    try:
        parsed = _sre_parse.parse(pattern)
        value = _gen(parsed)
    except (re.error, _Unsupported, RecursionError, ValueError, TypeError):
        return None
    return value if _search(pattern, value) else None


def _gen(items: Any) -> str:
    out = []
    for op, av in items:
        if op == _C.LITERAL:
            out.append(chr(av))
        elif op == _C.NOT_LITERAL:
            out.append("a" if chr(av) != "a" else "b")
        elif op == _C.ANY:
            out.append("a")
        elif op == _C.IN:
            out.append(_gen_class(av))
        elif op in (_C.MAX_REPEAT, _C.MIN_REPEAT, getattr(_C, "POSSESSIVE_REPEAT", None)):
            lo, hi, sub = av
            count = max(lo, min(1, hi))
            out.append("".join(_gen(sub) for _ in range(count)))
        elif op == _C.SUBPATTERN:
            out.append(_gen(av[-1]))
        elif op == getattr(_C, "ATOMIC_GROUP", None):
            out.append(_gen(av))
        elif op == _C.BRANCH:
            out.append(_gen(av[1][0]))
        elif op in (_C.AT, _C.ASSERT, _C.ASSERT_NOT):
            continue  # anchors / lookarounds: generate nothing, let the final check decide
        else:
            raise _Unsupported(str(op))
    return "".join(out)


_CATEGORY_CHARS = {
    "CATEGORY_DIGIT": "0", "CATEGORY_NOT_DIGIT": "a", "CATEGORY_WORD": "a",
    "CATEGORY_NOT_WORD": "-", "CATEGORY_SPACE": " ", "CATEGORY_NOT_SPACE": "a",
}


def _gen_class(items: Any) -> str:
    if items and items[0][0] == _C.NEGATE:
        members = items[1:]
        for ch in "aA0_-x.":
            if not any(_class_matches(op, av, ch) for op, av in members):
                return ch
        raise _Unsupported("negated class")
    for op, av in items:
        if op == _C.LITERAL:
            return chr(av)
        if op == _C.RANGE:
            return chr(av[0])
        if op == _C.CATEGORY:
            return _CATEGORY_CHARS.get(str(av), "a")
    raise _Unsupported("class")


def _class_matches(op: Any, av: Any, ch: str) -> bool:
    if op == _C.LITERAL:
        return chr(av) == ch
    if op == _C.RANGE:
        return av[0] <= ord(ch) <= av[1]
    if op == _C.CATEGORY:
        name = str(av)
        tests = {"CATEGORY_DIGIT": ch.isdigit(), "CATEGORY_WORD": ch.isalnum() or ch == "_",
                 "CATEGORY_SPACE": ch.isspace()}
        base = name.replace("NOT_", "")
        hit = tests.get(base, False)
        return not hit if "NOT_" in name else hit
    return False
