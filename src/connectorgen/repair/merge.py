"""Carry local customizations from the current connector onto a regenerated one.

The regenerated connector is the source of truth for what the API *is*. From the
current connector we keep what was added *locally*:
- action names (published names are stable; the new upstream name becomes an alias)
- aliases, and summaries/descriptions/pagination filled by enrichment where the new
  spec still leaves them empty
- auth inferred by enrichment when the spec still declares none
- the base URL (often a local override) and the MCP launch config
- trigger names and their local config (webhook signing, delivery-id header, inputs),
  plus triggers added by hand whose source action still exists
- fixes learned from the live API (repair/live.py), re-applied where the new spec
  still needs them
"""

from __future__ import annotations

from ..ir import Connector
from .diff import ConnectorDiff
from .live import reapply_live_fixes, reapply_output

_PAGINATION_NOTE = ("paginat", "cursor", "link-header")


def carry_over(current: Connector, candidate: Connector, diff: ConnectorDiff) -> Connector:
    merged = candidate.model_copy(deep=True)
    notes: list[str] = []

    if current.base_url and current.base_url != merged.base_url:
        if merged.base_url:
            notes.append(f"Kept base URL {current.base_url} (spec says {merged.base_url}).")
            merged.provenance["spec_base_url"] = merged.base_url
        merged.base_url = current.base_url
    if current.mcp is not None:
        merged.mcp = current.mcp.model_copy(deep=True)
    if not merged.auth and current.auth:
        merged.auth = [s.model_copy(deep=True) for s in current.auth]
        if "auth" in current.provenance:
            merged.provenance["auth"] = current.provenance["auth"]

    old_by_name = {a.name: a for a in current.actions}
    new_by_name = {a.name: a for a in merged.actions}
    rename_map: dict[str, str] = {}  # candidate name -> final name
    taken = set(new_by_name)

    for old_name, new_name in diff.matched:
        old, new = old_by_name[old_name], new_by_name.get(new_name)
        if new is None:
            continue
        final = new.name
        if old.name != new.name and (old.name not in taken or old.name == new.name):
            taken.discard(new.name)
            taken.add(old.name)
            final = old.name
            new.aliases = sorted(set(old.aliases) | {new.name})
            new.name = old.name
        else:
            new.aliases = sorted(set(old.aliases) | set(new.aliases) | ({old.name} - {new.name}))
        rename_map[new_name] = final

        for field_name in ("summary", "description"):
            if not getattr(new, field_name).strip() and getattr(old, field_name).strip():
                setattr(new, field_name, getattr(old, field_name))
                if field_name in old.provenance:
                    new.provenance[field_name] = old.provenance[field_name]
        if "pagination" in old.provenance and new.pagination.style in ("none", "link_header") \
                and old.pagination != new.pagination:
            new.pagination = old.pagination.model_copy(deep=True)
            new.provenance["pagination"] = old.provenance["pagination"]
            new.notes = [n for n in new.notes if not any(k in n.lower() for k in _PAGINATION_NOTE)]
        if "name" in old.provenance:
            new.provenance["name"] = old.provenance["name"]
        notes += reapply_live_fixes(old, new)
        notes += reapply_output(merged, new)

    # triggers: point at final action names, keep old names and local config
    for t in merged.triggers:
        if t.source_action in rename_map:
            t.source_action = rename_map[t.source_action]
    by_source = {(t.kind, t.source_action, t.event): t for t in merged.triggers}
    by_name = {t.name: t for t in merged.triggers}
    final_actions = {a.name for a in merged.actions}
    for old_t in current.triggers:
        source = rename_map.get(old_t.source_action or "", old_t.source_action)
        target = by_name.get(old_t.name) or by_source.get((old_t.kind, source, old_t.event))
        if target is None:
            custom = old_t.provenance.get("trigger") not in ("derived", "openapi")
            if custom and (old_t.kind == "webhook" or source in final_actions):
                t = old_t.model_copy(deep=True)
                t.source_action = source
                merged.triggers.append(t)
            continue
        if target.name != old_t.name and old_t.name not in by_name:
            by_name.pop(target.name, None)
            target.name = old_t.name
            by_name[target.name] = target
        target.signature = old_t.signature or target.signature
        target.delivery_id_header = old_t.delivery_id_header or target.delivery_id_header
        if old_t.inputs and old_t.kind == "polling":
            action = next((a for a in merged.actions if a.name == target.source_action), None)
            names = {p.name for p in action.params} if action else set()
            target.inputs = {k: v for k, v in old_t.inputs.items() if k in names} or target.inputs

    merged.notes = list(dict.fromkeys(notes + [n for n in merged.notes if n not in notes]))
    return merged
