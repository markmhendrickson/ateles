"""
execution/daemons/apis/panel_check_gate.py — hold the lens panel until the PR
head's gating checks are green.

WHY THIS EXISTS
---------------
An audit of 469 lens-reviewed PRs found 47 where a lens approved a head that
already had a failed check, 23 where a check went red after approval, 13 that
needed a CI-only fix after four or more lenses had cleared a head, and 6 that
merged red. Every one of those spent a full panel's agent time on a head that
could not merge as it stood. Seating a lens on a head whose required checks are
red or still running is wasted work, and a lens verdict on a head that then
changes to fix CI is carried or re-run, which costs more.

This module is the ONE definition of "may a lens be seated on this head": it
holds unless the head's checks are green by the same reading the panel-gated
approval tool applies (``approve_pr_as_app.evaluate_checks``: required checks
judged by branch protection, re-runs judged by their newest run, a paginated and
fully-read check list). Both seating paths use it — the Apis review panel
(``swarm_dispatch.SwarmDispatcher._handle_pr`` and the single-lens re-dispatch)
and the bootstrap lens runner (``execution/scripts/harness_lens_runner.py``) — so
there is one reading of "green", and it is the one approval will apply anyway.

FAIL CLOSED
-----------
The safety meaning lives in ``CheckGate.green``. It is False for a failing
check, a pending check, a head with no check signal at all, an unreadable check
list, and any error while reading — an unreadable check state is a hold, never a
pass. ``CheckGate.green`` is True only when ``evaluate_checks`` returned green.

``approve_pr_as_app`` imports ``swarm_dispatch`` at module level, and
``swarm_dispatch`` imports this module, so the import of ``evaluate_checks``
is deferred to the call (the approval tool bootstraps its own ``sys.path``).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("panel_check_gate")

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "scripts")


@dataclass(frozen=True)
class CheckGate:
    """The verdict on one PR head: ``green`` is the only field that releases a hold."""

    head: str
    green: bool
    # Why the head is held; empty when green. Names the head and every failing
    # or pending check, so the log line alone says what to wait for.
    reason: str = ""
    failing: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    # Non-empty when the check state could not be read (as opposed to read and
    # found red or pending). Still a hold.
    unreadable: str = ""


def describe_gate(head: str, outcomes: list, all_green: bool) -> CheckGate:
    """Turn ``evaluate_checks`` output into a ``CheckGate``.

    Green iff ``all_green`` is True. A not-green result with no named failing or
    pending check (no check has reported, or the legacy commit status is not
    success) is still a hold and says so.
    """
    short = (head or "")[:9] or "<unknown head>"
    if all_green:
        return CheckGate(head=head, green=True)
    failing: list[str] = []
    pending: list[str] = []
    unreadable = ""
    for outcome in outcomes:
        if getattr(outcome, "passed", False) or getattr(outcome, "superseded", False):
            continue
        name = (getattr(outcome, "name", "") or "<unnamed check>").strip()
        state = str(getattr(outcome, "state", "") or "")
        label = f"{name} [{state}]" if state else name
        if state.startswith("unreadable"):
            unreadable = label
        elif state.startswith("pending"):
            pending.append(label)
        else:
            failing.append(label)
    parts: list[str] = []
    if failing:
        parts.append("failing checks: " + ", ".join(failing))
    if pending:
        parts.append("pending checks: " + ", ".join(pending))
    if unreadable:
        parts.append("check list unreadable: " + unreadable)
    if not parts:
        parts.append(
            "no check has reported green on this head (none have run yet, or the "
            "commit status is not success)"
        )
    return CheckGate(
        head=head,
        green=False,
        reason=f"head {short}: " + "; ".join(parts),
        failing=tuple(failing),
        pending=tuple(pending),
        unreadable=unreadable,
    )


def unreadable_gate(head: str, why: str) -> CheckGate:
    """A hold for a check state that could not be read at all."""
    short = (head or "")[:9] or "<unknown head>"
    return CheckGate(
        head=head,
        green=False,
        reason=f"head {short}: check state unreadable ({why}) — holding, not passing",
        unreadable=why,
    )


def _evaluate_checks():
    """``approve_pr_as_app.evaluate_checks``, imported on first use."""
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    import approve_pr_as_app  # noqa: PLC0415 — deferred: it imports swarm_dispatch

    return approve_pr_as_app.evaluate_checks


async def read_check_gate(repository: str, head_sha: str, base_ref: str) -> CheckGate:
    """Read *head_sha*'s checks and say whether a lens may be seated on it.

    Never raises: any failure to read is an unreadable hold. An empty
    *head_sha* is unreadable too (there is nothing to check).
    """
    if not head_sha:
        return unreadable_gate("", "PR head SHA is unknown")
    try:
        evaluate = _evaluate_checks()
        async with httpx.AsyncClient(timeout=30) as client:
            outcomes, all_green = await evaluate(
                client, repo=repository, head_sha=head_sha, base_ref=base_ref or ""
            )
    except Exception as exc:  # noqa: BLE001 — every failure is a hold
        log.warning(
            f"[panel_check_gate] {repository}@{head_sha[:9]}: could not read check "
            f"state: {type(exc).__name__}: {exc}"
        )
        return unreadable_gate(head_sha, f"{type(exc).__name__}: {exc}")
    return describe_gate(head_sha, list(outcomes or []), bool(all_green))
