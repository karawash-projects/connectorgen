"""Check a connector against its source, and propose an update for approval.

    proposal = check_for_drift(connector)            # re-reads the spec / MCP server / docs
    proposal.save("repair/")                         # proposal.json, candidate.json, summary.txt
    apply_proposal("repair/", "github.json")         # after a person approves

Verdicts:
- no_change          source identical (same hash), no live drift
- safe_update        source changed; only additive/info changes; contract tests no worse
- needs_review       breaking or safety-relevant changes, or new contract failures
- live_drift         source unchanged, but live responses no longer match it (from a
                     smoke report); nothing to regenerate, someone should look
- source_changed_unverified  docs changed, but regenerating from docs needs a model
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..errors import ConnectorError
from ..ir import Connector, hash_spec
from ..runtime.auth import Credentials
from ..validate.contract import run_contract_tests
from .diff import ConnectorDiff, DiffEntry, diff_connectors
from .merge import carry_over

Verdict = Literal["no_change", "safe_update", "needs_review", "live_drift", "live_fix",
                  "source_changed_unverified"]


@dataclass
class RepairProposal:
    connector: str
    source_kind: Literal["openapi", "mcp", "docs"]
    source: str | None
    verdict: Verdict
    current_hash: str
    new_hash: str
    created_at: float = field(default_factory=time.time)
    diff: ConnectorDiff = field(default_factory=ConnectorDiff)
    contract_before: dict[str, int] = field(default_factory=dict)
    contract_after: dict[str, int] = field(default_factory=dict)
    new_failures: list[str] = field(default_factory=list)
    live_findings: list[str] = field(default_factory=list)
    affected_used: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    current_file_sha: str | None = None
    live_fixes: dict[str, Any] = field(default_factory=dict)
    candidate: Connector | None = None

    @property
    def has_update(self) -> bool:
        return self.candidate is not None and self.verdict in ("safe_update", "needs_review", "live_fix")

    def to_text(self) -> str:
        head = {
            "no_change": "No change: the source is identical.",
            "safe_update": "Update available: additive changes only, contract tests no worse.",
            "needs_review": "Update needs review: breaking or safety-relevant changes.",
            "live_drift": "Source unchanged, but the live API no longer matches it.",
            "live_fix": "Source unchanged; proposed fixes so the connector matches the live API.",
            "source_changed_unverified": "Source docs changed; regenerate with a model to see how.",
        }[self.verdict]
        lines = [f"{self.connector} ({self.source_kind}): {head}"]
        if self.affected_used:
            lines.append(f"Affects actions in use: {', '.join(sorted(set(self.affected_used)))}")
        if not self.diff.empty:
            lines.append(self.diff.to_text(set(self.affected_used)))
        if self.contract_after:
            lines.append(f"Contract tests: before {_fmt(self.contract_before)}; "
                         f"after {_fmt(self.contract_after)}")
        if self.live_fixes.get("fixes") or self.live_fixes.get("skipped"):
            from .live import LiveFix, LiveFixReport
            rep = LiveFixReport([LiveFix(**f) for f in self.live_fixes.get("fixes", [])],
                                self.live_fixes.get("skipped", []))
            lines.append("Fixes from the live API:\n" + rep.to_text())
        lines += [f"  new contract failure: {f}" for f in self.new_failures]
        lines += [f"  live: {f}" for f in self.live_findings]
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)

    def save(self, directory: str | Path) -> Path:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        meta = {k: v for k, v in self.__dict__.items() if k not in ("diff", "candidate")}
        meta["diff"] = self.diff.to_dict()
        meta["has_candidate"] = self.candidate is not None
        (d / "proposal.json").write_text(json.dumps(meta, indent=2, default=str))
        if self.candidate is not None:
            self.candidate.save(d / "candidate.json")
        (d / "summary.txt").write_text(self.to_text() + "\n")
        return d

    @classmethod
    def load(cls, directory: str | Path) -> RepairProposal:
        d = Path(directory)
        meta = json.loads((d / "proposal.json").read_text())
        has_candidate = meta.pop("has_candidate", False)
        diff = ConnectorDiff.from_dict(meta.pop("diff", {}))
        candidate = Connector.load(d / "candidate.json") if has_candidate else None
        return cls(**meta, diff=diff, candidate=candidate)


def _fmt(c: dict[str, int]) -> str:
    return f"{c.get('passed', 0)} passed / {c.get('failed', 0)} failed / {c.get('skipped', 0)} skipped"


def file_sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_kind(connector: Connector) -> Literal["openapi", "mcp", "docs"]:
    if connector.mcp is not None:
        return "mcp"
    if str(connector.provenance.get("connector", "")).startswith("llm:"):
        return "docs"
    return "openapi"


def _load_smoke(smoke: Any) -> dict[str, Any] | None:
    if smoke is None:
        return None
    data = json.loads(Path(smoke).read_text()) if isinstance(smoke, (str, Path)) else smoke
    return data.to_dict() if hasattr(data, "to_dict") else data


def _live_findings(smoke: dict[str, Any] | None) -> list[tuple[str, str]]:
    if not smoke:
        return []
    out = []
    for r in smoke.get("results", []):
        if r.get("status") == "drift":
            out.append((r["action"], r["detail"]))
        elif r.get("status") == "http_error" and r.get("http_status") in (400, 422):
            out.append((r["action"], f"request rejected (HTTP {r['http_status']}): {r.get('detail', '')}"))
    return out


def _regenerate(current: Connector, kind: str, *, spec: Any, credentials: Credentials | None,
                env: dict[str, str] | None, llm: Any) -> tuple[Connector | None, str, list[str]]:
    """(candidate or None, new source hash, notes)."""
    if kind == "openapi":
        from ..ingest.openapi import connector_from_openapi, load_spec

        source = spec if spec is not None else current.source
        if source is None:
            raise ConnectorError("No source recorded for this connector; pass spec=... "
                                 "(path, URL or dict).")
        raw = source if isinstance(source, dict) else load_spec(source)
        new_hash = hash_spec(raw)
        if new_hash == current.spec_hash:
            return None, new_hash, []
        derive = any(t.provenance.get("trigger") == "derived" for t in current.triggers) or \
            not current.triggers
        candidate = connector_from_openapi(raw, name=current.name, derive_triggers=derive)
        if not isinstance(source, dict):
            candidate.source = str(source)
        return candidate, new_hash, []

    if kind == "mcp":
        from ..mcp.ingest import connector_from_mcp

        assert current.mcp is not None
        candidate = connector_from_mcp(current.mcp, name=current.name, auth=current.auth,
                                       credentials=credentials, env=env)
        if candidate.spec_hash == current.spec_hash:
            return None, candidate.spec_hash, []
        return candidate, candidate.spec_hash, []

    from ..ingest.docs import connector_from_docs, fetch_docs

    if spec is not None:  # explicit sources from the caller (may include local files)
        sources = [spec] if isinstance(spec, (str, Path)) else list(spec)
        sources = [part.strip() for x in sources for part in str(x).split(",") if part.strip()]
    else:
        # Sources recorded in the connector file are only trusted as URLs: a shared file
        # could otherwise name a local file (a private key, say) to be read and sent to the model.
        recorded = [s for s in (current.source or "").split(", ") if s]
        sources = [s for s in recorded if _plain_http_url(s)]
        if len(sources) < len(recorded):
            raise ConnectorError(
                "This docs connector was built from local files; pass them again explicitly "
                "(spec=[...] or --spec) so they're re-read on purpose.")
    if not sources:
        raise ConnectorError("No docs sources recorded for this connector.")
    pages = fetch_docs(sources)
    new_hash = hash_spec([p.text for p in pages])
    if new_hash == current.spec_hash:
        return None, new_hash, []
    if llm is None:
        return None, new_hash, ["Docs changed. Pass a model (llm=...) to regenerate and see the diff."]
    candidate, report = connector_from_docs(sources, llm, name=current.name,
                                            base_url=current.base_url or None)
    return candidate, new_hash, [f"docs: {d}" for d in report.dropped]


def _plain_http_url(value: str) -> bool:
    if not value.startswith(("http://", "https://")) or any(ch.isspace() or ch == "," for ch in value):
        return False
    try:
        import httpx

        return bool(httpx.URL(value).host)
    except Exception:  # noqa: BLE001
        return False


def check_for_drift(
    connector: Connector,
    *,
    spec: Any = None,
    credentials: Credentials | None = None,
    env: dict[str, str] | None = None,
    llm: Any = None,
    smoke_report: Any = None,
    used_actions: list[str] | None = None,
    current_path: str | Path | None = None,
    fix_live: bool = False,
    verify_live: bool = False,
    global_params: dict[str, Any] | None = None,
) -> RepairProposal:
    """Re-read the connector's source and build a proposal. Never modifies anything.

    fix_live: when the source is unchanged but the smoke report shows the live API
        disagreeing with it, propose fixes (repair/live.py). Request rejections need `llm`.
    verify_live: re-call the API (read-only) to confirm each live fix; uses `credentials`
        and `global_params` (the same --param values given to smoke)."""
    kind = source_kind(connector)
    candidate, new_hash, notes = _regenerate(connector, kind, spec=spec, credentials=credentials,
                                             env=env, llm=llm)
    used = set(used_actions or [])
    for name in list(used):  # accept aliases in the "used" list
        try:
            used.add(connector.action(name).name)
        except KeyError:
            notes.append(f"'{name}' (listed as used) isn't an action of this connector.")
    smoke = _load_smoke(smoke_report)
    live = _live_findings(smoke)

    proposal = RepairProposal(
        connector=connector.name, source_kind=kind,
        source=connector.source if kind != "mcp" else (connector.mcp.describe() if connector.mcp else None),
        verdict="no_change", current_hash=connector.spec_hash, new_hash=new_hash, notes=notes,
        live_findings=[f"{a}: {d}" for a, d in live],
        current_file_sha=file_sha(current_path) if current_path else None,
    )
    if candidate is None and fix_live and smoke and new_hash == connector.spec_hash:
        from .live import propose_live_fixes

        patched, fixes = propose_live_fixes(connector, smoke, llm=llm, verify_live=verify_live,
                                            credentials=credentials, env=env,
                                            global_params=global_params)
        proposal.live_fixes = fixes.to_dict()
        if fixes.accepted:
            diff = diff_connectors(connector, patched)
            for f in fixes.accepted:
                diff.add("review", f.action, f"live_fix_{f.kind}",
                         f"{f.kind} fix from the live API ({f.verified}): " + "; ".join(f.changes[:3])
                         + (f" (+{len(f.changes) - 3} more)" if len(f.changes) > 3 else ""))
            before, after = run_contract_tests(connector), run_contract_tests(patched)
            proposal.contract_before = {"passed": before.passed, "failed": before.failed,
                                        "skipped": before.skipped}
            proposal.contract_after = {"passed": after.passed, "failed": after.failed,
                                       "skipped": after.skipped}
            failed_before = {r.action for r in before.results if r.status == "fail"}
            proposal.new_failures = [r.action for r in after.results
                                     if r.status == "fail" and r.action not in failed_before]
            proposal.diff, proposal.candidate = diff, patched
            proposal.verdict = "needs_review" if proposal.new_failures else "live_fix"
            proposal.affected_used = list(dict.fromkeys(e.target for e in diff.affecting(used)))
            return proposal
    elif candidate is not None and fix_live and live:
        proposal.notes.append("The source changed too: apply this update, re-run smoke against "
                              "it, then fix any remaining live drift.")

    if candidate is None:
        if new_hash != connector.spec_hash:
            proposal.verdict = "source_changed_unverified"
        elif live:
            proposal.verdict = "live_drift"
            for action, detail in live:
                proposal.diff.entries.append(DiffEntry("review", action, "live_drift", detail))
        proposal.affected_used = list(dict.fromkeys(e.target for e in proposal.diff.affecting(used)))
        return proposal

    diff = diff_connectors(connector, candidate)
    merged = carry_over(connector, candidate, diff)
    for action, detail in live:
        diff.add("review", action, "live_drift", f"live response didn't match the old spec: {detail}")

    before = run_contract_tests(connector)
    after = run_contract_tests(merged)
    failed_before = {r.action for r in before.results if r.status == "fail"}
    rename_map = {new: old for old, new in diff.matched}
    proposal.new_failures = [r.action for r in after.results if r.status == "fail"
                             and rename_map.get(r.action, r.action) not in failed_before]
    proposal.contract_before = {"passed": before.passed, "failed": before.failed, "skipped": before.skipped}
    proposal.contract_after = {"passed": after.passed, "failed": after.failed, "skipped": after.skipped}
    proposal.diff = diff
    proposal.candidate = merged
    proposal.notes += merged.notes
    proposal.affected_used = list(dict.fromkeys(e.target for e in diff.affecting(used)))
    risky = diff.breaking or diff.by_severity("review") or proposal.new_failures
    proposal.verdict = "needs_review" if risky else "safe_update"
    return proposal


def apply_proposal(directory: str | Path, connector_path: str | Path, *,
                   force: bool = False) -> Path:
    """Replace the connector file with the proposal's candidate. Keeps a timestamped
    backup and refuses if the file changed since the proposal was made."""
    proposal = RepairProposal.load(directory)
    if proposal.candidate is None or not proposal.has_update:
        raise ConnectorError(f"Nothing to apply (verdict: {proposal.verdict}).")
    path = Path(connector_path)
    if proposal.current_file_sha and file_sha(path) != proposal.current_file_sha and not force:
        raise ConnectorError(f"{path} changed after this proposal was made; run the check again "
                             "(or force=True to overwrite those changes).")
    current = Connector.load(path)
    if current.name != proposal.connector:
        raise ConnectorError(f"Proposal is for '{proposal.connector}', not '{current.name}'.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    n = 2
    while backup.exists():  # two applies in the same second keep both backups
        backup = path.with_name(f"{path.name}.{stamp}-{n}.bak")
        n += 1
    shutil.copy2(path, backup)
    mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.fchmod(fd, mode)  # mkstemp creates 0600; keep the file's own permissions
    except (AttributeError, OSError):  # pragma: no cover - non-POSIX
        pass
    with os.fdopen(fd, "w") as f:
        f.write(proposal.candidate.model_dump_json(indent=2) + "\n")
    os.replace(tmp, path)
    return backup
