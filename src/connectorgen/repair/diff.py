"""Structural diff between two versions of a connector, classified by impact.

Severity:
- breaking: existing workflows can fail (action/param removed, new required input,
  type changed, output field removed, auth scheme removed, trigger removed)
- review:   safety or behavior changed in a way a person should look at
  (became destructive, base URL changed, nested schema changes)
- additive: new capability that can't break anything (new action, optional param,
  new output field)
- info:     internal changes the runtime absorbs (path moved, pagination changed,
  upstream rename covered by an alias)
"""

from __future__ import annotations

import contextvars
import difflib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..enrich.context import deref
from ..ir import Action, Connector

Severity = Literal["breaking", "review", "additive", "info"]
ORDER: dict[str, int] = {"breaking": 0, "review": 1, "additive": 2, "info": 3}
_NOISE = {"description", "title", "example", "examples", "externalDocs", "xml", "deprecated"}
# Expanding schemas for comparison is bounded per action: a spec whose $refs fan out
# (40 properties x 7 levels, say) would otherwise take all memory. Past the budget the
# comparison is cut short and the action is flagged for review instead.
EXPAND_BUDGET = 200_000
_budget: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("expand_budget", default=None)


@dataclass
class DiffEntry:
    severity: Severity
    target: str          # action name (current version), trigger name, or "connector"
    kind: str
    message: str
    before: Any = None
    after: Any = None


@dataclass
class ConnectorDiff:
    entries: list[DiffEntry] = field(default_factory=list)
    matched: list[tuple[str, str]] = field(default_factory=list)   # (old name, new name)
    renamed: list[tuple[str, str, str]] = field(default_factory=list)  # (old, new, why)

    def add(self, severity: Severity, target: str, kind: str, message: str,
            before: Any = None, after: Any = None) -> None:
        self.entries.append(DiffEntry(severity, target, kind, message, before, after))

    @property
    def empty(self) -> bool:
        return not self.entries

    def by_severity(self, severity: Severity) -> list[DiffEntry]:
        return [e for e in self.entries if e.severity == severity]

    @property
    def breaking(self) -> list[DiffEntry]:
        return self.by_severity("breaking")

    def affecting(self, used: set[str]) -> list[DiffEntry]:
        return [e for e in self.entries if e.target in used and e.severity in ("breaking", "review")]

    def counts(self) -> dict[str, int]:
        return {s: len(self.by_severity(s)) for s in ORDER}  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [asdict(e) for e in self.entries], "matched": self.matched,
                "renamed": self.renamed, "counts": self.counts()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectorDiff:
        return cls([DiffEntry(**e) for e in data.get("entries", [])],
                   [tuple(m) for m in data.get("matched", [])],  # type: ignore[misc]
                   [tuple(r) for r in data.get("renamed", [])])  # type: ignore[misc]

    def to_text(self, used: set[str] | None = None) -> str:
        if self.empty:
            return "No changes."
        lines = []
        for e in sorted(self.entries, key=lambda e: (ORDER[e.severity], e.target)):
            flag = "  <- used" if used and e.target in used and e.severity in ("breaking", "review") else ""
            lines.append(f"  {e.severity.upper():<8} {e.target}: {e.message}{flag}")
        c = self.counts()
        lines.append(f"\n{c['breaking']} breaking, {c['review']} to review, "
                     f"{c['additive']} additive, {c['info']} info")
        return "\n".join(lines)


# ------------------------------------------------------------------ matching


def identity(a: Action) -> str:
    if a.mcp_tool:
        return f"mcp:{a.mcp_tool}"
    if a.operation_id:
        return f"op:{a.operation_id}"
    return f"http:{a.method} {a.path}"


def _route(a: Action) -> str | None:
    if a.mcp_tool:
        return None
    import re
    return f"{a.method} {re.sub(r'{[^}]+}', '{}', a.path)}"


def match_actions(old: Connector, new: Connector) -> tuple[list[tuple[Action, Action, str]],
                                                           list[Action], list[Action]]:
    """(pairs with how they matched, removed, added)."""
    pairs: list[tuple[Action, Action, str]] = []
    rest_old = list(old.actions)
    rest_new = list(new.actions)

    def take(key_fn: Any, how: str) -> None:
        index: dict[Any, Action] = {}
        for a in rest_new:
            k = key_fn(a)
            if k is not None and k not in index:
                index[k] = a
        for a in list(rest_old):
            k = key_fn(a)
            b = index.pop(k, None) if k is not None else None
            if b is not None and b in rest_new:
                pairs.append((a, b, how))
                rest_old.remove(a)
                rest_new.remove(b)

    take(identity, "identity")
    take(_route, "same endpoint, renamed operation")
    take(lambda a: a.name, "same name")
    # MCP tools renamed upstream: identical arguments and a near-identical description
    for a in list(rest_old):
        if not a.mcp_tool:
            continue
        for b in rest_new:
            if b.mcp_tool and _canon(a.arguments_schema) == _canon(b.arguments_schema) and \
                    difflib.SequenceMatcher(None, a.description, b.description).ratio() >= 0.85:
                pairs.append((a, b, "same arguments and description, renamed tool"))
                rest_old.remove(a)
                rest_new.remove(b)
                break
    return pairs, rest_old, rest_new


# ------------------------------------------------------------------ schema shape


def _canon(schema: Any) -> str:
    return json.dumps(schema, sort_keys=True, default=str)


def _expand(schema: Any, defs: dict[str, Any], depth: int = 32, seen: frozenset[str] = frozenset()) -> Any:
    """Inline refs (depth-limited) and drop documentation-only keys, for comparison."""
    budget = _budget.get()
    if budget is not None:
        budget[0] -= 1
        if budget[0] < 0:
            return {"$truncated": True}
    s, seen, cycle = deref(schema, defs, seen)
    if cycle:
        return {"$recursive": cycle}
    if isinstance(s, list):
        return [_expand(x, defs, depth, seen) for x in s]
    if not isinstance(s, dict):
        return s
    if depth <= 0:
        return {"type": s.get("type")}
    out: dict[str, Any] = {}
    for k, v in s.items():
        if k in _NOISE or k.startswith("x-"):  # docs-only keys and vendor extensions
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {name: _expand(sub, defs, depth - 1, seen) for name, sub in v.items()}
        elif isinstance(v, (dict, list)) and k not in ("enum", "required", "const", "default"):
            out[k] = _expand(v, defs, depth - 1, seen)
        else:
            out[k] = v
    return out


def _shape(schema: Any, defs: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """Top-level properties (expanded) and required names."""
    budget = _budget.get()
    if budget is not None:
        budget[0] -= 1
        if budget[0] < 0:
            return {}, set()
    s, _, _ = deref(schema, defs)
    if not isinstance(s, dict):
        return {}, set()
    props = dict(s.get("properties") or {})
    required = set(s.get("required") or [])
    for sub in s.get("allOf") or []:
        p, r = _shape(sub, defs)
        props.update(p)
        required |= r
    return {k: _expand(v, defs) for k, v in props.items()}, required


_RANK: dict[str, int] = {"breaking": 0, "review": 1, "additive": 2, "info": 3}
# constraints that reject inputs when added (and accept more when removed)
_TIGHTENING = ("pattern", "format", "const", "multipleOf", "uniqueItems", "additionalProperties",
)
# value limits: True if raising the number accepts more inputs
_LIMITS = {"maxLength": True, "maximum": True, "maxItems": True, "exclusiveMaximum": True,
           "maxProperties": True, "minLength": False, "minimum": False, "minItems": False,
           "exclusiveMinimum": False, "minProperties": False}


def classify_nested(old: Any, new: Any, path: str) -> list[tuple[str, str]]:
    """Classify how an input schema changed below the top level, from the point of view
    of someone sending data: adding options is additive, taking them away is breaking."""
    if _canon(old) == _canon(new):
        return []
    if not (isinstance(old, dict) and isinstance(new, dict)):
        return [("review", f"{path} changed")]
    out: list[tuple[str, str]] = []
    to, tn = old.get("type"), new.get("type")
    if to is not None and tn is not None and to != tn:
        widened = isinstance(tn, list) and (to in tn if isinstance(to, str) else set(to) <= set(tn))
        return [("additive" if widened else "breaking", f"{path} type {to} -> {tn}")]
    if to is None and tn is not None:
        out.append(("breaking", f"{path} now must be {tn}"))
    elif to is not None and tn is None:
        out.append(("additive", f"{path} no longer restricted to {to}"))
    oe, ne = old.get("enum"), new.get("enum")
    if isinstance(oe, list) and isinstance(ne, list):
        removed = [v for v in oe if v not in ne]
        added = [v for v in ne if v not in oe]
        if removed:
            out.append(("breaking", f"{path} no longer accepts {removed[:5]}"))
        if added:
            out.append(("additive", f"{path} accepts new values {added[:5]}"))
    elif isinstance(ne, list) and oe is None:
        out.append(("breaking", f"{path} now restricted to {ne[:5]}"))
    elif isinstance(oe, list) and ne is None:
        out.append(("additive", f"{path} no longer restricted to fixed values"))
    op, np_ = old.get("properties") or {}, new.get("properties") or {}
    oreq, nreq = set(old.get("required") or []), set(new.get("required") or [])
    for name in sorted(op.keys() - np_.keys()):
        out.append(("breaking", f"{path}.{name} removed"))
    for name in sorted(np_.keys() - op.keys()):
        out.append(("breaking" if name in nreq else "additive",
                    f"{path}.{name} added{' (required)' if name in nreq else ''}"))
    for name in sorted(op.keys() & np_.keys()):
        if name in nreq and name not in oreq:
            out.append(("breaking", f"{path}.{name} now required"))
        out += classify_nested(op[name], np_[name], f"{path}.{name}")
    if "items" in old or "items" in new:
        out += classify_nested(old.get("items"), new.get("items"), f"{path}[]")
    for key in ("anyOf", "oneOf"):
        ob, nb = old.get(key) or [], new.get(key) or []
        if not ob and nb:
            out.append(("breaking", f"{path} must now match {key} ({len(nb)} alternatives)"))
        elif ob and not nb:
            out.append(("additive", f"{path} no longer has to match {key}"))
        elif ob or nb:
            oc, nc = {_canon(b) for b in ob}, {_canon(b) for b in nb}
            if oc - nc and nc - oc:
                out.append(("review", f"{path} alternatives changed"))
            elif oc - nc:
                out.append(("breaking", f"{path} accepts fewer alternatives"))
            elif nc - oc:
                out.append(("additive", f"{path} accepts more alternatives"))
    for key, looser_when_higher in _LIMITS.items():
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        if a is None or b is None:  # limit added (tighter) or removed (looser)
            looser = (b is None)
        elif not all(isinstance(x, (int, float)) for x in (a, b)):
            out.append(("review", f"{path} {key} {a!r} -> {b!r}"))
            continue
        else:
            looser = (b > a) == looser_when_higher
        out.append(("additive" if looser else "breaking",
                    f"{path} {key} {a} -> {b} ({'looser' if looser else 'tighter'})"))
    for key in _TIGHTENING:
        a, b = old.get(key), new.get(key)
        if a == b or (key == "additionalProperties" and isinstance(a, dict) and isinstance(b, dict)):
            continue
        tighter_added = (a is None or a is True) and b is not None and b is not True
        if key == "uniqueItems":
            tighter_added, loosened = bool(b) and not a, bool(a) and not b
        else:
            loosened = a is not None and a is not True and (b is None or b is True)
        if tighter_added:
            out.append(("breaking", f"{path} now has {key} {b!r}"))
        elif loosened:
            out.append(("additive", f"{path} {key} removed"))
        else:
            out.append(("review", f"{path} {key} {a!r} -> {b!r}"))
    if isinstance(old.get("additionalProperties"), dict) or isinstance(new.get("additionalProperties"), dict):
        out += classify_nested(old.get("additionalProperties"), new.get("additionalProperties"),
                               f"{path}{{*}}")
    structural = {"type", "enum", "properties", "required", "items", "anyOf", "oneOf", *_LIMITS,
                  *_TIGHTENING}
    other = [k for k in set(old) | set(new) if k not in structural and old.get(k) != new.get(k)]
    if other:
        out.append(("review", f"{path} constraints changed ({', '.join(sorted(other)[:4])})"))
    return out


def _type_of(expanded: Any) -> Any:
    return expanded.get("type") if isinstance(expanded, dict) else None


def _add_worst(diff: ConnectorDiff, target: str, kind: str, prefix: str,
               findings: list[tuple[str, str]]) -> None:
    """Record one entry per field at the severity of its worst finding."""
    if not findings:
        return
    worst = min(findings, key=lambda f: _RANK[f[0]])[0]
    shown = [m for sev, m in findings if sev == worst][:3]
    more = len([1 for sev, _ in findings if sev == worst]) - len(shown)
    diff.add(worst, target, kind,  # type: ignore[arg-type]
             prefix + "; ".join(shown) + (f" (+{more} more)" if more > 0 else ""))


_FIELD_KEYS = ("properties", "required", "allOf")


def _top_level(schema: Any, defs: dict[str, Any]) -> Any:
    """The schema's own constraints (type, additionalProperties, minProperties, oneOf, ...)
    without its fields, which are compared one by one. Constraints inside `allOf` members
    apply to the whole value too, so they're lifted up (or kept as a stripped allOf)."""
    s, _, _ = deref(schema, defs)
    if not isinstance(s, dict):
        return s
    top = {k: v for k, v in s.items() if k not in _FIELD_KEYS}
    leftover = []
    for member in s.get("allOf") or []:
        m = _top_level(member, defs)
        if not isinstance(m, dict):
            continue
        for k, v in m.items():
            if k == "allOf":
                leftover.extend(v)
            elif k not in top:
                top[k] = v
            elif top[k] != v:
                leftover.append({k: v})
    if leftover:
        top["allOf"] = leftover
    return _expand(top, defs)


def _compare_input_object(diff: ConnectorDiff, target: str, label: str, old: Any, new: Any,
                          old_defs: dict[str, Any], new_defs: dict[str, Any]) -> None:
    _add_worst(diff, target, f"{label}_changed", f"{label}: ",
               classify_nested(_top_level(old, old_defs), _top_level(new, new_defs), label))
    op, oreq = _shape(old, old_defs)
    np_, nreq = _shape(new, new_defs)
    for name in sorted(np_.keys() - op.keys()):
        if name in nreq:
            diff.add("breaking", target, f"{label}_field_added_required",
                     f"new required {label} field '{name}'")
        else:
            diff.add("additive", target, f"{label}_field_added", f"new optional {label} field '{name}'")
    for name in sorted(op.keys() - np_.keys()):
        diff.add("breaking", target, f"{label}_field_removed",
                 f"{label} field '{name}' removed (inputs that set it will be rejected)")
    for name in sorted(op.keys() & np_.keys()):
        if _type_of(op[name]) != _type_of(np_[name]) and _type_of(op[name]) is not None:
            diff.add("breaking", target, f"{label}_field_type_changed",
                     f"{label} field '{name}' type {_type_of(op[name])} -> {_type_of(np_[name])}",
                     _type_of(op[name]), _type_of(np_[name]))
        elif name in nreq and name not in oreq:
            diff.add("breaking", target, f"{label}_field_now_required",
                     f"{label} field '{name}' is now required")
        elif name in oreq and name not in nreq:
            diff.add("additive", target, f"{label}_field_now_optional",
                     f"{label} field '{name}' is now optional")
        elif _canon(op[name]) != _canon(np_[name]):
            _add_worst(diff, target, f"{label}_field_changed", f"{label} field '{name}': ",
                       classify_nested(op[name], np_[name], name))


def classify_output(old: Any, new: Any, path: str) -> list[tuple[str, str]]:
    """Classify a response schema change from the point of view of a step reading the
    data: losing a field or changing its type breaks readers; new values to handle need a
    look; new fields are additive; tighter constraints on what the API sends are harmless."""
    if _canon(old) == _canon(new):
        return []
    if not (isinstance(old, dict) and isinstance(new, dict)):
        return [("review", f"{path} changed")]
    out: list[tuple[str, str]] = []
    to, tn = old.get("type"), new.get("type")
    if to is not None and tn is not None and to != tn:
        old_set = set(to) if isinstance(to, list) else {to}
        new_set = set(tn) if isinstance(tn, list) else {tn}
        if new_set < old_set:
            out.append(("info", f"{path} is now only {tn}"))
        elif old_set < new_set:
            return [("review", f"{path} can now also be {'/'.join(sorted(new_set - old_set))}")]
        else:
            return [("breaking", f"{path} type {to} -> {tn}")]
    oe, ne = old.get("enum"), new.get("enum")
    if isinstance(ne, list) and isinstance(oe, list):
        added = [v for v in ne if v not in oe]
        if added:
            out.append(("review", f"{path} can now return {added[:5]}"))
    elif isinstance(oe, list) and ne is None:
        out.append(("review", f"{path} no longer limited to {oe[:5]}"))
    op, np_ = old.get("properties") or {}, new.get("properties") or {}
    for name in sorted(op.keys() - np_.keys()):
        out.append(("breaking", f"{path}.{name} removed"))
    for name in sorted(np_.keys() - op.keys()):
        out.append(("additive", f"{path}.{name} added"))
    oreq, nreq = set(old.get("required") or []), set(new.get("required") or [])
    for name in sorted(op.keys() & np_.keys()):
        if name in oreq and name not in nreq:
            out.append(("review", f"{path}.{name} may now be missing"))
        out += classify_output(op[name], np_[name], f"{path}.{name}")
    if "items" in old or "items" in new:
        out += classify_output(old.get("items"), new.get("items"), f"{path}[]")
    for key in ("anyOf", "oneOf"):
        if _canon(old.get(key)) != _canon(new.get(key)):
            out.append(("review", f"{path} alternatives changed"))
    if to is not None and tn is None:
        out.append(("review", f"{path} no longer declares a type ({to})"))
    shape_keys = ("not", "allOf", "if", "then", "else", "const", "$ref", "$truncated", "$recursive",
                  "additionalProperties", "prefixItems", "contains", "patternProperties")
    changed = [k for k in shape_keys if _canon(old.get(k)) != _canon(new.get(k))]
    if changed:
        out.append(("review", f"{path} shape changed ({', '.join(changed)})"))
    if not out:
        out.append(("info", f"{path} constraints changed"))
    return out


def _compare_output(diff: ConnectorDiff, target: str, old: Any, new: Any,
                    old_defs: dict[str, Any], new_defs: dict[str, Any]) -> None:
    if old is None and new is None:
        return
    if old is None:
        diff.add("additive", target, "output_documented", "response schema now documented")
        return
    if new is None:
        diff.add("review", target, "output_undocumented", "response schema no longer documented")
        return
    op, _ = _shape(old, old_defs)
    np_, _ = _shape(new, new_defs)
    if op or np_:  # objects: the response's own type and constraints, then each field
        _add_worst(diff, target, "output_changed", "response: ",
                   classify_output(_top_level(old, old_defs), _top_level(new, new_defs), "response"))
    if not op and not np_:  # arrays, scalars, free-form objects
        _add_worst(diff, target, "output_changed", "response: ",
                   classify_output(_expand(old, old_defs), _expand(new, new_defs), "response"))
        return
    for name in sorted(op.keys() - np_.keys()):
        diff.add("breaking", target, "output_field_removed",
                 f"response field '{name}' removed (steps reading it get nothing)")
    for name in sorted(np_.keys() - op.keys()):
        diff.add("additive", target, "output_field_added", f"new response field '{name}'")
    for name in sorted(op.keys() & np_.keys()):
        before, after = _type_of(op[name]), _type_of(np_[name])
        if before != after and before is not None and after is not None:
            old_set = set(before) if isinstance(before, list) else {before}
            new_set = set(after) if isinstance(after, list) else {after}
            if old_set < new_set:  # widened: everything that worked still does
                diff.add("review", target, "output_field_type_widened",
                         f"response field '{name}' can now also be "
                         f"{'/'.join(sorted(new_set - old_set))}")
                continue
            if not new_set < old_set:
                diff.add("breaking", target, "output_field_type_changed",
                         f"response field '{name}' type {before} -> {after}")
                continue
        _add_worst(diff, target, "output_field_changed", f"response field '{name}': ",
                   classify_output(op[name], np_[name], name))


def compare_actions(diff: ConnectorDiff, old: Action, new: Action, old_c: Connector,
                    new_c: Connector, how: str) -> None:
    budget = [EXPAND_BUDGET]
    token = _budget.set(budget)
    try:
        _compare_actions(diff, old, new, old_c, new_c, how)
    finally:
        _budget.reset(token)
    if budget[0] < 0:
        diff.add("review", old.name, "schema_too_large",
                 "schemas are too large to compare fully (refs that fan out); review by hand")


def _compare_actions(diff: ConnectorDiff, old: Action, new: Action, old_c: Connector,
                     new_c: Connector, how: str) -> None:
    t = old.name
    if old.name != new.name and new.name not in old.aliases:
        diff.add("info", t, "renamed_upstream",
                 f"upstream name is now '{new.name}' ({how}); the old name keeps working as an alias",
                 old.name, new.name)
        diff.renamed.append((old.name, new.name, how))
    if (old.method, old.path) != (new.method, new.path) and not old.mcp_tool:
        # Only placeholder names changing (/items/{id} -> /items/{itemId}) is bookkeeping;
        # any other move means existing workflows would call a different endpoint.
        same_route = old.method == new.method and \
            re.sub(r"{[^}]+}", "{}", old.path) == re.sub(r"{[^}]+}", "{}", new.path)
        if same_route:  # renamed placeholders are fine; reordered ones swap callers' values
            old_names = re.findall(r"{([^}]+)}", old.path)
            new_names = re.findall(r"{([^}]+)}", new.path)
            same_route = all(n == o or n not in old_names for o, n in zip(old_names, new_names))
        diff.add("info" if same_route else "review", t, "endpoint_moved",
                 f"{old.method} {old.path} -> {new.method} {new.path}")
    if old.mcp_tool and old.mcp_tool != new.mcp_tool:
        diff.add("info", t, "tool_renamed", f"MCP tool '{old.mcp_tool}' -> '{new.mcp_tool}'")

    if old.arguments_schema is not None or new.arguments_schema is not None:
        _compare_input_object(diff, t, "argument", old.arguments_schema or {}, new.arguments_schema or {},
                              old_c.schemas, new_c.schemas)
    else:
        oparams = {p.name: p for p in old.params}
        nparams = {p.name: p for p in new.params}
        for name in sorted(nparams.keys() - oparams.keys()):
            p = nparams[name]
            if p.required:
                diff.add("breaking", t, "param_added_required", f"new required {p.location} param '{name}'")
            else:
                diff.add("additive", t, "param_added", f"new optional {p.location} param '{name}'")
        for name in sorted(oparams.keys() - nparams.keys()):
            diff.add("breaking", t, "param_removed",
                     f"param '{name}' removed (inputs that set it will be rejected)")
        for name in sorted(oparams.keys() & nparams.keys()):
            a, b = oparams[name], nparams[name]
            if b.required and not a.required:
                diff.add("breaking", t, "param_now_required", f"param '{name}' is now required")
            elif a.required and not b.required:
                diff.add("additive", t, "param_now_optional", f"param '{name}' is now optional")
            _add_worst(diff, t, "param_changed", f"param '{name}': ",
                       classify_nested(_expand(a.json_schema, old_c.schemas),
                                       _expand(b.json_schema, new_c.schemas), name))
            if (a.style, a.explode) != (b.style, b.explode):
                diff.add("review", t, "param_serialization_changed",
                         f"param '{name}' serialization {a.style}/{a.explode} -> {b.style}/{b.explode}")
            if a.location != b.location:
                diff.add("review" if "header" in (a.location, b.location) else "info", t, "param_moved",
                         f"param '{name}' moved {a.location} -> {b.location}")

        if old.body_schema is None and new.body_schema is not None:
            sev: Severity = "breaking" if new.body_required else "additive"
            diff.add(sev, t, "body_added", "request body added" + (" (required)" if new.body_required else ""))
        elif old.body_schema is not None and new.body_schema is None:
            diff.add("breaking", t, "body_removed", "request body removed")
        elif old.body_schema is not None and new.body_schema is not None:
            if new.body_required and not old.body_required:
                diff.add("breaking", t, "body_now_required", "request body is now required")
            _compare_input_object(diff, t, "body", old.body_schema, new.body_schema,
                                  old_c.schemas, new_c.schemas)
            if old.body_encoding != new.body_encoding:
                diff.add("info", t, "body_encoding_changed",
                         f"body encoding {old.body_encoding} -> {new.body_encoding}")

    _compare_output(diff, t, old.output_schema, new.output_schema, old_c.schemas, new_c.schemas)

    if new.destructive and not old.destructive:
        diff.add("review", t, "became_destructive", "now flagged destructive")
    elif old.safe and not new.safe:
        diff.add("review", t, "no_longer_read_only", "no longer read-only")
    elif new.safe and not old.safe:
        diff.add("info", t, "now_read_only", "now read-only")
    # None = not recorded (e.g. a connector from an older version): unknown, not a change
    if old.security is not None and old.security != new.security:
        diff.add("review", t, "security_changed",
                 f"accepted auth {old.security if old.security is not None else 'any'} -> "
                 f"{new.security if new.security is not None else 'any'}")
    if old.pagination != new.pagination:
        diff.add("info", t, "pagination_changed",
                 f"pagination {old.pagination.style} -> {new.pagination.style}")


def spec_base_url(c: Connector) -> str:
    """The server URL the source spec declares (a --base-url override is the user's own
    choice, kept by merge, so it isn't upstream drift)."""
    return c.provenance.get("spec_base_url") or c.base_url


def _describe_scheme(s: Any) -> str:
    return f"{s.type}{' ' + s.param_name if s.param_name else ''} ({s.id})"


def _compare_auth(diff: ConnectorDiff, old: list[Any], new: list[Any]) -> None:
    """Anything that changes where or how credentials are sent is safety-relevant: a changed
    token URL or key location in an upstream spec could redirect secrets, so it's 'review'."""
    rest_new = list(new)
    pairs = []
    for s in old:
        match = next((n for n in rest_new if n.id == s.id), None) or \
            next((n for n in rest_new if (n.type, n.param_name) == (s.type, s.param_name)), None)
        if match is None:
            diff.add("breaking", "connector", "auth_removed", f"auth scheme {_describe_scheme(s)} removed")
        else:
            rest_new.remove(match)
            pairs.append((s, match))
    for n in rest_new:
        diff.add("review", "connector", "auth_added",
                 f"auth scheme {_describe_scheme(n)} added (check where it sends credentials)")
    for a, b in pairs:
        changes = []
        for field_name in ("type", "param_name", "location"):
            if getattr(a, field_name) != getattr(b, field_name):
                changes.append(f"{field_name} {getattr(a, field_name)} -> {getattr(b, field_name)}")
        old_flows = {f.flow: f for f in a.oauth_flows}
        new_flows = {f.flow: f for f in b.oauth_flows}
        for flow in sorted(old_flows.keys() | new_flows.keys()):
            fa, fb = old_flows.get(flow), new_flows.get(flow)
            if fa is None or fb is None:
                changes.append(f"{flow} flow {'added' if fa is None else 'removed'}")
                continue
            for url_field in ("authorization_url", "token_url", "refresh_url"):
                if getattr(fa, url_field) != getattr(fb, url_field):
                    changes.append(f"{flow} {url_field} {getattr(fa, url_field)} -> {getattr(fb, url_field)}")
            if set(fa.scopes) != set(fb.scopes):
                changes.append(f"{flow} scopes changed")
        if changes:
            diff.add("review", "connector", "auth_changed",
                     f"auth scheme {_describe_scheme(a)}: " + "; ".join(changes), a.model_dump(), b.model_dump())


def diff_connectors(old: Connector, new: Connector) -> ConnectorDiff:
    diff = ConnectorDiff()
    pairs, removed, added = match_actions(old, new)
    for a, b, how in pairs:
        diff.matched.append((a.name, b.name))
        compare_actions(diff, a, b, old, new, how)
    for a in removed:
        diff.add("breaking", a.name, "action_removed", "action no longer exists upstream")
    for b in added:
        diff.add("additive", b.name, "action_added",
                 f"new action ({b.mcp_tool and 'tool ' + b.mcp_tool or b.method + ' ' + b.path})")

    _compare_auth(diff, old.auth, new.auth)
    old_base, new_base = spec_base_url(old), spec_base_url(new)
    if old_base and new_base and old_base != new_base:
        diff.add("review", "connector", "base_url_changed",
                 f"spec's base URL {old_base} -> {new_base}", old_base, new_base)

    renames = {a.name: b.name for a, b, _ in pairs}
    old_t = {t.name: t for t in old.triggers}
    new_t = {t.name: t for t in new.triggers}
    new_by_source = {(t.kind, t.source_action, t.event): t for t in new.triggers}
    for name, t in old_t.items():
        match = new_t.get(name) or new_by_source.get(
            (t.kind, renames.get(t.source_action or "", t.source_action), t.event))
        if match is None:
            diff.add("breaking", name, "trigger_removed", "trigger no longer available")
        elif t.kind == "polling" and match.dedup_key != t.dedup_key:
            diff.add("review", name, "trigger_dedup_changed",
                     f"dedup key {t.dedup_key} -> {match.dedup_key} (items may be re-emitted once)")
    old_sources = {(t.kind, renames.get(t.source_action or "", t.source_action), t.event) for t in old.triggers}
    for name, t in new_t.items():
        if name not in old_t and (t.kind, t.source_action, t.event) not in old_sources:
            diff.add("additive", name, "trigger_added", f"new {t.kind} trigger")
    return diff
