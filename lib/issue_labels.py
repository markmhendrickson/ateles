"""
lib/issue_labels.py — Frozen GitHub issue label prefixes for the Ateles swarm.

Label conventions are shared between Formica (general GitHub automation),
neotoma-agent (neotoma-repo automation), and any GHA workflows. Centralising
them here prevents drift between repos and makes label-based routing logic
importable by any daemon.

Label format: <prefix>/<value>  e.g.  type/bug, priority/p1, agent/formica

These are frozen — changes require a deliberate update here plus a migration
of existing labels in the affected repos.
"""

from __future__ import annotations

from enum import Enum


class TypeLabel(str, Enum):
    """Issue/PR type classification."""

    BUG = "type/bug"
    FEATURE = "type/feature"
    DOCS = "type/docs"
    REFACTOR = "type/refactor"
    CHORE = "type/chore"
    QUESTION = "type/question"


class PriorityLabel(str, Enum):
    """Issue priority, aligned with Neotoma Priority enum."""

    P0 = "priority/p0"  # critical / production down
    P1 = "priority/p1"  # blocker
    P2 = "priority/p2"  # operator decision required
    P3 = "priority/p3"  # normal / info


class StatusLabel(str, Enum):
    """Agent-managed workflow state."""

    NEEDS_TRIAGE = "status/needs-triage"
    IN_PROGRESS = "status/in-progress"
    AWAITING_INPUT = "status/awaiting-input"
    STALE = "status/stale"
    WONT_FIX = "status/wont-fix"


class AgentLabel(str, Enum):
    """Which agent last acted on this issue."""

    FORMICA = "agent/formica"
    NEOTOMA_AGENT = "agent/neotoma-agent"
    LOXIA = "agent/loxia"
    GHA = "agent/gha"


# Convenience: all label values as a flat set, for validation
ALL_LABELS: frozenset[str] = frozenset(
    label.value
    for cls in (TypeLabel, PriorityLabel, StatusLabel, AgentLabel)
    for label in cls
)


def is_known_label(label: str) -> bool:
    """Return True if label is a known Ateles swarm label."""
    return label in ALL_LABELS


def priority_from_label(label: str) -> str | None:
    """
    Map a PriorityLabel value back to a Neotoma Priority string.
    Returns None if not a priority label.
    """
    mapping = {
        PriorityLabel.P0: "critical",
        PriorityLabel.P1: "blocker",
        PriorityLabel.P2: "operator_decision",
        PriorityLabel.P3: "info",
    }
    try:
        pl = PriorityLabel(label)
        return mapping[pl]
    except ValueError:
        return None
