"""Repair loop: detect upstream changes, regenerate, keep local customizations,
classify the diff, re-test, and propose an update for a person to approve."""

from .check import RepairProposal, apply_proposal, check_for_drift, source_kind
from .diff import ConnectorDiff, DiffEntry, diff_connectors, match_actions
from .merge import carry_over

__all__ = [
    "ConnectorDiff",
    "DiffEntry",
    "RepairProposal",
    "apply_proposal",
    "carry_over",
    "check_for_drift",
    "diff_connectors",
    "match_actions",
    "source_kind",
]
