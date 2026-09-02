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


class PhaseLabel(str, Enum):
    """Which gate currently OWNS the issue (`current_owner`).

    Mutually exclusive — an issue carries at most one `phase/` label.
    """

    PM = "phase/pm"
    UX = "phase/ux"
    ARCH = "phase/arch"
    IMPL = "phase/impl"
    PR_REVIEW = "phase/pr-review"
    QA = "phase/qa"
    LEGAL = "phase/legal"
    RELEASE = "phase/release"


class GateLabel(str, Enum):
    """Cumulative progress trail: one per gate that is signed_off or waived.

    `waived` is deliberately NOT distinguished from `signed_off` in the label
    itself — the audit trail for *how* a gate cleared lives in the issue
    entity's `owner_history`, which is the source of truth. The label answers
    only "is this gate still holding the issue up?".
    """

    PM = "gate/pm-signed"
    UX = "gate/ux-signed"
    ARCH = "gate/arch-signed"
    IMPL = "gate/impl-signed"
    PR_REVIEW = "gate/pr-review-signed"
    QA = "gate/qa-signed"
    LEGAL = "gate/legal-signed"


# Single derived flag: at least one PRE-IMPL gate is still pending/blocked.
# Cheaper and more filterable than a blocked label per gate.
BLOCKED_GATES_LABEL = "blocked/gates"

# Gates that must clear before implementation may start.
#
# THIS IS NOT A GATE SEQUENCE. It is the last-resort set used only when the
# caller does not supply one, and it exists solely so this pure projection
# function stays callable without I/O.
#
# It used to be `("pm", "arch")` while the comment above it claimed it mirrored
# `swarm_dispatch.PRE_IMPL_GATES` — which was `("pm", "ux", "arch")`. Because
# `BLOCKED_GATES_LABEL` derives from this tuple, `blocked/gates` read CLEAR on a
# feature issue whose `ux` gate was still pending. Two copies of one sequence,
# silently disagreeing.
#
# Callers that know the issue now pass `pre_impl_gates` explicitly, resolved
# from the governing `workflow_definition` entity via
# `lib.daemon_runtime.workflow_resolver.resolve_pre_impl_gates`. The value below
# is deliberately EMPTY: with no gates named, no `blocked/gates` label is
# emitted, so an unsupplied sequence produces *no claim* about blocking rather
# than a confident wrong one. Under-labelling is visible and recoverable;
# clearing a block that is still live is what shipped code past an unsigned gate.
_UNSUPPLIED_PRE_IMPL_GATES: tuple[str, ...] = ()

# Back-compat alias. Prefer passing `pre_impl_gates` to `labels_for_gate_status`.
PRE_IMPL_GATE_NAMES: tuple[str, ...] = _UNSUPPLIED_PRE_IMPL_GATES

# Gate states that mean "this gate is no longer holding the issue up".
_CLEARED_STATES = frozenset({"signed_off", "waived"})

# Gate name -> its cumulative "signed" label.
_GATE_TO_LABEL: dict[str, GateLabel] = {
    "pm": GateLabel.PM,
    "ux": GateLabel.UX,
    "arch": GateLabel.ARCH,
    "impl": GateLabel.IMPL,
    "pr_review": GateLabel.PR_REVIEW,
    "qa": GateLabel.QA,
    "legal": GateLabel.LEGAL,
}

# current_owner -> its phase label. Keys match the gate names the swarm uses.
_OWNER_TO_PHASE: dict[str, PhaseLabel] = {
    "pm": PhaseLabel.PM,
    "ux": PhaseLabel.UX,
    "arch": PhaseLabel.ARCH,
    "impl": PhaseLabel.IMPL,
    "pr_review": PhaseLabel.PR_REVIEW,
    "qa": PhaseLabel.QA,
    "legal": PhaseLabel.LEGAL,
    "release": PhaseLabel.RELEASE,
}

# Agent name -> the gate it owns, so `current_owner` may name either the gate
# ("arch") or the agent that owns it ("waxwing"). The swarm writes both forms.
_AGENT_TO_GATE: dict[str, str] = {
    "pavo": "pm",
    "accipiter": "ux",
    "waxwing": "arch",
    "cicada": "impl",
    "vanellus": "pr_review",
    "phoenicurus": "qa",
    "buteo": "legal",
}

# Every prefix this module MANAGES. A reconciler removes stale labels bearing
# these prefixes and leaves all other labels (type/, priority/, lanius-triage,
# human-applied ones) untouched.
MANAGED_LABEL_PREFIXES: tuple[str, ...] = ("phase/", "gate/", "blocked/")


def labels_for_gate_status(
    gate_status: dict[str, str] | None,
    current_owner: str | None = None,
    pre_impl_gates: tuple[str, ...] | list[str] | None = None,
) -> set[str]:
    """Project a swarm issue's gate state onto the GitHub labels it should carry.

    Pure function — no I/O — so the projection is trivially unit-testable and
    the reconciler that calls it stays a thin transport shim.

    `gate_status` maps gate name -> state (`pending` | `signed_off` | `waived`
    | `blocked` | `not_required`). `not_required` deliberately produces NO
    label: absence is the signal, and emitting one per gate per state would put
    ~28 labels on an issue.

    `current_owner` may name a gate (`"arch"`) or the agent that owns it
    (`"waxwing"`); both resolve to the same `phase/` label.

    `pre_impl_gates` is the gate set that must clear before implementation, for
    THIS issue — resolve it from the governing `workflow_definition` with
    `lib.daemon_runtime.workflow_resolver.resolve_pre_impl_gates` rather than
    assuming one. It differs per workflow: `ateles|feature` declares
    `pm, ux, arch`; `ateles|bug` and `ateles|security` declare only `pm`.
    Omitting it emits no `blocked/gates` label at all (see
    `_UNSUPPLIED_PRE_IMPL_GATES`) — no claim beats a wrong one.

    Returns only the labels this module manages; callers union it with the
    issue's unmanaged labels.
    """
    desired: set[str] = set()
    status = gate_status or {}

    for gate, state in status.items():
        if not isinstance(state, str):
            continue
        if state.strip().lower() in _CLEARED_STATES:
            label = _GATE_TO_LABEL.get(gate.strip().lower())
            if label is not None:
                desired.add(label.value)

    # Blocked flag: any PRE-IMPL gate not yet cleared and not not_required.
    blocking_gates = (
        tuple(pre_impl_gates)
        if pre_impl_gates is not None
        else _UNSUPPLIED_PRE_IMPL_GATES
    )
    for gate in blocking_gates:
        state = str(status.get(gate, "")).strip().lower()
        if state and state not in _CLEARED_STATES and state != "not_required":
            desired.add(BLOCKED_GATES_LABEL)
            break

    if current_owner:
        owner = current_owner.strip().lower()
        gate_name = _AGENT_TO_GATE.get(owner, owner)
        phase = _OWNER_TO_PHASE.get(gate_name)
        if phase is not None:
            desired.add(phase.value)

    return desired


def is_managed_label(label: str) -> bool:
    """True if `label` is one this module owns (and may therefore remove)."""
    return any(label.startswith(p) for p in MANAGED_LABEL_PREFIXES)


def reconcile_labels(current: list[str] | set[str], desired: set[str]) -> set[str]:
    """The label set to PUT: `desired` plus every unmanaged current label.

    Managed labels absent from `desired` are dropped; unmanaged labels are
    always preserved, so this never clobbers `type/`, `priority/`,
    `lanius-triage`, or anything a human applied.
    """
    preserved = {lbl for lbl in current if not is_managed_label(lbl)}
    return preserved | desired


# Convenience: all label values as a flat set, for validation
ALL_LABELS: frozenset[str] = frozenset(
    [
        label.value
        for cls in (TypeLabel, PriorityLabel, StatusLabel, AgentLabel, PhaseLabel, GateLabel)
        for label in cls
    ]
    + [BLOCKED_GATES_LABEL]
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
