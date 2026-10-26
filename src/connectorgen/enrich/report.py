from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..llm.base import Usage


@dataclass
class Change:
    pass_name: str
    target: str  # action name, or "connector"
    field: str
    before: Any
    after: Any
    accepted: bool
    reason: str = ""

    def line(self) -> str:
        mark = "+" if self.accepted else "x"
        def show(v: Any) -> str:
            s = v if isinstance(v, str) else json.dumps(v, default=str)
            return s if len(s) <= 90 else s[:90] + "…"
        out = f"  {mark} [{self.pass_name}] {self.target}.{self.field}: {show(self.after)}"
        if self.reason:
            out += f"\n      {'why' if self.accepted else 'rejected'}: {self.reason}"
        return out


@dataclass
class EnrichmentReport:
    changes: list[Change] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    @property
    def accepted(self) -> list[Change]:
        return [c for c in self.changes if c.accepted]

    @property
    def rejected(self) -> list[Change]:
        return [c for c in self.changes if not c.accepted]

    def to_text(self, verbose: bool = False) -> str:
        lines = []
        shown = self.changes if verbose else [c for c in self.changes
                                               if not c.accepted or c.field != "description"]
        lines += [c.line() for c in shown]
        lines += [f"  ! {e}" for e in self.errors]
        lines.append(f"\n{len(self.accepted)} changes accepted, {len(self.rejected)} rejected"
                     f"{f', {len(self.errors)} errors' if self.errors else ''}. {self.usage}")
        return "\n".join(lines)
