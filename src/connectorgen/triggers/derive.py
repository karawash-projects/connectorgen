"""Derive polling triggers from a connector's list actions (deterministic, no model).

A list action becomes a "new X" trigger when its items have a stable identifier
to deduplicate on. If the action can sort by creation time, newest first, the
trigger asks for that order so polls can stop early.
"""

from __future__ import annotations

import re
from typing import Any

from ..enrich.context import deref
from ..ir import Action, Connector, Trigger

ID_FIELDS = ("id", "uuid", "guid", "key", "node_id", "sha", "number")
SORT_PARAMS = ("sort", "sort_by", "sortby", "order_by", "orderby")
DIRECTION_PARAMS = ("direction", "order", "sort_order", "sortorder", "sort_direction")
_PREFIXES = ("list_", "get_", "search_", "fetch_", "find_")


def _object_props(schema: Any, defs: dict[str, Any]) -> dict[str, Any]:
    schema, _, _ = deref(schema, defs)
    if not isinstance(schema, dict):
        return {}
    props = dict(schema.get("properties") or {})
    for sub in schema.get("allOf") or []:
        props.update(_object_props(sub, defs))
    return props


def item_schema(action: Action, defs: dict[str, Any]) -> Any | None:
    """Schema of one item in the action's list response, or None if it isn't a list."""
    out, _, _ = deref(action.output_schema, defs)
    if not isinstance(out, dict):
        return None
    path = action.pagination.items_path if action.pagination.style != "none" else None
    if path:
        node: Any = out
        for part in path.split("."):
            node = _object_props(node, defs).get(part)
            if node is None:
                return None
        node, _, _ = deref(node, defs)
        return node.get("items") if isinstance(node, dict) else None
    if out.get("type") == "array" or ("items" in out and "properties" not in out):
        return out.get("items")
    return None


def _ordering(action: Action) -> tuple[dict[str, Any], str]:
    query = {p.name.lower(): p for p in action.params if p.location == "query"}
    sort = next((query[n] for n in SORT_PARAMS if n in query), None)
    if sort is None:
        return {}, "unknown"
    values = [v for v in sort.json_schema.get("enum") or [] if isinstance(v, str)]
    desc_created = next((v for v in values if v.lstrip("-").startswith("created")
                         and v.startswith("-")), None)
    if desc_created:
        return {sort.name: desc_created}, "newest_first"
    created = next((v for v in values if v.startswith("created")), None)
    direction = next((query[n] for n in DIRECTION_PARAMS if n in query), None)
    if created and direction and "desc" in (direction.json_schema.get("enum") or []):
        return {sort.name: created, direction.name: "desc"}, "newest_first"
    return {}, "unknown"


def derive_polling_triggers(connector: Connector) -> list[Trigger]:
    triggers: list[Trigger] = []
    used = {t.name for t in connector.triggers}
    for act in connector.actions:
        if not act.safe or act.mcp_tool:  # MCP tools have no list/paging semantics
            continue
        item = item_schema(act, connector.schemas)
        if item is None:
            continue
        props = _object_props(item, connector.schemas)
        dedup = next((f for f in ID_FIELDS if f in props), None)
        if dedup is None:
            continue
        inputs, order = _ordering(act)
        base = act.name
        for prefix in _PREFIXES:
            if base.startswith(prefix):
                base = base[len(prefix):]
                break
        else:  # verb in the middle, e.g. GitHub's issues_list_for_repo -> issues_for_repo
            base = re.sub(r"(^|_)(list|search)(_|$)", r"\1", base, count=1).strip("_") or act.name
        name, n = f"new_{base}", 2
        while name in used:
            name, n = f"new_{base}_{n}", n + 1
        used.add(name)
        label = act.summary or act.name.replace("_", " ")
        triggers.append(Trigger(
            name=name,
            kind="polling",
            description=f"Fires for each new item returned by “{label}”.",
            source_action=act.name,
            dedup_key=dedup,
            inputs=inputs,
            order=order,
            provenance={"trigger": "derived"},
        ))
    return triggers
