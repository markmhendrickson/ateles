#!/usr/bin/env python3
"""Sort the open checkpoint queue into what truly waits on the operator.

Read only: this module never approves, resolves, retires or changes anything.
Approving a checkpoint releases its task to the dispatcher and rejecting one
declines the task, so what to do with each class stays the operator's call.

Each open checkpoint lands in exactly one class. The order of the checks is the
safety property: a hold a person or session raised, and an operator-only hold,
are recognised before anything that could call a hold noise. A hold this module
does not recognise is kept, never treated as noise, so the failure of an
incomplete rule is a longer queue rather than a real decision lost.

The labels on a hold are mechanical. The action type behind a "payment" hold is
often a guess from the task's words, so ``live_effect`` means "says it might
touch money, mail or publication", not "does". It is kept for the operator
rather than sorted further here.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.task_lifecycle import is_terminal, normalize  # noqa: E402
from lib.daemon_runtime.task_ownership import session_owned  # noqa: E402

# ── Classes ──────────────────────────────────────────────────────────────────
RAISED_BY_PERSON_OR_SESSION = "raised_by_person_or_session"
OPERATOR_ONLY = "operator_only"
OPERATOR_ONLY_LABEL = "operator_only_label"
OPERATOR_ONLY_TASK_FINISHED = "operator_only_task_finished"
OWNED_BY_PERSON = "owned_by_person"
SESSION_WROTE_HOLD = "session_wrote_hold"
TASK_FINISHED = "task_finished"
OLDER_DUPLICATE = "older_duplicate"
TASK_MOVED_ON = "task_moved_on"
LIVE_SESSION_OWNED = "live_session_owned"
LIVE_EFFECT = "live_effect"
LIVE_ASKS_FOR_DECISION = "live_asks_for_decision"
LIVE_CODE = "live_code"
LIVE_LOW = "live_low"
UNRECOGNISED = "unrecognised"

#: What reaches the operator: a person's or session's own hold, work only the
#: operator can do, anything that may touch money, mail or publication, anything
#: whose title asks for a decision, and anything this module cannot place. The
#: daily reading ages exactly these. An operator-only hold on finished work stays
#: open until the operator says otherwise but has nothing left to decide, and a
#: task a person already owns is that person's to do with or without a hold.
REAL_DECISION_CLASSES = frozenset(
    {
        RAISED_BY_PERSON_OR_SESSION,
        OPERATOR_ONLY,
        OPERATOR_ONLY_LABEL,
        SESSION_WROTE_HOLD,
        LIVE_EFFECT,
        LIVE_ASKS_FOR_DECISION,
        UNRECOGNISED,
    }
)

# ── What the gate writes ─────────────────────────────────────────────────────
GATE_HANDLER = "apis"
GATE_CHECKPOINT_NAME = "PLAN"
GATE_ACTIONS = frozenset(
    {"checkpoint_plan_approval", "checkpoint_plan_approval_with_alternatives"}
)
#: Every reason the gate composes without anyone choosing the words
#: (``lib.daemon_runtime.gating.evaluate_gate`` and the replacement paths). A
#: note may follow after a semicolon. Anything else was written by someone.
AUTOMATIC_REASON_PREFIXES = (
    "not scored by a producer",
    "below confidence threshold",
    "low confidence and high blast radius",
    "high blast radius — operator approval required",
    "operator-only action — never auto-executable",
    "producer score could not be recorded",
    "task authorization context changed",
    "re-issued checkpoint",
    "checkpoint check failed at boundary",
)
OPERATOR_ONLY_PREFIX = "operator-only action"

#: Action types that really do name the operator. The gate writes the words
#: "operator-only action" on any action type its policy does not list, so most of
#: that label is a classification gap, not a reservation; the two are told apart
#: here and both stay until the operator rules otherwise.
OPERATOR_ACTION_TYPES = frozenset(
    {"operator_only", "operator_action", "operator_decision", "decision"}
)
#: The high-blast action types that reach outside. Mirrors what the default
#: execution policy gates at its own entrances.
EFFECT_ACTION_TYPES = frozenset(
    {
        "payment",
        "send_external_comms",
        "publish",
        "delete_entity_or_data",
        "external_api_write",
        "release",
        "correspondence",
    }
)
#: A title that asks somebody to choose. Matched on the task's title only, so it
#: errs towards keeping a hold: a false match costs one more line in the reading,
#: while a miss would hide a real decision among the noise.
_ASKS_FOR_DECISION = re.compile(
    r"^\W*(decide|decision|approve|approval|choose|confirm|sign|rule on|ruling|"
    r"operator|review and (decide|approve))\b"
    r"|\b(your|operator|mark'?s?) (decision|approval|sign-?off|call|ruling)\b"
    r"|\b(needs|awaiting|requires?) (the )?(operator|human)\b",
    re.IGNORECASE,
)
_ACTION_IN_SUMMARY = re.compile(r"\bAction: ([^.]+)\.")
_DEFAULT_PERSON_OWNERS = frozenset({"operator"})


def _person_owners() -> frozenset[str]:
    """Names meaning a person owns the task, from the environment, never code."""
    names = set(_DEFAULT_PERSON_OWNERS)
    for var in ("APIS_OPERATOR_LOGIN", "ATELES_OPERATOR_NAMES"):
        names.update(
            part.strip().lower()
            for part in os.environ.get(var, "").split(",")
            if part.strip()
        )
    return frozenset(names)


@dataclass(frozen=True)
class Placement:
    """Where one open checkpoint landed and what acting on it could do."""

    checkpoint_id: str
    task_id: str | None
    klass: str
    action_type: str | None
    blast_radius: str | None
    raised_at: str | None
    age_hours: float | None
    task_status: str | None
    approval_could_release: bool


def is_automatic_reason(reason: Any) -> bool:
    text = str(reason or "").strip()
    return any(text.startswith(prefix) for prefix in AUTOMATIC_REASON_PREFIXES)


def resolved_action_type(brief: dict, task: dict | None) -> str | None:
    """The action type the gate judged, which its summary states, else declared."""
    match = _ACTION_IN_SUMMARY.search(str(brief.get("plan_summary") or ""))
    if match:
        value = match.group(1).strip().lower()
        if value and value != "unknown":
            return value
    declared = (task or {}).get("action_type")
    return str(declared).strip().lower() if declared else None


def is_gate_written(brief: dict) -> bool:
    return (
        brief.get("handler") == GATE_HANDLER
        and brief.get("checkpoint_name") == GATE_CHECKPOINT_NAME
        and brief.get("gate_action") in GATE_ACTIONS
    )


def approval_could_release(brief: dict, task_record: dict | None) -> bool:
    """False when approving could not start work, whatever else is true.

    Conservative in one direction only: True means the hold's signed record still
    names the task as it is now, so approval MAY release it; the release path
    makes further checks. False is firm: release refuses a brief with no signed
    record and one bound to a different revision of the task, and files a fresh
    checkpoint instead.
    """
    if not task_record or normalize(
        (task_record.get("snapshot") or {}).get("status")
    ) != ("awaiting_approval"):
        return False
    if str(brief.get("blast_radius") or "").strip().lower() not in {"low", "high"}:
        return False
    body = brief.get("body")
    if not isinstance(body, str):
        return False
    try:
        envelope = json.loads(body)
    except ValueError:
        return False
    return (
        isinstance(envelope, dict)
        and envelope.get("task_entity_id") == task_record.get("entity_id")
        and envelope.get("task_observation_count")
        == task_record.get("observation_count")
        and envelope.get("task_last_observation_at")
        == task_record.get("last_observation_at")
    )


def classify_one(
    brief: dict,
    task: dict | None,
    *,
    has_newer_open: bool,
    person_owners: frozenset[str] | None = None,
) -> str:
    """Return the class of one open checkpoint. First match wins; order matters."""
    people = person_owners if person_owners is not None else _person_owners()
    reason = str(brief.get("reason") or "").strip()

    # 1. Not the gate's own wording or shape: somebody raised this.
    if not is_gate_written(brief) or not is_automatic_reason(reason):
        return RAISED_BY_PERSON_OR_SESSION
    if task is None:
        return UNRECOGNISED

    action_type = resolved_action_type(brief, task)

    # 2. Operator-only holds stay, whatever became of the task. A task can carry
    #    the label from a later gate pass than this brief, so both are read.
    assigned = str(task.get("assigned_to") or "").strip().lower()
    owner = str(task.get("owner") or "").strip().lower()
    status = task.get("status")
    task_reason = str(task.get("blocked_reason") or "").strip()
    if reason.startswith(OPERATOR_ONLY_PREFIX) or task_reason.startswith(
        OPERATOR_ONLY_PREFIX
    ):
        if is_terminal(status):
            return OPERATOR_ONLY_TASK_FINISHED
        return (
            OPERATOR_ONLY
            if action_type in OPERATOR_ACTION_TYPES
            else OPERATOR_ONLY_LABEL
        )

    # 3. A person owns it.
    if not is_terminal(status) and (assigned in people or owner in people):
        return OWNED_BY_PERSON

    # 4. The work is over or has left the hold: the checkpoint is a leftover.
    if is_terminal(status):
        return TASK_FINISHED
    if has_newer_open:
        return OLDER_DUPLICATE
    if normalize(status) != "awaiting_approval":
        return TASK_MOVED_ON

    # 5. Still held. A reason on the task that the gate did not write is someone
    #    saying why it waits, which is a statement to keep, not noise.
    if not is_automatic_reason(task.get("blocked_reason")):
        return SESSION_WROTE_HOLD

    # 6. Still held only because the gate held it.
    #    The raw stored values, as the dispatcher's router reads them: the two
    #    must agree on which tasks a session owns.
    if session_owned(task.get("assigned_to"), task.get("owner")):
        return LIVE_SESSION_OWNED
    if action_type in EFFECT_ACTION_TYPES:
        return LIVE_EFFECT
    if _ASKS_FOR_DECISION.search(str(task.get("title") or "")):
        return LIVE_ASKS_FOR_DECISION
    if str(brief.get("blast_radius") or "").strip().lower() == "high":
        return LIVE_CODE
    return LIVE_LOW


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def classify_queue(
    briefs: Iterable[dict],
    tasks_by_id: dict[str, dict],
    *,
    now: datetime | None = None,
    person_owners: frozenset[str] | None = None,
) -> list[Placement]:
    """Place every open checkpoint. ``briefs`` and tasks are full entity records."""
    moment = now or datetime.now(timezone.utc)
    open_briefs = [
        record
        for record in briefs
        if (record.get("snapshot") or {}).get("status") == "awaiting_operator"
    ]
    newest: dict[str, str] = {}
    for record in open_briefs:
        task_id = (record.get("snapshot") or {}).get("task_entity_id")
        stamp = str(record.get("last_observation_at") or "")
        if task_id and stamp > newest.get(task_id, ""):
            newest[task_id] = stamp

    placements: list[Placement] = []
    for record in open_briefs:
        brief = record.get("snapshot") or {}
        task_id = brief.get("task_entity_id")
        task_record = tasks_by_id.get(task_id) if task_id else None
        task = (task_record or {}).get("snapshot")
        stamp = str(record.get("last_observation_at") or "")
        raised = _parse_time(record.get("last_observation_at"))
        placements.append(
            Placement(
                checkpoint_id=str(record.get("entity_id") or ""),
                task_id=task_id,
                klass=classify_one(
                    brief,
                    task,
                    has_newer_open=bool(task_id) and stamp < newest.get(task_id, ""),
                    person_owners=person_owners,
                ),
                action_type=resolved_action_type(brief, task),
                blast_radius=brief.get("blast_radius"),
                raised_at=record.get("last_observation_at"),
                age_hours=(
                    round((moment - raised).total_seconds() / 3600, 1)
                    if raised
                    else None
                ),
                task_status=(task or {}).get("status"),
                approval_could_release=approval_could_release(brief, task_record),
            )
        )
    return placements


def summarize(placements: list[Placement], *, wait_limit_hours: float = 72.0) -> dict:
    """Counts per class, and the reading the 72-hour test is judged on."""
    counts: collections.Counter[str] = collections.Counter(p.klass for p in placements)
    release: collections.Counter[str] = collections.Counter(
        p.klass for p in placements if p.approval_could_release
    )
    # One decision per task, aged from its oldest open hold: the same task held
    # three times is one question that has waited since the first time it was asked.
    oldest_per_task: dict[str, float | None] = {}
    for p in placements:
        if p.klass not in REAL_DECISION_CLASSES:
            continue
        key = p.task_id or p.checkpoint_id
        held = oldest_per_task.get(key)
        if key not in oldest_per_task or (p.age_hours or 0) > (held or 0):
            oldest_per_task[key] = p.age_hours
    real = oldest_per_task
    ages = sorted(age for age in real.values() if age is not None)
    return {
        "total_open": len(placements),
        "by_class": dict(counts.most_common()),
        "approval_could_release_by_class": dict(release.most_common()),
        "real_decisions": len(real),
        "real_decisions_over_limit": sum(1 for age in ages if age > wait_limit_hours),
        "wait_limit_hours": wait_limit_hours,
        "real_decisions_oldest_hours": ages[-1] if ages else None,
        "real_decisions_median_hours": ages[len(ages) // 2] if ages else None,
    }


def _load_ndjson(path: Path) -> list[dict]:
    with path.open() as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--briefs", type=Path, required=True, help="NDJSON of checkpoint records"
    )
    parser.add_argument(
        "--tasks", type=Path, required=True, help="NDJSON of task records"
    )
    parser.add_argument(
        "--placements", type=Path, help="write every placement here as NDJSON"
    )
    args = parser.parse_args(argv)

    tasks = {record["entity_id"]: record for record in _load_ndjson(args.tasks)}
    placements = classify_queue(_load_ndjson(args.briefs), tasks)
    if args.placements:
        with args.placements.open("w") as handle:
            for placement in placements:
                handle.write(json.dumps(asdict(placement)) + "\n")
    print(json.dumps(summarize(placements), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
