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

Scope. It reads only the named task, its checkpoints, its tenant ownership, the
execution policy and the Fly delivery ledger, through the readers the
dispatcher already uses, with the configured Apis credential
(``NEOTOMA_BASE_URL`` / ``NEOTOMA_BEARER_TOKEN``). It never queries the task
collection. It takes no gate override, approval, status, assignee, action type
or tenant input.

First admission only. The task must be explicitly ``pending``, assigned to the
builder with ``action_type: open_or_merge_pr``, name a strict ``owner/repo``,
carry no hold (``blocked_reason``, deferral disposition or another owner), have
no checkpoint at all, and no in-flight or unknown Fly run. Anything else, or any
read that fails, is a refusal with a reason; nothing is reset, cleared,
released or re-filed. A replay of the same request is such a refusal
(``refused`` / ``not_first_admission``, exit 1, zero effects): the task is no
longer ``pending``. This is deliberately not an idempotent success; the state it
reached is read with the task, not by repeating the command.

Tenant and authority. The task snapshot must carry a truthy ``user_id`` that
equals the tenant Apis itself derives for the task (the complete, immutable
observation ownership ``fetch_entity_user_id`` reads with the Apis credential,
which is the tenant the checkpoint will be filed under). A missing, falsy,
unreadable or different tenant is refused. The execution policy for the
assignee must read, be loaded (not the unreachable-Neotoma fallback) and
classify the action as high blast, so the ordinary gate holds it at a
checkpoint; an unreadable or malformed policy is refused.

State this process cannot see. ``--apply`` runs ``apis.dispatch_task`` in this
CLI process, not in the running Apis daemon. The daemon's in-process state is
therefore NOT visible here: the build registry (``build_delivery.active_build``
/ ``_inflight``), the build-run table behind ``tracking_run_id`` (``_runs``),
the detached-run set and the dispatch counters are all empty in this process,
and ``FlyLedger`` reads a missing ledger file as an empty ledger. A dispatch
that consults them would pass vacuously and admit work the daemon would defer or
leave alone. So the entrance fails closed on everything it cannot observe, with
zero writes: it refuses when the task is about an issue
(``build_delivery.issue_key`` resolves, so a build may hold it that only the
daemon can see), when the task text carries a ``build-run:`` marker (it is a
build's own tracking task, which the ordinary path leaves exactly as filed), and
when the configured Fly ledger file (``APIS_FLY_LEDGER_FILE``, default
``~/.config/ateles/fly-runner-ledger.json``, the path the daemon resolves) does
not exist or cannot be read, for instance because this command runs with another
``HOME`` or environment than the daemon.

Exclusion. ``--apply`` moves the task ``pending -> routed`` with a
version-conditional write (``set_task_field_if_version``) against the version
read during validation, then reads the task back. A concurrent caller, or a
task or policy change since the read, loses with a version conflict and starts
nothing. Only after the read-back shows ``routed`` is the task handed to the
SAME ``dispatch_task`` the created-event path uses, so the readiness gate and
the confidence x blast-radius execution gate apply unchanged. A PR delivery is
high blast: it is held at an ordinary checkpoint, and this command grants no
approval or release; the existing release consumer releases it.

Result. ``dispatch_task`` returns without a value and has many early returns
after the claim (a Fly run held by another task, a deferral behind a build, a
build's tracking task, a detached run, a status that changed under the decision,
an unqualified status write) that leave the task ``routed`` with no checkpoint
and no worker. So ``admitted`` is reported ONLY when the final read-back shows a
state the ordinary gate itself writes (``awaiting_approval``, ``awaiting_input``,
``executing``, ``verified`` or ``done``). A task still ``routed`` or returned to
``pending`` is ``stalled``; an unreadable or unexpected final state, a failed
claim read-back, a dispatch error and an unconfirmed claim write are
``unknown``. ``stalled`` and ``unknown`` exit non-zero.

Recovery. If the process dies after the ``routed`` write, or the outcome is
``stalled``, the task is a ``routed`` task with no run. ``task_watchdog``
(``TaskWatchdog.classify``) treats a ``routed`` task untouched for the stall
window (``stall_seconds()``: at least ``APIS_STALL_SECONDS``, default 3600, and
longer than one dispatch) as stalled and re-dispatches it through the same
``dispatch_task`` gates with trigger ``watchdog_retry``, up to ``MAX_ATTEMPTS``
times, after which it writes ``blocked`` and pages the operator; it skips a task
whose Fly run may be in flight. That needs the Apis daemon running with its
watchdog. A task returned to ``pending`` by a deferral is owned by the deferral
recovery sweep instead. Re-running this command does not help (the replay is a
refusal), so the operator next reads the task's status: wait for the watchdog,
or, if the daemon is down, ask the Apis owner to restart it. An unconfirmed
conditional write or a failed claim read-back is ``unknown`` and nothing is
started.

Output: one JSON object (``outcome``, ``task_id``, ``reason``, ``status``),
never task content or error text. Exit 0 for ``preview`` and ``admitted``, 1
otherwise. ``admitted`` means the ordinary gate took the task and wrote the
reported ``status`` (for example ``awaiting_approval``); it is not a finished
worker. A dry run evaluates only the first-admission checks above
(``passes_first_admission_checks``); the readiness and confidence gates run
only on ``--apply``, so an applied task can still end at ``awaiting_input``.
Setup failures print ``{"outcome": "refused", "reason": "setup_failed"}``
instead of a traceback.
"""

from __future__ import annotations

import argparse
import inspect
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
STALLED = "stalled"

# Task fields a build's run marker can ride in (``build_delivery.tracking_run_id``
# reads description, body and notes; the title is checked too).
_MARKER_FIELDS = ("title", "description", "body", "notes")

# The states the ordinary gate itself writes after the claim, and what each
# means for the operator. Anything else is not an admission.
_GATE_STATES = {
    TaskStatus.AWAITING_APPROVAL.value: "held_at_plan_checkpoint",
    TaskStatus.AWAITING_INPUT.value: "readiness_hold_awaiting_input",
    TaskStatus.EXECUTING.value: "dispatched",
    TaskStatus.VERIFIED.value: "dispatched",
    TaskStatus.DONE.value: "dispatched",
}


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
    """The dispatcher's own readers and writers. Injected for tests only.

    ``read_task``, ``checkpoints`` and ``conditional_status`` may be plain
    callables or return an awaitable; the others are plain callables.
    """

    read_task: Callable[[str], tuple[dict | None, str | None]]
    checkpoints: Callable[[str], list | None]
    fly_refuses: Callable[[dict, str], bool]
    canonical_assignee: Callable[[Any], str | None]
    dispatch_repo: Callable[[dict], str | None]
    conditional_status: Callable[..., Any]
    dispatch: Callable[[str, dict, str], Awaitable[Any]]
    # The tenant Apis derives for the task (None when it cannot be established).
    tenant_owner: Callable[[str], str | None]
    # The execution policy governing the named assignee.
    policy: Callable[[str], Any]
    # The Fly ledger file the daemon uses.
    ledger_path: Callable[[], Path]
    # The (repository, issue number) a task is about, as the dispatcher derives it.
    issue_key: Callable[[dict, str | None], Any]


def default_deps(notifier=None) -> AdmissionDeps:
    """Bind to the live Apis helpers (imported here, not at module import).

    Helpers the tests replace on ``apis`` are looked up when called, so a
    patched ``apis`` attribute reaches the dependency.
    """
    import apis
    import build_delivery
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
        tenant_owner=lambda tid: apis.fetch_entity_user_id(tid),
        policy=lambda assignee: apis.resolve_policy_for_agent(assignee),
        ledger_path=build_delivery._registry_path,
        issue_key=build_delivery.issue_key,
    )


async def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a dependency that may be sync or async."""
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _status_label(snapshot: dict | None) -> str | None:
    if not isinstance(snapshot, dict):
        return None
    return normalize(snapshot.get("status")) or None


def _first_admission_refusal(snapshot: dict, task_id: str, deps: AdmissionDeps) -> str | None:
    """Why the task is not an explicit, ready, unheld first admission; None if it is."""
    import build_delivery

    # The tenant must be present and truthy. A reader preserves provenance that
    # disagrees between envelope and snapshot as an explicit None, and a task
    # with no key at all has no tenant to verify: both are absent authority.
    if not snapshot.get("user_id"):
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


def _unobservable_state_refusal(snapshot: dict, deps: AdmissionDeps) -> str | None:
    """Refuse on in-process Apis state this CLI process cannot see (module docstring)."""
    import build_delivery

    for field in _MARKER_FIELDS:
        if build_delivery.RUN_MARKER_PREFIX in str(snapshot.get(field) or ""):
            return "build_tracking_task"
    try:
        if deps.issue_key(snapshot, deps.dispatch_repo(snapshot)):
            return "issue_build_state_unobservable"
    except Exception:  # noqa: BLE001 — an unresolvable key is not "no key"
        return "issue_build_state_unobservable"
    try:
        path = Path(deps.ledger_path())
        if not path.is_file():
            return "fly_ledger_unavailable"
        with open(path, "rb"):
            pass
    except Exception:  # noqa: BLE001 — a ledger we cannot find or read is unknown state
        return "fly_ledger_unavailable"
    return None


def _tenant_refusal(snapshot: dict, task_id: str, deps: AdmissionDeps) -> str | None:
    """Refuse unless the snapshot tenant equals the tenant Apis derives for the task."""
    try:
        owner = deps.tenant_owner(task_id)
    except Exception:  # noqa: BLE001
        return "tenant_unverified"
    if not isinstance(owner, str) or not owner:
        return "tenant_unverified"
    tenant = snapshot.get("user_id")
    if not isinstance(tenant, str) or not tenant:
        return "tenant_unverified"
    if tenant != owner:
        return "tenant_mismatch"
    return None


def _policy_refusal(snapshot: dict, deps: AdmissionDeps) -> str | None:
    """Refuse unless the governing policy is readable, loaded and holds this action."""
    from lib.daemon_runtime.gating import BlastRadius

    try:
        policy = deps.policy(deps.canonical_assignee(snapshot.get("assigned_to")))
        if getattr(policy, "loaded", False) is not True or not callable(
            getattr(policy, "blast_radius_for", None)
        ):
            return "policy_unreadable"
        blast = policy.blast_radius_for(snapshot.get("action_type"))
    except Exception:  # noqa: BLE001 — a failed or malformed read is not an answer
        return "policy_unreadable"
    if blast != BlastRadius.HIGH:
        return "policy_not_checkpointed"
    return None


def _after_dispatch(task_id: str, after: dict | None) -> AdmissionResult:
    """Classify the final read-back; ``admitted`` only for a gate-written state."""
    status = _status_label(after)
    if status is None:
        return AdmissionResult(UNKNOWN, task_id, "final_read_failed", None)
    if status in _GATE_STATES:
        return AdmissionResult(ADMITTED, task_id, _GATE_STATES[status], status)
    if status == TaskStatus.ROUTED.value:
        return AdmissionResult(STALLED, task_id, "dispatch_left_task_routed", status)
    if status == TaskStatus.PENDING.value:
        return AdmissionResult(STALLED, task_id, "dispatch_returned_task_to_pending", status)
    return AdmissionResult(UNKNOWN, task_id, "unexpected_status_after_dispatch", status)


async def admit(task_id: object, *, apply: bool, deps: AdmissionDeps) -> AdmissionResult:
    """Validate, and with ``apply`` admit, one named existing task. Never raises."""
    state = {"claimed": False}
    try:
        return await _admit(task_id, apply, deps, state)
    except Exception:  # noqa: BLE001 — never a traceback, and never a false "refused" after a claim
        if state["claimed"]:
            return AdmissionResult(UNKNOWN, task_id if isinstance(task_id, str) else "", "internal_error")
        return AdmissionResult(REFUSED, task_id if isinstance(task_id, str) else "", "internal_error")


async def _admit(
    task_id: object, apply: bool, deps: AdmissionDeps, state: dict
) -> AdmissionResult:
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        return AdmissionResult(REFUSED, "", "malformed_task_id")

    try:
        snapshot, version = await _call(deps.read_task, task_id)
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

    why = _tenant_refusal(snapshot, task_id, deps)
    if why:
        return AdmissionResult(REFUSED, task_id, why, status)
    why = _policy_refusal(snapshot, deps)
    if why:
        return AdmissionResult(REFUSED, task_id, why, status)
    why = _unobservable_state_refusal(snapshot, deps)
    if why:
        return AdmissionResult(REFUSED, task_id, why, status)

    try:
        if deps.fly_refuses(snapshot, task_id):
            return AdmissionResult(REFUSED, task_id, "delivery_in_flight_or_unknown", status)
    except Exception:  # noqa: BLE001
        return AdmissionResult(REFUSED, task_id, "delivery_ledger_unreadable", status)

    try:
        existing = await _call(deps.checkpoints, task_id)
    except Exception:  # noqa: BLE001
        existing = None
    if existing is None:
        return AdmissionResult(REFUSED, task_id, "checkpoint_read_failed", status)
    if existing:
        return AdmissionResult(REFUSED, task_id, "prior_checkpoint", status)

    if not apply:
        return AdmissionResult(PREVIEW, task_id, "passes_first_admission_checks", status)

    # The one consequential claim: conditional on the version validated above.
    # A per-request key so two callers can never share one deduplicated write.
    state["claimed"] = True  # from here a failure is "unknown", never "refused"
    try:
        write = await _call(
            deps.conditional_status,
            task_id, "status", TaskStatus.ROUTED.value,
            expected_version=version, handler=HANDLER,
            key_suffix=f"{TRIGGER}-{uuid.uuid4().hex}",
        )
    except Exception:  # noqa: BLE001 — the write may or may not have landed
        return AdmissionResult(UNKNOWN, task_id, "claim_write_unconfirmed", status)
    if getattr(write, "conflict", False):
        state["claimed"] = False
        return AdmissionResult(CONFLICT, task_id, "task_changed_or_claimed", status)
    if not write:
        return AdmissionResult(UNKNOWN, task_id, "claim_write_unconfirmed", status)

    try:
        fresh, _ = await _call(deps.read_task, task_id)
    except Exception:  # noqa: BLE001
        fresh = None
    if _status_label(fresh) != TaskStatus.ROUTED.value:
        return AdmissionResult(UNKNOWN, task_id, "claim_readback_failed", _status_label(fresh))

    try:
        await deps.dispatch(task_id, fresh, TRIGGER)
    except Exception:  # noqa: BLE001 — report the type of failure only
        return AdmissionResult(UNKNOWN, task_id, "dispatch_error", TaskStatus.ROUTED.value)

    try:
        after, _ = await _call(deps.read_task, task_id)
    except Exception:  # noqa: BLE001
        after = None
    return _after_dispatch(task_id, after)


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

        try:
            notifier = None
            if args.apply:
                from red_conditions import ApisNotifier

                notifier = ApisNotifier.from_neotoma()
            deps = default_deps(notifier)
        except Exception:  # noqa: BLE001 — a bad environment is a refusal, not a traceback
            result = AdmissionResult(REFUSED, args.task, "setup_failed")
        else:
            result = asyncio.run(admit(args.task, apply=args.apply, deps=deps))
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
