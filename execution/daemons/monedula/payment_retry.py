#!/usr/bin/env python3
"""Operator-run retry of one definitely failed Monedula payment.

This is deliberately not a general replay mechanism. It reconstructs one
recorded calendar obligation, admits only the literal terminal outcome
``failed``, raises a separate payment approval, and journals a single
deterministic retry attempt before calling the existing handler.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import payment_approval
import payment_journal
from attendance import parse_reply
from consent_email import PendingItem, journal_path as default_journal_path
from consent_email import obligation_key

DEFINITE_FAILURE_OUTCOME = "failed"
KNOWN_REFUSED_OUTCOMES = frozenset(
    {
        "sent",
        "succeeded",
        "success",
        "completed",
        "unknown",
        "pending",
        "processing",
        "in_progress",
        "awaiting",
        "skipped",
        "rejected",
        "declined",
        "manual_required",
        "unreported",
    }
)
MADRID_TZ = ZoneInfo("Europe/Madrid")


class RetryRefused(RuntimeError):
    """The selected journal record cannot safely be retried."""


@dataclass(frozen=True)
class RetryResult:
    state: str
    original_key: str
    retry_key: str
    detail: str = ""
    payment_status: str = ""


def retry_key(original_key: str) -> str:
    """One deterministic retry identity for one original obligation."""
    basis = f"monedula-payment-retry-v1|{original_key}"
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def _recorded_outcome(journal: dict[str, Any], original_key: str) -> str:
    entry = journal["obligations"].get(original_key)
    if not isinstance(entry, dict):
        raise RetryRefused(f"payment {original_key} is not recorded in the journal")
    state = entry.get("state")
    if state != payment_journal.STATE_DONE:
        raise RetryRefused(
            f"payment {original_key} outcome is unknown (intent-only or malformed); "
            "refusing a blind retry"
        )
    raw = entry.get("outcome_status")
    if not isinstance(raw, str) or not raw.strip():
        raise RetryRefused(
            f"payment {original_key} has no recognised recorded outcome; refusing"
        )
    outcome = raw.strip().lower()
    if outcome == DEFINITE_FAILURE_OUTCOME:
        return outcome
    if outcome in KNOWN_REFUSED_OUTCOMES:
        raise RetryRefused(
            f"payment {original_key} outcome {outcome!r} is not a definite failure; "
            "only the literal outcome 'failed' may be retried"
        )
    raise RetryRefused(
        f"payment {original_key} has unrecognised outcome {outcome!r}; refusing"
    )


def _handler_map(handlers: Sequence[Any]) -> dict[str, Any]:
    by_name: dict[str, Any] = {}
    for handler in handlers:
        name = str(getattr(handler, "name", "") or "")
        if not name:
            continue
        if name in by_name:
            raise RetryRefused(f"more than one active payment handler is named {name!r}")
        by_name[name] = handler
    return by_name


def _session_parts(
    session_key: str,
    entry: dict[str, Any],
    handlers: dict[str, Any],
) -> tuple[Any, str, str]:
    handler_name, separator, event_id = session_key.partition(":")
    if not separator or not handler_name or not event_id:
        raise RetryRefused(
            f"session key {session_key!r} is malformed; expected <handler>:<event-id>"
        )
    handler = handlers.get(handler_name)
    if handler is None:
        raise RetryRefused(
            f"session {session_key!r} has no uniquely matching active handler"
        )
    ended_at = entry.get("ended_at")
    if not isinstance(ended_at, (int, float)):
        raise RetryRefused(
            f"session {session_key!r} has no recorded end time; its payment date "
            "cannot be reconstructed safely"
        )
    payment_date = (
        datetime.fromtimestamp(float(ended_at), tz=timezone.utc)
        .astimezone(MADRID_TZ)
        .date()
        .isoformat()
    )
    return handler, payment_date, event_id


def _resolve_original(
    target: str,
    journal: dict[str, Any],
    handlers: Sequence[Any],
) -> tuple[str, str, Any, str, str]:
    """Return original key, session key, handler, payment date and event id."""
    obligations = journal["obligations"]
    sessions = journal["sessions"]
    target_is_obligation = target in obligations
    target_is_session = target in sessions
    if not target_is_obligation and not target_is_session:
        raise RetryRefused(f"payment or session {target!r} is not recorded")

    by_name = _handler_map(handlers)
    candidates: list[tuple[str, str, Any, str, str]] = []
    session_items = (
        [(target, sessions[target])]
        if target_is_session
        else list(sessions.items())
    )
    for session_key, session_entry in session_items:
        if not isinstance(session_entry, dict):
            continue
        try:
            handler, payment_date, event_id = _session_parts(
                session_key, session_entry, by_name
            )
        except RetryRefused:
            if target_is_session:
                raise
            continue
        original = obligation_key(handler, payment_date, event_id)
        if original not in obligations:
            if target_is_session:
                raise RetryRefused(
                    f"session {session_key!r} does not resolve to a recorded "
                    "obligation under the active payment terms"
                )
            continue
        if target_is_obligation and original != target:
            continue
        candidates.append((original, session_key, handler, payment_date, event_id))

    if len(candidates) != 1:
        raise RetryRefused(
            f"payment {target!r} did not resolve to exactly one recorded session "
            f"(found {len(candidates)}); refusing to guess"
        )
    return candidates[0]


def _retry_item(
    original: str,
    handler: Any,
    payment_date: str,
    event_id: str,
    generation: int,
) -> PendingItem:
    profile = getattr(handler, "profile", None)
    label = str(getattr(profile, "label", None) or handler.name)
    amount = int(getattr(profile, "amount_eur", 0) or 0)
    retry = retry_key(original)
    match = {
        "event": {
            "id": event_id,
            "start": {"date": payment_date},
        },
        "retry_of": original,
    }
    return PendingItem(
        handler_name=str(handler.name),
        label=label,
        amount_eur=amount,
        payment_date=payment_date,
        is_calendar=True,
        token=f"retry-{retry[:16]}",
        match_id=event_id,
        match=match,
        handler=handler,
        obligation=retry,
        generation=generation,
    )


def run_retry(
    target: str,
    *,
    journal_path: Path,
    handlers: Sequence[Any],
    dry_run: bool = False,
) -> RetryResult:
    """Advance one failed payment retry by at most one safe step.

    First invocation raises the new approval. After the configured approver
    authenticates an affirmative answer on that record, a later invocation
    records retry intent, executes once, and records the retry outcome.
    """
    target = target.strip()
    if not target:
        raise RetryRefused("a payment obligation or session key is required")

    journal = payment_journal.load(journal_path)
    original, _session, handler, payment_date, event_id = _resolve_original(
        target, journal, handlers
    )
    _recorded_outcome(journal, original)
    retry = retry_key(original)
    retry_state = payment_journal.obligation_state(journal, retry)
    if retry_state == payment_journal.STATE_DONE:
        raise RetryRefused(
            f"retry {retry} already has an outcome; a second retry is forbidden"
        )
    if retry_state == payment_journal.STATE_INTENT:
        raise RetryRefused(
            f"retry {retry} outcome is unknown after intent; refusing a second payment"
        )

    original_entry = journal["obligations"][original]
    generation = int(original_entry.get("generation") or 0) + 1
    item = _retry_item(original, handler, payment_date, event_id, generation)
    if dry_run:
        return RetryResult(
            "dry_run",
            original,
            retry,
            "would raise or inspect exactly one retry approval; no state changed",
        )

    entity = payment_approval.find_entity(retry)
    if entity is None:
        payment_approval.raise_request(item)
        return RetryResult(
            "awaiting_approval",
            original,
            retry,
            "fresh retry approval raised; no payment attempted",
        )

    verdict = payment_approval.judge(item, entity, parse_reply)
    if verdict.state == "awaiting":
        return RetryResult(
            "awaiting_approval", original, retry, "approval still awaiting the operator"
        )
    if verdict.state == "skipped":
        return RetryResult("rejected", original, retry, "operator rejected the retry")
    if verdict.state != "approved":
        return RetryResult("blocked", original, retry, verdict.reason)

    # Re-read every safety input immediately before intent. A concurrent retry,
    # changed payment profile, or changed original record must refuse.
    current = payment_journal.load(journal_path)
    _recorded_outcome(current, original)
    current_retry_state = payment_journal.obligation_state(current, retry)
    if current_retry_state is not None:
        if current_retry_state == payment_journal.STATE_DONE:
            raise RetryRefused(
                f"retry {retry} already has an outcome; a second retry is forbidden"
            )
        raise RetryRefused(
            f"retry {retry} outcome is unknown after intent; refusing a second payment"
        )
    if obligation_key(handler, payment_date, event_id) != original:
        raise RetryRefused(
            "payment terms changed after approval; refusing to execute the retry"
        )

    preflight = getattr(handler, "preflight", None)
    if callable(preflight):
        preflight(item.match)
    payment_journal.record_intent(
        journal_path,
        retry,
        item.token,
        generation=item.generation,
    )
    result = handler.execute(item.match)
    status = (
        str(result.get("status") or "unreported")
        if isinstance(result, dict)
        else "unreported"
    )
    payment_journal.record_outcome(journal_path, retry, status)
    state = "paid" if status == "sent" else "completed_not_sent"
    return RetryResult(
        state,
        original,
        retry,
        f"retry completed with recorded outcome {status!r}",
        payment_status=status,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Raise or execute the one supported retry for one definitely failed "
            "Monedula calendar payment."
        )
    )
    parser.add_argument("payment", help="original obligation key or handler:event-id")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the one action without writing or paying",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        from handlers import load_handlers

        result = run_retry(
            args.payment,
            journal_path=default_journal_path(),
            handlers=load_handlers([]),
            dry_run=args.dry_run,
        )
    except (RetryRefused, payment_journal.JournalError, payment_approval.ApprovalError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(
        f"{result.state}: original={result.original_key} retry={result.retry_key} "
        f"{result.detail}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
