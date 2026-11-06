"""Describe what a live response looked like, without keeping its data.

`infer_shape` turns a response into a JSON Schema of types and field names only.
Values are never stored, with one narrow exception: `enum_findings` keeps short
code-like strings (e.g. "pending_review") that a response used where the spec's
enum didn't allow them, because those codes *are* the fix.
"""

from __future__ import annotations

import re
from typing import Any

from ..limits import BoundedValidator, SchemaTooLarge


_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,39}$")
_FIELD_NAME = re.compile(r"^[A-Za-z_$@][A-Za-z0-9_.$:-]{0,63}$")
_DATA_HINTS = re.compile(r"@.*\.|^[0-9a-f]{8}-[0-9a-f]{4}-|\d{4,}", re.I)
MAX_ITEMS = 25
MAX_FIELDS = 100  # more keys than this: a map of data, not a record with named fields


def data_like_key(key: str) -> bool:
    """A key that is data rather than a field name: an email, a UUID, a number, a date, a
    phrase. Observations keep such keys out (they can be personal data) and treat the
    object as a map instead."""
    return not _FIELD_NAME.match(key) or bool(_DATA_HINTS.search(key))


def _type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def infer_shape(value: Any, depth: int = 8) -> dict[str, Any]:
    t = _type(value)
    if t == "array":
        if depth <= 0 or not value:
            return {"type": "array"}
        items: dict[str, Any] | None = None
        for v in value[:MAX_ITEMS]:
            s = infer_shape(v, depth - 1)
            items = s if items is None else merge_shapes(items, s)
        return {"type": "array", "items": items or {}}
    if t == "object":
        if depth <= 0:
            return {"type": "object"}
        if len(value) > MAX_FIELDS:
            named: dict[str, Any] = {}
            mapped = list(value.values())
        else:
            named = {str(k): v for k, v in value.items() if not data_like_key(str(k))}
            mapped = [v for k, v in value.items() if data_like_key(str(k))]
        shape: dict[str, Any] = {
            "type": "object",
            "properties": {k: infer_shape(v, depth - 1) for k, v in named.items()},
            "required": sorted(named)}
        if mapped:  # keys not kept: describe the values only
            values: dict[str, Any] | None = None
            for v in mapped[:MAX_ITEMS]:
                s = infer_shape(v, depth - 1)
                values = s if values is None else merge_shapes(values, s)
            shape["additionalProperties"] = values or {}
        return shape
    return {"type": t}


def _types(shape: dict[str, Any]) -> list[str]:
    t = shape.get("type")
    return t if isinstance(t, list) else ([t] if t else [])


def merge_shapes(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Combine two observed shapes (e.g. two items of one array)."""
    ta, tb = _types(a), _types(b)
    if ta == tb == ["object"]:
        pa, pb = a.get("properties", {}), b.get("properties", {})
        props = {k: merge_shapes(pa[k], pb[k]) if k in pa and k in pb else (pa.get(k) or pb[k])
                 for k in list(pa) + [k for k in pb if k not in pa]}
        req = sorted(set(a.get("required", [])) & set(b.get("required", [])))
        merged: dict[str, Any] = {"type": "object", "properties": props, "required": req}
        extra = [s for s in (a.get("additionalProperties"), b.get("additionalProperties"))
                 if isinstance(s, dict)]
        if extra:
            merged["additionalProperties"] = extra[0] if len(extra) == 1 \
                else merge_shapes(extra[0], extra[1])
        return merged
    if ta == tb == ["array"]:
        if "items" in a and "items" in b:
            return {"type": "array", "items": merge_shapes(a["items"], b["items"])}
        return a if "items" in a else b
    complex_types = {"object", "array"}
    if (set(ta) | set(tb)) & complex_types and set(ta) != set(tb):
        # e.g. sometimes an object, sometimes null
        non_null = [s for s in (a, b) if _types(s) != ["null"]]
        if len(non_null) == 1:
            merged = dict(non_null[0])
            merged["type"] = sorted(set(_types(non_null[0])) | {"null"})
            return merged
        return {"anyOf": [a, b]}
    types = sorted(set(ta) | set(tb))
    return {"type": types[0] if len(types) == 1 else types}


def enum_findings(schema: dict[str, Any], data: Any) -> list[dict[str, Any]]:
    """Short code values the response used outside the spec's enums: [{path, value}]."""
    found: list[dict[str, Any]] = []
    try:
        errors = BoundedValidator(schema).errors(data)
    except SchemaTooLarge:
        return found
    for err in errors:
        candidates = [err, *(err.context or [])]
        for e in candidates:
            if e.validator == "enum" and isinstance(e.instance, str) and _CODE.match(e.instance):
                item = {"path": list(e.absolute_path), "value": e.instance}
                if item not in found:
                    found.append(item)
    return found[:20]
