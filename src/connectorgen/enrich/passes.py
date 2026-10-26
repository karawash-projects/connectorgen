"""Enrichment passes: the model proposes, contract tests decide.

Each pass:
1. picks targets the deterministic parser couldn't settle (or left bare),
2. asks the model for a structured proposal,
3. validates it (names exist, values well-formed, confidence not low),
4. applies it to a copy and runs the action's contract test,
5. keeps it only if the test passes, recording provenance either way.

Spec-provided values are never overwritten: the model fills gaps, it doesn't
second-guess the spec. Renames are limited to names the parser invented.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..ir import Action, AuthScheme, Connector, Pagination, pagination_problem
from ..llm.base import LLM, LLMError, enum, strict_object, text
from ..validate.contract import check_action
from .context import UNTRUSTED_NOTICE, clip, compact_schema
from .report import Change, EnrichmentReport

PASSES = ("pagination", "auth", "describe")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_PAGINATION_NOTE = re.compile(r"paginat|cursor|link-header", re.I)
_AUTH_HINT = re.compile(r"(api[ _-]?key|access token|bearer|oauth|authoriz|personal token)", re.I)

# ------------------------------------------------------------------ shared schemas
# Empty string means "not applicable" (cheaper for the model than nullable unions).

PAGINATION_FIELDS = {
    "style": enum(["none", "cursor", "offset", "page", "link_header"]),
    "request_param": text("query param carrying the cursor/offset/page number, or ''"),
    "limit_param": text("query param for page size, or ''"),
    "items_path": text("dotted path to the item array in the response body; '' if the body "
                       "itself is the array"),
    "response_cursor_path": text("dotted path to the next cursor (or next-page URL) in the "
                                 "body, or ''"),
    "cursor_from_item_field": text("if the next cursor is a field of the last item "
                                   "(e.g. Stripe 'starting_after' uses 'id'), that field; else ''"),
    "has_more_path": text("dotted path to a boolean 'more pages' flag, or ''"),
}

AUTH_SCHEME_SCHEMA = strict_object({
    "type": enum(["api_key", "bearer", "basic", "oauth2"]),
    "param_name": text("header/query/cookie name for api_key, else ''"),
    "location": enum(["header", "query", "cookie", ""]),
    "description": text("one short sentence on how to obtain/use the credential"),
})


def _blank(v: str | None) -> str | None:
    return v.strip() or None if isinstance(v, str) else None


def pagination_from_proposal(p: dict[str, Any]) -> Pagination:
    return Pagination(
        style=p["style"],
        request_param=_blank(p.get("request_param")),
        limit_param=_blank(p.get("limit_param")),
        items_path=_blank(p.get("items_path")),
        response_cursor_path=_blank(p.get("response_cursor_path")),
        cursor_from_item_field=_blank(p.get("cursor_from_item_field")),
        has_more_path=_blank(p.get("has_more_path")),
    )


def auth_from_proposal(items: Iterable[dict[str, Any]], prefix: str = "llm"
                       ) -> tuple[list[AuthScheme], list[str]]:
    schemes, problems = [], []
    for i, s in enumerate(items):
        if s["type"] == "api_key" and not (_blank(s.get("param_name")) and s.get("location")):
            problems.append("api_key scheme without param_name/location")
            continue
        schemes.append(AuthScheme(
            id=f"{prefix}_{s['type']}" + (f"_{i}" if i else ""),
            type=s["type"],
            param_name=_blank(s.get("param_name")) if s["type"] == "api_key" else None,
            location=(s.get("location") or "header") if s["type"] == "api_key" else None,
            description=s.get("description", ""),
        ))
    return schemes, problems


# ------------------------------------------------------------------ entry point


def enrich(connector: Connector, llm: LLM, *, passes: Iterable[str] = PASSES,
           docs_text: str | None = None, rename_all: bool = False,
           batch_size: int = 25) -> tuple[Connector, EnrichmentReport]:
    """Return an enriched copy of `connector` and a report of every proposal."""
    unknown = set(passes) - set(PASSES)
    if unknown:
        raise ValueError(f"Unknown passes: {', '.join(sorted(unknown))}. Choose from {PASSES}.")
    c = connector.model_copy(deep=True)
    report = EnrichmentReport(usage=llm.usage)
    wanted = list(passes)
    # Order matters: auth and pagination change behaviour; describe is cosmetic, so last.
    for name in PASSES:
        if name not in wanted:
            continue
        try:
            if name == "pagination":
                _pagination_pass(c, llm, report)
            elif name == "auth":
                _auth_pass(c, llm, report, docs_text)
            elif name == "describe":
                _describe_pass(c, llm, report, rename_all, batch_size)
        except LLMError as e:
            report.errors.append(f"{name} pass stopped: {e}")
    return c, report


def _passes_contract(c: Connector, action: Action) -> tuple[bool, str]:
    result = check_action(c, action)
    if result.status == "fail":
        return False, "contract test failed: " + "; ".join(result.failures)
    return True, ""


# ------------------------------------------------------------------ pagination


_PAGINATION_SYSTEM = f"""You configure pagination for API connectors.
Given one API operation, decide how a client fetches every page of results.
- request_param and limit_param must be names from the listed query parameters.
- Paths are dotted paths into the JSON response body, e.g. "meta.next_cursor".
- Choose "link_header" when the next-page URL arrives in an HTTP Link header
  (GitHub-style APIs; usually mentioned in the description or API conventions).
- Choose "none" if the operation is not paginated.
- Use confidence "low" when the material doesn't really tell you.
{UNTRUSTED_NOTICE}"""

_PAGINATION_SCHEMA = strict_object({
    **PAGINATION_FIELDS,
    "confidence": enum(["low", "medium", "high"]),
    "reasoning": text("one or two sentences citing the evidence"),
})


def _pagination_targets(c: Connector) -> list[Action]:
    return [a for a in c.actions
            if a.method == "GET" and a.pagination.style in ("none", "link_header")
            and any(_PAGINATION_NOTE.search(n) for n in a.notes)]


def _pagination_pass(c: Connector, llm: LLM, report: EnrichmentReport) -> None:
    for act in _pagination_targets(c):
        query = [p for p in act.params if p.location == "query"]
        params = "\n".join(
            f"- {p.name} ({p.json_schema.get('type', 'any')}): {clip(p.description, 160)}"
            for p in query) or "(none)"
        prompt = f"""<spec>
API: {c.title}
Operation: {act.method} {act.path}
Summary: {clip(act.summary, 200)}
Description: {clip(act.description, 1500)}
Query parameters:
{params}
Response body schema (compact): {compact_schema(act.output_schema, c.schemas)}
</spec>
The deterministic parser noted: {'; '.join(act.notes)}"""
        proposal = llm.complete_json(system=_PAGINATION_SYSTEM, prompt=prompt,
                                     schema=_PAGINATION_SCHEMA, max_tokens=1500)
        new = pagination_from_proposal(proposal)
        before = act.pagination.model_dump(exclude_defaults=True)
        after = new.model_dump(exclude_defaults=True)
        change = Change("pagination", act.name, "pagination", before, after, False,
                        proposal.get("reasoning", ""))
        report.changes.append(change)

        names = {p.name for p in query}
        if proposal["confidence"] == "low":
            change.reason = "model confidence low: " + change.reason
            continue
        problem = pagination_problem(new, names)
        if problem:
            change.reason = problem
            continue

        old = act.pagination
        act.pagination = new
        ok, why = _passes_contract(c, act)
        if not ok:
            act.pagination = old
            change.reason = why
            continue
        change.accepted = True
        act.notes = [n for n in act.notes if not _PAGINATION_NOTE.search(n)]
        act.provenance["pagination"] = f"llm:{llm.model}"


# ------------------------------------------------------------------ auth


_AUTH_SYSTEM = f"""You identify how clients authenticate to an API.
Return every supported scheme, most common first. Use only what the material
supports; if it doesn't say, return no schemes with confidence "low".
For api_key give the exact header/query/cookie name. For a token sent as
"Authorization: Bearer <token>" use bearer. Quote the evidence briefly.
{UNTRUSTED_NOTICE}"""

_AUTH_SCHEMA = strict_object({
    "schemes": {"type": "array", "items": AUTH_SCHEME_SCHEMA},
    "confidence": enum(["low", "medium", "high"]),
    "evidence": text("short quotes or paraphrases from the material"),
})


def _auth_pass(c: Connector, llm: LLM, report: EnrichmentReport, docs_text: str | None) -> None:
    if c.auth:
        return
    hints: list[str] = []
    for a in c.actions:
        for chunk in (a.description, *(p.description for p in a.params)):
            m = _AUTH_HINT.search(chunk or "")
            if m:
                start = max(0, m.start() - 120)
                hints.append(clip(chunk[start:m.end() + 160], 300))
        for p in a.params:
            if p.location == "header" and _AUTH_HINT.search(p.name):
                hints.append(f"header parameter '{p.name}' on {a.method} {a.path}")
        if len(hints) >= 15:
            break
    prompt = f"""<spec>
API: {c.title}
Base URL: {c.base_url}
API description: {clip(c.description, 4000)}
Auth-related snippets from operations:
{chr(10).join('- ' + h for h in hints[:15]) or '(none found)'}
</spec>"""
    if docs_text:
        prompt += f"\n<docs>\n{clip(docs_text, 20000)}\n</docs>"
    proposal = llm.complete_json(system=_AUTH_SYSTEM, prompt=prompt, schema=_AUTH_SCHEMA,
                                 max_tokens=1500)
    schemes, problems = auth_from_proposal(proposal["schemes"])
    change = Change("auth", "connector", "auth", [],
                    [s.model_dump(exclude_defaults=True) for s in schemes], False,
                    proposal.get("evidence", ""))
    report.changes.append(change)
    if problems:
        change.reason = "; ".join(problems)
        return
    if proposal["confidence"] == "low" or not schemes:
        change.reason = "model confidence low or no scheme found: " + change.reason
        return
    c.auth = schemes
    sample = [a for a in c.actions if a.safe][:5] or c.actions[:5]
    for a in sample:
        ok, why = _passes_contract(c, a)
        if not ok:
            c.auth = []
            change.reason = f"{a.name}: {why}"
            return
    change.accepted = True
    c.provenance["auth"] = f"llm:{llm.model}"


# ------------------------------------------------------------------ names & descriptions


_DESCRIBE_SYSTEM = f"""You write names and descriptions for API actions shown to
no-code automation users, as steps in a workflow.
For each action return:
- new_name: snake_case verb_object, e.g. list_repo_issues, create_customer, get_invoice.
  Keep the current name if it's already clear. Use list_ for collections, get_ for one item.
- summary: at most 70 characters, sentence case, no trailing period, e.g. "Create a customer".
- description: one or two plain sentences on what it does and when to use it.
Return exactly one entry per input action, with `name` copied unchanged.
{UNTRUSTED_NOTICE}"""

_DESCRIBE_SCHEMA = strict_object({"actions": {"type": "array", "items": strict_object({
    "name": text("the current name, copied unchanged"),
    "new_name": text(),
    "summary": text(),
    "description": text(),
})}})


def _describe_targets(c: Connector, rename_all: bool) -> list[Action]:
    return [a for a in c.actions
            if rename_all or a.operation_id is None or not a.summary.strip()
            or not a.description.strip()]


def _describe_pass(c: Connector, llm: LLM, report: EnrichmentReport, rename_all: bool,
                   batch_size: int) -> None:
    targets = _describe_targets(c, rename_all)
    taken = {a.name for a in c.actions}
    for i in range(0, len(targets), batch_size):
        batch = {a.name: a for a in targets[i:i + batch_size]}
        listing = "\n".join(
            f"- name={a.name} | {a.method} {a.path} | summary={clip(a.summary, 120)!r} | "
            f"description={clip(a.description, 240)!r} | tags={','.join(a.tags)} | "
            f"params={','.join(p.name for p in a.params)}{' | has body' if a.body_schema else ''}"
            for a in batch.values())
        prompt = f"<spec>\nAPI: {c.title}\nActions:\n{listing}\n</spec>"
        proposal = llm.complete_json(system=_DESCRIBE_SYSTEM, prompt=prompt,
                                     schema=_DESCRIBE_SCHEMA,
                                     max_tokens=min(16000, 400 + 250 * len(batch)))
        for item in proposal["actions"]:
            act = batch.pop(item["name"], None)
            if act is None:
                report.errors.append(f"describe: model returned unknown action '{item['name']}'")
                continue
            _apply_description(c, act, item, llm, report, rename_all, taken)
        for missing in batch:
            report.errors.append(f"describe: no proposal returned for '{missing}'")


def _apply_description(c: Connector, act: Action, item: dict[str, Any], llm: LLM,
                       report: EnrichmentReport, rename_all: bool, taken: set[str]) -> None:
    source = f"llm:{llm.model}"
    summary = " ".join(item["summary"].split()).rstrip(".")
    if not act.summary.strip() and summary:
        if len(summary) > 100:
            report.changes.append(Change("describe", act.name, "summary", "", summary, False,
                                         "longer than 100 characters"))
        else:
            report.changes.append(Change("describe", act.name, "summary", "", summary, True))
            act.summary = summary
            act.provenance["summary"] = source
    description = " ".join(item["description"].split())
    if not act.description.strip() and description:
        report.changes.append(Change("describe", act.name, "description", "", description, True))
        act.description = description
        act.provenance["description"] = source

    new_name = item["new_name"].strip()
    if new_name == act.name or not (rename_all or act.operation_id is None):
        return
    change = Change("describe", act.name, "name", act.name, new_name, False)
    report.changes.append(change)
    if not NAME_RE.match(new_name):
        change.reason = "not a valid snake_case name"
        return
    if new_name in taken:
        change.reason = "name already used by another action"
        return
    old = act.name
    taken.discard(old)
    taken.add(new_name)
    act.name = new_name
    for t in c.triggers:
        if t.source_action == old:
            t.source_action = new_name
    act.provenance["name"] = f"{source} (was {old})"
    change.accepted = True
