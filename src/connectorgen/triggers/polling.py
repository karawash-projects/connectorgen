"""Polling triggers: call a list action on a schedule, emit only unseen items.

Semantics:
- First run is a baseline: existing items are recorded, not emitted, so turning a
  trigger on doesn't replay a user's whole history (emit_on_first_run=True to opt in).
- Items are emitted oldest first.
- Dedup is by the trigger's `dedup_key` (e.g. "id"); without one, by a hash of the
  item, which means an edited item counts as new.
- With order="newest_first" the poll stops at the first already-seen item.
- With order="oldest_first", new items arrive at the end of the list, so each poll
  resumes from where the last one stopped (page, offset or cursor), re-reading a little
  for safety; the first-run baseline continues across polls until it reaches the end.
- With order="unknown" the whole list is read each time (up to max_pages / max_items).
  If it's longer than that, items past the limit can't be seen: the result says so
  (complete=False) and a warning is logged. Prefer a trigger with a sort order.
- poll() commits state immediately (at-most-once). For at-least-once delivery, call
  poll(commit=False), process the items, then commit(result).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..errors import ConnectorError
from ..ir import Trigger
from ..runtime.client import ConnectorClient, get_path
from ..runtime.retry import log

OFFSET_OVERLAP = 100  # items re-read when resuming an offset list (deletions shift items down)


class StateStore(Protocol):
    def load(self, key: str) -> dict[str, Any] | None: ...
    def save(self, key: str, state: dict[str, Any]) -> None: ...


class MemoryStateStore:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    def load(self, key: str) -> dict[str, Any] | None:
        state = self.data.get(key)
        return json.loads(json.dumps(state)) if state is not None else None

    def save(self, key: str, state: dict[str, Any]) -> None:
        self.data[key] = json.loads(json.dumps(state))


class FileStateStore:
    """One JSON file per trigger key, written atomically."""

    def __init__(self, directory: str | Path = ".connectorgen_state"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = hashlib.sha256(key.encode()).hexdigest()[:24]
        return self.directory / f"{safe}.json"

    def load(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def save(self, key: str, state: dict[str, Any]) -> None:
        path = self._path(key)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({**state, "key": key}, f)
        os.replace(tmp, path)


@dataclass
class PollResult:
    items: list[Any]
    baseline: bool  # first run: existing items recorded, not emitted
    fetched: int
    stopped_early: bool = False
    complete: bool = True  # False: the list was longer than the limits and wasn't fully read
    _state: dict[str, Any] = field(default_factory=dict, repr=False)


def item_key(item: Any, dedup_key: str | None) -> str:
    if dedup_key:
        value = get_path(item, dedup_key)
        if value is not None:
            return str(value)
    return "h:" + hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:32]


class Poller:
    def __init__(
        self,
        client: ConnectorClient,
        trigger: str | Trigger,
        store: StateStore | None = None,
        *,
        inputs: dict[str, Any] | None = None,
        key: str | None = None,
        max_items: int = 500,
        max_pages: int = 10,
        page_size: int | None = None,
        seen_limit: int = 5000,
        emit_on_first_run: bool = False,
    ):
        self.client = client
        connector = client.connector
        if isinstance(trigger, str):
            match = next((t for t in connector.triggers if t.name == trigger), None)
            if match is None:
                names = ", ".join(t.name for t in connector.triggers if t.kind == "polling")
                raise KeyError(f"No trigger named '{trigger}'. Polling triggers: {names or '(none)'}")
            trigger = match
        if trigger.kind != "polling" or not trigger.source_action:
            raise ConnectorError(f"Trigger '{trigger.name}' is not a polling trigger.")
        self.trigger = trigger
        self.action = connector.action(trigger.source_action)
        self.inputs = {**trigger.inputs, **(inputs or {})}
        self.store = store or MemoryStateStore()
        fingerprint = hashlib.sha256(json.dumps(self.inputs, sort_keys=True, default=str)
                                     .encode()).hexdigest()[:12]
        self.key = key or f"{connector.name}:{trigger.name}:{fingerprint}"
        self.max_items = max_items
        self.max_pages = max_pages
        self.page_size = page_size if self.action.pagination.limit_param else None
        self.seen_limit = seen_limit
        self.emit_on_first_run = emit_on_first_run
        pag = self.action.pagination
        self.resumable = trigger.order == "oldest_first" and (
            bool(pag.request_param) and pag.style in ("offset", "page", "cursor")
            or pag.style in ("link_header", "cursor"))
        if self.resumable and self.max_pages < 2:
            # Resuming needs the next page's position (a cursor) or a look past the last page
            # (to see the list end), so read at least two pages per poll.
            self.max_pages = 2

    def poll(self, *, commit: bool = True) -> PollResult:
        state = self.store.load(self.key) or {"seen": [], "initialized": False}
        seen = set(state["seen"])
        newest_first = self.trigger.order == "newest_first"
        inputs = dict(self.inputs)
        resume = self._valid_resume(state.get("resume")) if self.resumable else None
        start_url = None
        if resume:
            start_url = resume.pop("__url__", None)
            inputs.update(resume)

        fetched: dict[str, Any] = {}
        stopped_early = False
        count = 0
        stats: dict[str, Any] = {}
        for item in self.client.paginate(self.action, inputs, max_pages=self.max_pages,
                                         page_size=self.page_size, stats=stats,
                                         start_url=start_url):
            count += 1
            k = item_key(item, self.trigger.dedup_key)
            if newest_first and k in seen:
                stopped_early = True
                break
            fetched.setdefault(k, item)
            if count >= self.max_items and not self.resumable:
                break
            # Resumable pollers stop only at page boundaries (max_pages), since a resume
            # point mid-page can't be expressed and the rest of the page would be lost.
        exhausted = bool(stats.get("exhausted")) and (self.resumable or count < self.max_items)
        complete = stopped_early or exhausted

        new = [(k, item) for k, item in fetched.items() if k not in seen]
        if newest_first:
            new.reverse()  # emit oldest first
        baseline = not state["initialized"]
        emitted = [item for _, item in new] if (not baseline or self.emit_on_first_run) else []

        keys = state["seen"] + [k for k, _ in new]
        next_state: dict[str, Any] = {
            "seen": keys[-self.seen_limit:],
            # a resumable baseline stays open until it has read to the end of the list
            "initialized": True if not self.resumable else (state["initialized"] or exhausted),
            "last_poll": time.time(),
        }
        if self.resumable:
            next_state["resume"] = (self._resume_point(inputs, stats, count, exhausted)
                                    or ({"__url__": stats["url"]} if stats.get("url") and stats.get("pages", 0) > 1
                                        or start_url else None)
                                    or state.get("resume"))
            complete = True  # the next poll continues where this one stopped
        elif not complete:
            log.warning("%s: read %d items and the list goes on; newer items past that point "
                        "can't be seen without a sort order (raise max_items/max_pages, or use "
                        "a trigger that sorts newest first)", self.trigger.name, count)
        result = PollResult(emitted, baseline, count, stopped_early, complete, next_state)
        if commit:
            self.commit(result)
        return result

    def _valid_resume(self, resume: Any) -> dict[str, Any] | None:
        """The saved resume point, if it's one this poller could have written; a damaged or
        edited state file restarts from the beginning instead of failing or going elsewhere."""
        pag = self.action.pagination
        if not isinstance(resume, dict) or not resume:
            return None
        if set(resume) == {"__url__"} and isinstance(resume["__url__"], str):
            return dict(resume)
        if set(resume) != {pag.request_param}:
            log.warning("%s: ignoring an unexpected saved resume point", self.trigger.name)
            return None
        value = resume[pag.request_param]
        if pag.style in ("offset", "page"):
            if not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
                log.warning("%s: ignoring an invalid saved resume point", self.trigger.name)
                return None
        elif not isinstance(value, (str, int)):
            return None
        return dict(resume)

    def _resume_point(self, inputs: dict[str, Any], stats: dict[str, Any], count: int,
                      exhausted: bool) -> dict[str, Any] | None:
        """Where the next poll should start. While the end of the list hasn't been reached,
        always further along than this poll started (so a long list is eventually read to the
        end); once it has, a little before the end, since appended items show up there and
        deletions shift items towards the front."""
        pag = self.action.pagination
        rp = pag.request_param
        if not rp:
            return None
        if pag.style == "offset":
            start = int(inputs.get(rp) or 0)
            end = start + count
            if exhausted:
                return {rp: max(start, end - OFFSET_OVERLAP)}
            return {rp: max(start + 1, end - min(OFFSET_OVERLAP, count // 2))}
        if pag.style == "page":
            start = int(inputs.get(rp) or 1)
            pages = max(1, int(stats.get("pages") or 1))
            last = start + pages - 1
            if exhausted and stats.get("page_items") == 0 and last > start:
                last -= 1              # the list ended on an empty page: the tail is the page before
            if exhausted or pages >= 2:
                return {rp: last}      # re-read the last page: it overlaps, and progress is made
            return {rp: last + 1}      # one page per poll: move on
        params = stats.get("params")    # cursor: the cursor that fetched the last page read
        if params and params.get(rp) is not None:
            return {rp: params[rp]}
        return None

    def commit(self, result: PollResult) -> None:
        self.store.save(self.key, result._state)

    def reset(self) -> None:
        self.store.save(self.key, {"seen": [], "initialized": False})
