"""
lib/daemon_runtime/checkpoint_posture.py — on_check_failure posture enforcement.

Implements ateles#350: fail-open vs fail-closed is a per-boundary decision
(``ExecutionPolicy.posture_for(boundary)``), not a single global default.

A checkpoint "check failure" here means the CHECK ITSELF errored — an
exception, timeout, unloadable policy/deps, or malformed response — never a
check that ran cleanly and returned a "blocked" verdict. Conflating those two
is the bug this module exists to prevent: `evaluate_with_posture` only ever
sees a `CheckFailure`, so callers must catch/raise distinctly from a normal
"blocked" return value.

Posture semantics:
    OPEN   → proceed, but ALWAYS log (never a silent degrade).
    CLOSED → block, file a checkpoint_brief, notify the operator, and wait
             for a verdict (never proceed, never a silent hang).

Reuses the existing gate primitives (`write_checkpoint_brief`,
`GateDecision`) rather than inventing a parallel escalation system, per the
arch gate's contract-first-ordering requirement on #350.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from .gating import (
    BlastRadius,
    CheckpointPosture,
    ExecutionPolicy,
    GateAction,
    GateDecision,
    write_checkpoint_brief,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

# Notify callback signature: (message, *, handler) -> None. Kept as a plain
# callable (not `lib.notify.Notifier`) so this module stays dependency-light
# and usable from any daemon without importing the Apprise-backed notifier.
NotifyFn = Callable[..., None]

# file_brief callback signature: (boundary, error, decision) -> Awaitable[str | None].
# Overrides the default `write_checkpoint_brief` (which expects a Neotoma
# `task` entity and is synchronous/blocking). Callers whose checkpoint world
# isn't keyed by a Neotoma task entity (e.g. a GitHub-event dispatcher keyed
# by `owner/repo#n`) supply their own async escalation here — this keeps the
# OPEN/CLOSED *decision* in one place (this function) without forcing every
# caller's checkpoint_brief onto one schema.
FileBriefFn = Callable[[str, str, "GateDecision"], Awaitable[str | None]]


class CheckFailure(Exception):
    """Raised by a checkpoint check when the check itself cannot complete
    (exception, timeout, unloadable policy/deps, malformed response) — NOT
    for a check that ran and legitimately returned "blocked".
    """


@dataclass
class PostureOutcome:
    boundary: str
    posture: CheckpointPosture
    proceeded: bool
    checkpoint_brief_id: str | None
    error: str


async def evaluate_with_posture(
    check: Callable[[], Awaitable[T]],
    *,
    boundary: str,
    policy: ExecutionPolicy,
    task_entity_id: str = "",
    title: str,
    handler: str,
    notify: NotifyFn | None = None,
    file_brief: FileBriefFn | None = None,
) -> T | PostureOutcome:
    """Run `check()`; on success return its result unchanged.

    On `CheckFailure` (or any exception surfaced from `check`), resolve this
    boundary's posture via `policy.posture_for(boundary)` and either:
      - OPEN:   log the failure and return a PostureOutcome(proceeded=True)
                so the caller's existing "proceed" path can continue.
      - CLOSED: file a blocking checkpoint_brief, notify the operator (if a
                `notify` callback is given), and return
                PostureOutcome(proceeded=False) — the caller MUST NOT
                proceed past this boundary on that result, even if the
                checkpoint_brief itself fails to persist (fail closed, not
                silent-hang: the notify call and the returned
                proceeded=False are the enforcement).

    `file_brief`, when given, REPLACES the default escalation (which calls
    `write_checkpoint_brief` and requires `task_entity_id` — a Neotoma
    `task` entity). Pass it when the caller's checkpoint world isn't keyed by
    a Neotoma task entity (e.g. a GitHub-event dispatcher keyed by
    `owner/repo#n`) so its checkpoint_brief has the right shape/links for its
    own resolution flow, while still routing the OPEN/CLOSED decision itself
    through this one function rather than a re-derived if-tree at the call
    site.

    Callers distinguish "proceed" from "blocked" by checking whether the
    return value is a `PostureOutcome` (posture path) vs. the check's own
    result type (happy path) — e.g.
    ``isinstance(result, PostureOutcome) and not result.proceeded``.
    """
    try:
        return await check()
    except Exception as exc:  # noqa: BLE001 — any check failure, not just CheckFailure
        posture = policy.posture_for(boundary)
        error = f"{type(exc).__name__}: {exc}"

        if posture == CheckpointPosture.OPEN:
            log.warning(
                "checkpoint check failed; proceeding because "
                f"on_check_failure=open (boundary={boundary}): {error}"
            )
            return PostureOutcome(
                boundary=boundary,
                posture=posture,
                proceeded=True,
                checkpoint_brief_id=None,
                error=error,
            )

        # CLOSED: block, escalate, never proceed.
        log.error(
            f"checkpoint check failed; blocking because on_check_failure=closed "
            f"(boundary={boundary}): {error}"
        )
        decision = GateDecision(
            action=GateAction.CHECKPOINT,
            blast_radius=BlastRadius.HIGH,
            confidence=0.0,
            threshold=policy.confidence_threshold,
            policy_id=policy.entity_id,
            reason=f"checkpoint check failed at boundary={boundary}: {error}",
        )
        if file_brief is not None:
            brief_id = await file_brief(boundary, error, decision)
        else:
            brief_id = write_checkpoint_brief(
                task_entity_id=task_entity_id,
                decision=decision,
                title=title,
                plan_summary=(
                    f"Checkpoint check at boundary '{boundary}' failed ({error}). "
                    f"posture=closed for this boundary, so the swarm stopped "
                    "rather than proceeding on an unverified check. Review the "
                    "failure and reply /approve to proceed, /reject to stop, or "
                    "/hold to park."
                ),
                handler=handler,
            )
        if notify is not None:
            notify(
                f"BLOCKED at checkpoint '{boundary}': check failed ({error}). "
                "posture=closed — waiting for /approve, /reject, or /hold.",
                handler=handler,
            )
        return PostureOutcome(
            boundary=boundary,
            posture=posture,
            proceeded=False,
            checkpoint_brief_id=brief_id,
            error=error,
        )
