#!/usr/bin/env python3
"""Admit ONE existing, ready PR-delivery task through the ordinary Apis path.

Apis dispatches a task off its ``task.created`` event, and the level-triggered
backstop (``task_reconciler.py``) is off by default. A task whose single
created event was never consumed therefore has no supported way in: the
reconciler would sweep the whole backlog, and a fake event or a clone would
create a second record. This command is the narrow entrance for exactly one
named task, and it leaves the global reconciler disabled.

Usage (dry run by default; writes nothing)::

    python3 execution/daemons/apis/admit_task.py --task <task_entity_id>
    python3 execution/daemons/apis/admit_task.py --task <task_entity_id> --apply

Scope. It reads only the named task, its checkpoints and the Fly delivery
ledger, through the readers the dispatcher already uses, with the configured
Apis credential (``NEOTOMA_BASE_URL`` / ``NEOTOMA_BEARER_TOKEN``). It never
queries the task collection. It takes no gate override, approval, status,
assignee, action type or tenant input.

First admission only. The task must be explicitly ``pending``, assigned to the
builder with ``action_type: open_or_merge_pr``, name a strict ``owner/repo``,
carry no hold (``blocked_reason``, deferral disposition or another owner), have
no checkpoint at all, and no in-flight or unknown Fly run. Anything else, or any
read that fails, is a refusal with a reason; nothing is reset, cleared,
released or re-filed.

Exclusion. ``--apply`` moves the task ``pending -> routed`` with a
version-conditional write (``set_task_field_if_version``) against the version
read during validation, then reads the task back. A concurrent caller, or a
task or policy change since the read, loses with a version conflict and starts
nothing. Only after the read-back shows ``routed`` is the task handed to the
SAME ``dispatch_task`` the created-event path uses, so the readiness gate and
the confidence x blast-radius execution gate apply unchanged. A PR delivery is
high blast: it is held at an ordinary checkpoint, and this command grants no
approval or release; the existing release consumer releases it.

Recovery. A replay of the same request finds the task no longer ``pending`` and
refuses with zero effects. If the process dies after the ``routed`` write, the
task is a stalled ``routed`` task, which the watchdog already owns. An
unconfirmed conditional write is reported as ``unknown`` and nothing is started.

Output: one JSON object (``outcome``, ``task_id``, ``reason``, ``status``),
never task content or error text. Exit 0 for ``preview`` and ``admitted``,
1 otherwise. ``admitted`` means the ordinary path accepted the task; the
reported ``status`` (for example ``awaiting_approval``) is the state it is in,
not a finished worker.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]
for _p in (str(_HERE), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.daemon_runtime.task_lifecycle import TaskStatus, normalize  # noqa: E402

HANDLER = "apis"
TRIGGER = "admit"

# Canonical Neotoma entity ids. Checked before any read, so a malformed
# invocation touches nothing.
_TASK_ID_RE = re.compile(r"ent_[0-9a-f]{24}")

PREVIEW = "preview"
ADMITTED = "admitted"
REFUSED = "refused"
CONFLICT = "conflict"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class AdmissionResult:
    outcome: str
    task_id: str
    reason: str
    status: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return self.outcome in (PREVIEW, ADMITTED)


@dataclass
class AdmissionDeps:
    """The dispatcher's own readers and writers. Injected for tests only."""

    read_task: Callable[[str], tuple[dict | None, str | None]]
    checkpoints: Callable[[str], list | None]
    fly_refuses: Callable[[dict, str], bool]
    canonical_assignee: Callable[[Any], str | None]
    dispatch_repo: Callable[[dict], str | None]
    conditional_status: Callable[..., Any]
    dispatch: Callable[[str, dict, str], Awaitable[Any]]


def default_deps(notifier=None) -> AdmissionDeps:
    """Bind to the live Apis helpers (imported here, not at module import)."""
    import apis
    from lib.daemon_runtime import gating

    def _dispatch(task_id: str, snapshot: dict, trigger: str):
        return apis.dispatch_task(
            task_id, snapshot, trigger, notifier=notifier, snapshot_hydrated=True
        )

    return AdmissionDeps(
        read_task=gating.fetch_task_snapshot_and_version,
        checkpoints=lambda tid: gating.checkpoints_for_task(tid, strict=True),
        fly_refuses=lambda snap, tid: apis._fly_run_refuses_dispatch(snap, tid, "admit"),
        canonical_assignee=apis._canonical_assignee,
        dispatch_repo=apis._dispatch_repo_from_snapshot,
        conditional_status=apis.set_task_field_if_version,
        dispatch=_dispatch,
    )


def _status_label(snapshot: dict | None) -> str | None:
    if not isinstance(snapshot, dict):
        return None
    return normalize(snapshot.get("status")) or None


def _first_admission_refusal(snapshot: dict, task_id: str, deps: AdmissionDeps) -> str | None:
    """Why the task is not an explicit, ready, unheld first admission; None if it is."""
    import build_delivery

    # Tenant provenance that disagrees between envelope and snapshot is
    # preserved by the reader as an explicit None: absent authority.
    if "user_id" in snapshot and not snapshot.get("user_id"):
        return "tenant_unverified"
    raw_status = snapshot.get("status")
    if not isinstance(raw_status, str) or normalize(raw_status) != TaskStatus.PENDING.value:
        return "not_first_admission"
    if str(snapshot.get("blocked_reason") or "").strip():
        return "held"
    if snapshot.get("disposition"):
        return "held_by_recovery"
    assignee = deps.canonical_assignee(snapshot.get("assigned_to"))
    action = snapshot.get("action_type")
    action = action.strip().lower() if isinstance(action, str) else ""
    if not build_delivery.is_delivery_dispatch(assignee, action):
        return "not_pr_delivery"
    owner = snapshot.get("owner")
    if owner and deps.canonical_assignee(owner) != assignee:
        return "foreign_owner"
    try:
        build_delivery.validate_repository(deps.dispatch_repo(snapshot))
    except Exception:  # noqa: BLE001 — any invalid or absent repository refuses
        return "repository_invalid"
    return None


async def admit(task_id: object, *, apply: bool, deps: AdmissionDeps) -> AdmissionResult:
    """Validate, and with ``apply`` admit, one named existing task. Never raises."""
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        return AdmissionResult(REFUSED, "", "malformed_task_id")

    try:
        snapshot, version = deps.read_task(task_id)
    except Exception:  # noqa: BLE001 — a failed read is not an absent task
        return AdmissionResult(REFUSED, task_id, "task_read_failed")
    if not isinstance(snapshot, dict):
        return AdmissionResult(REFUSED, task_id, "task_unreadable")
    status = _status_label(snapshot)

    why = _first_admission_refusal(snapshot, task_id, deps)
    if why:
        return AdmissionResult(REFUSED, task_id, why, status)
    if not version:
        return AdmissionResult(REFUSED, task_id, "task_version_unavailable", status)

    try:
        if deps.fly_refuses(snapshot, task_id):
            return AdmissionResult(REFUSED, task_id, "delivery_in_flight_or_unknown", status)
    except Exception:  # noqa: BLE001
        return AdmissionResult(REFUSED, task_id, "delivery_ledger_unreadable", status)

    try:
        existing = deps.checkpoints(task_id)
    except Exception:  # noqa: BLE001
        existing = None
    if existing is None:
        return AdmissionResult(REFUSED, task_id, "checkpoint_read_failed", status)
    if existing:
        return AdmissionResult(REFUSED, task_id, "prior_checkpoint", status)

    if not apply:
        return AdmissionResult(PREVIEW, task_id, "eligible", status)

    # The one consequential claim: conditional on the version validated above.
    # A per-request key so two callers can never share one deduplicated write.
    write = deps.conditional_status(
        task_id, "status", TaskStatus.ROUTED.value,
        expected_version=version, handler=HANDLER,
        key_suffix=f"{TRIGGER}-{uuid.uuid4().hex}",
    )
    if getattr(write, "conflict", False):
        return AdmissionResult(CONFLICT, task_id, "task_changed_or_claimed", status)
    if not write:
        return AdmissionResult(UNKNOWN, task_id, "claim_write_unconfirmed", status)

    try:
        fresh, _ = deps.read_task(task_id)
    except Exception:  # noqa: BLE001
        fresh = None
    if _status_label(fresh) != TaskStatus.ROUTED.value:
        return AdmissionResult(UNKNOWN, task_id, "claim_readback_failed", _status_label(fresh))

    try:
        await deps.dispatch(task_id, fresh, TRIGGER)
    except Exception:  # noqa: BLE001 — report the type of failure only
        return AdmissionResult(UNKNOWN, task_id, "dispatch_error", TaskStatus.ROUTED.value)

    try:
        after, _ = deps.read_task(task_id)
    except Exception:  # noqa: BLE001
        after = None
    return AdmissionResult(ADMITTED, task_id, "ordinary_dispatch_entered", _status_label(after))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Admit one existing ready PR-delivery task (dry run by default)."
    )
    parser.add_argument("--task", required=True, metavar="TASK_ID",
                        help="the one existing task entity id to admit")
    parser.add_argument("--apply", action="store_true",
                        help="admit it through the ordinary dispatch path; default writes nothing")
    args = parser.parse_args(argv)

    if not _TASK_ID_RE.fullmatch(args.task):
        result = AdmissionResult(REFUSED, "", "malformed_task_id")
    else:
        import asyncio

        notifier = None
        if args.apply:
            from red_conditions import ApisNotifier

            notifier = ApisNotifier.from_neotoma()
        result = asyncio.run(admit(args.task, apply=args.apply, deps=default_deps(notifier)))
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
