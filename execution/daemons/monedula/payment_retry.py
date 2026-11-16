#!/usr/bin/env python3
"""Operator-run retry of one definitely failed Monedula payment.

This is deliberately not a general replay mechanism. It reconstructs one
recorded calendar obligation, admits only the literal terminal outcome
``failed``, raises a separate payment approval, and journals a single
deterministic retry attempt before calling the existing handler.

ADMISSION
---------
- Any rail, ``failure_class == pre_submission``: the first attempt provably
  stopped before anything was submitted.
- BTC, any failure class: an automatic, recorded chain reconciliation
  (``chain_reconcile``) of the payee address from the original intent time
  onward must show no output paying the payee. It runs before the approval is
  raised (the evidence is a bound field of the approval), again before the
  reservation (the fresh evidence is stored on the reservation), and once
  more after the reservation immediately before ``execute`` (a payment seen
  then aborts the reservation). A failed, timed-out or unparseable lookup
  refuses.
- Wise (or any other rail) with an ``unknown`` or historical unclassified
  failure: refused. Those need manual reconciliation on the rail; no phrase in
  the operator's answer can stand in for it.

The operator's authenticated answer carries attendance only.

THE APPROVAL IS THE CONTROL
---------------------------
``--execute`` with its interactive TTY prompt (typing the amount and payee)
is a guard against a slip of the keyboard. It is not an identity control:
anyone who can run this command on the host can type the confirmation. The
control is the payment_approval record, whose resolution must be
authenticated as the configured approver, and which binds the payment terms,
the original attempt and the chain evidence.

APPROVAL RACES
--------------
The final judgment's approval revision (snapshot plus the observation each
field came from) is bound to the reservation inside the locked journal
transaction, which re-reads the approval and refuses if it changed. After the
reservation and immediately before ``handler.execute()`` the approval is read
again; if it changed, the reservation is marked ``aborted`` and nothing is
paid.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO

# Direct script execution puts only this daemon directory on ``sys.path``.
# Add the repository root before importing consent_email, which uses ``lib``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import chain_reconcile  # noqa: E402
import payment_approval  # noqa: E402
import payment_journal  # noqa: E402
from attendance import parse_reply  # noqa: E402
from consent_email import PendingItem, journal_path as default_journal_path  # noqa: E402
from consent_email import obligation_key  # noqa: E402

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
class RetryRefused(RuntimeError):
    """The selected journal record cannot safely be retried."""


@dataclass(frozen=True)
class RetryResult:
    state: str
    original_key: str
    retry_key: str
    detail: str = ""
    payment_status: str = ""


ADMISSION_PRE_SUBMISSION = "pre_submission"
ADMISSION_BTC_CHAIN = "btc_chain_reconciled"


@dataclass(frozen=True)
class OriginalAttempt:
    outcome: str
    failure_class: str
    intent_at: float
    outcome_at: float


def retry_key(original_key: str) -> str:
    """One deterministic retry identity for one original obligation."""
    basis = f"monedula-payment-retry-v1|{original_key}"
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def _recorded_outcome(
    journal: dict[str, Any], original_key: str
) -> OriginalAttempt:
    entry = journal["obligations"].get(original_key)
    if not isinstance(entry, dict):
        raise RetryRefused(f"payment {original_key} is not recorded in the journal")
    if (
        not isinstance(entry.get("token"), str)
        or not entry["token"]
        or not isinstance(entry.get("generation"), int)
        or isinstance(entry["generation"], bool)
        or not isinstance(entry.get("intent_at"), (int, float))
    ):
        raise RetryRefused(
            f"payment {original_key} has a malformed journal entry; refusing"
        )
    state = entry.get("state")
    if state != payment_journal.STATE_DONE:
        raise RetryRefused(
            f"payment {original_key} outcome is unknown (intent-only or malformed); "
            "refusing a blind retry"
        )
    if not isinstance(entry.get("outcome_at"), (int, float)):
        raise RetryRefused(
            f"payment {original_key} has a malformed journal entry; refusing"
        )
    raw = entry.get("outcome_status")
    if not isinstance(raw, str) or not raw.strip():
        raise RetryRefused(
            f"payment {original_key} has no recognised recorded outcome; refusing"
        )
    outcome = raw.strip().lower()
    if outcome == DEFINITE_FAILURE_OUTCOME:
        failure_class = str(entry.get("failure_class") or "unclassified")
        if failure_class not in (
            payment_journal.FAILURE_PRE_SUBMISSION,
            payment_journal.FAILURE_UNKNOWN,
        ):
            failure_class = "unclassified"
        return OriginalAttempt(
            outcome=outcome,
            failure_class=failure_class,
            intent_at=float(entry["intent_at"]),
            outcome_at=float(entry["outcome_at"]),
        )
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
) -> tuple[Any, str, str, str]:
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
    payment_date = entry.get("started_on")
    if not isinstance(payment_date, str) or not payment_date:
        raise RetryRefused(
            f"session {session_key!r} has no recorded start date; refusing to "
            "reconstruct it from the end time"
        )
    original = entry.get("obligation")
    if not isinstance(original, str) or not original:
        raise RetryRefused(
            f"session {session_key!r} has no recorded obligation key; refusing "
            "to recompute it"
        )
    if entry.get("state") not in payment_journal.SESSION_TERMINAL:
        raise RetryRefused(
            f"session {session_key!r} is not terminal; refusing a retry"
        )
    return handler, payment_date, event_id, original


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
            handler, payment_date, event_id, original = _session_parts(
                session_key, session_entry, by_name
            )
        except RetryRefused:
            if target_is_session or (
                target_is_obligation
                and session_entry.get("obligation") == target
            ):
                raise
            continue
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


def _rail(handler: Any) -> str:
    profile = getattr(handler, "profile", None)
    return str(getattr(profile, "payment_type", "") or "wise")


def _btc_payee_address(handler: Any) -> str:
    """The address ``execute`` pays: the handler's payment_profile."""
    profile = getattr(handler, "profile", None)
    address = str(getattr(profile, "btc_address", "") or "").strip()
    if not address:
        raise RetryRefused("BTC payee address is not configured on the payment profile")
    return address


def _admission(handler: Any, attempt: OriginalAttempt) -> str:
    if _rail(handler) == "btc":
        return ADMISSION_BTC_CHAIN
    if attempt.failure_class == payment_journal.FAILURE_PRE_SUBMISSION:
        return ADMISSION_PRE_SUBMISSION
    raise RetryRefused(
        f"the first {_rail(handler)} attempt's outcome is "
        f"{attempt.failure_class}, not provably pre-submission; it needs "
        "manual reconciliation on the rail and cannot be retried by this command"
    )


def _reconcile(
    explorer: chain_reconcile.EsploraExplorer,
    handler: Any,
    attempt: OriginalAttempt,
) -> dict[str, Any]:
    try:
        evidence = chain_reconcile.reconcile(
            explorer, _btc_payee_address(handler), attempt.intent_at
        )
    except chain_reconcile.ReconciliationError as exc:
        raise RetryRefused(f"chain reconciliation failed; refusing: {exc}") from exc
    if evidence["result"] != chain_reconcile.RESULT_CLEAR:
        raise RetryRefused(
            "the chain shows a payment to the payee since the first attempt "
            f"(txids {', '.join(evidence['paying_txids'])}); refusing a second payment"
        )
    return evidence


def _retry_item(
    original: str,
    handler: Any,
    payment_date: str,
    event_id: str,
    generation: int,
    attempt: OriginalAttempt,
    admission: str,
    evidence_json: str,
) -> PendingItem:
    profile = getattr(handler, "profile", None)
    label = str(getattr(profile, "label", None) or handler.name)
    amount = int(getattr(profile, "amount_eur", 0) or 0)
    retry = retry_key(original)
    if admission == ADMISSION_PRE_SUBMISSION:
        notice = "the first attempt provably stopped before submission"
    else:
        notice = (
            "first attempt outcome unconfirmed by the rail; the chain shows no "
            f"payment to the payee since {_timestamp(chain_reconcile.window_start(attempt.intent_at))} "
            "(evidence in reconciliation_evidence)"
        )
    match = {
        "event": {
            "id": event_id,
            "start": {"date": payment_date},
        },
        "retry_of": original,
        "retry_context": {
            "retry_of": original,
            "original_intent_at": repr(float(attempt.intent_at)),
            "original_outcome_at": repr(float(attempt.outcome_at)),
            "original_failure_class": attempt.failure_class,
            "retry_admission": admission,
            "reconciliation_evidence": evidence_json,
            "operator_notice": notice,
        },
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


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def parse_retry_reply(reply: str | None, handler_names: list[str]) -> set[str]:
    """Attendance only. No phrase in the answer is evidence of non-payment."""
    if not isinstance(reply, str):
        return set()
    return parse_reply(reply, handler_names)


def _confirm_execution(
    item: PendingItem,
    *,
    stdin: TextIO,
    stdout: TextIO,
) -> None:
    if not stdin.isatty():
        raise RetryRefused("--execute requires an interactive TTY")
    payee = payment_approval.payee_display_name(item.handler)
    expected = f"{item.amount_eur} EUR {payee}"
    print(
        "Type the exact amount and payee to execute this retry:\n"
        f"  {expected}",
        file=stdout,
    )
    stdout.flush()
    typed = stdin.readline().rstrip("\r\n")
    if typed != expected:
        raise RetryRefused("typed amount/payee confirmation did not match")


def run_retry(
    target: str,
    *,
    journal_path: Path,
    handlers: Sequence[Any],
    dry_run: bool = False,
    execute: bool = False,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    explorer: chain_reconcile.EsploraExplorer | None = None,
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
    original, session, handler, payment_date, event_id = _resolve_original(
        target, journal, handlers
    )
    attempt = _recorded_outcome(journal, original)
    retry = retry_key(original)
    retry_state = payment_journal.retry_state(journal, original)
    if retry_state == payment_journal.STATE_DONE:
        raise RetryRefused(
            f"retry {retry} already has an outcome; a second retry is forbidden"
        )
    if retry_state == payment_journal.STATE_ABORTED:
        raise RetryRefused(
            f"retry {retry} was aborted before payment because its approval "
            "changed; it needs manual handling"
        )
    if retry_state == payment_journal.STATE_INTENT:
        raise RetryRefused(
            f"retry {retry} outcome is unknown after intent; refusing a second payment"
        )
    admission = _admission(handler, attempt)
    is_btc = admission == ADMISSION_BTC_CHAIN
    if is_btc:
        _btc_payee_address(handler)

    original_entry = journal["obligations"][original]
    generation = int(original_entry.get("generation") or 0) + 1

    def item_for(evidence_json: str) -> PendingItem:
        return _retry_item(
            original,
            handler,
            payment_date,
            event_id,
            generation,
            attempt,
            admission,
            evidence_json,
        )

    if dry_run:
        return RetryResult(
            "dry_run",
            original,
            retry,
            f"admission={admission}; would "
            + ("reconcile the chain, then " if is_btc else "")
            + "raise or inspect exactly one retry approval; no state changed",
        )

    if is_btc and explorer is None:
        explorer = chain_reconcile.EsploraExplorer()

    entity = payment_approval.find_entity(retry)
    if entity is None:
        evidence_json = ""
        if is_btc:
            evidence_json = chain_reconcile.canonical(
                _reconcile(explorer, handler, attempt)
            )
        payment_approval.raise_request(item_for(evidence_json))
        return RetryResult(
            "awaiting_approval",
            original,
            retry,
            "fresh retry approval raised; no payment attempted",
        )

    approved_evidence = str(
        payment_approval._snapshot(entity).get("reconciliation_evidence") or ""
    )
    if is_btc:
        try:
            chain_reconcile.validate_approved(
                approved_evidence,
                explorer=explorer,
                payee_address=_btc_payee_address(handler),
                intent_at=attempt.intent_at,
            )
        except chain_reconcile.ReconciliationError as exc:
            return RetryResult("blocked", original, retry, str(exc))
    # For BTC the stored evidence was just validated against this payment's
    # own payee, window and explorer. No other rail carries evidence, so a
    # non-BTC record with any is a mismatch, never self-confirming.
    item = item_for(approved_evidence if is_btc else "")

    verdict = payment_approval.judge(item, entity, parse_retry_reply)
    if verdict.state == "awaiting":
        return RetryResult(
            "awaiting_approval", original, retry, "approval still awaiting the operator"
        )
    if verdict.state == "skipped":
        return RetryResult("rejected", original, retry, "operator rejected the retry")
    if verdict.state != "approved":
        return RetryResult("blocked", original, retry, verdict.reason)
    if not execute:
        return RetryResult(
            "approved",
            original,
            retry,
            "approval is valid; no payment attempted without --execute",
        )

    _confirm_execution(
        item,
        stdin=stdin or sys.stdin,
        stdout=stdout or sys.stdout,
    )

    if obligation_key(handler, payment_date, event_id) != original:
        raise RetryRefused(
            "payment terms changed after approval; refusing to execute the retry"
        )

    preflight = getattr(handler, "preflight", None)
    if callable(preflight):
        preflight(item.match)

    def current_approval_revision() -> str:
        """Re-read and re-judge the approval; return the revision judged."""
        fresh = payment_approval.find_entity(retry)
        if fresh is None:
            raise RetryRefused("retry approval disappeared")
        fresh_verdict = payment_approval.judge(item, fresh, parse_retry_reply)
        if fresh_verdict.state != "approved":
            raise RetryRefused(
                f"retry approval no longer permits payment: "
                f"{fresh_verdict.reason or fresh_verdict.state}"
            )
        return payment_approval.approval_revision(fresh)

    # The final judgment. The operator may still reject or overwrite the
    # approval after it; the reservation re-checks this exact revision under
    # the journal lock.
    revision = current_approval_revision()
    reconciliation = _reconcile(explorer, handler, attempt) if is_btc else None
    try:
        payment_journal.reserve_retry(
            journal_path,
            original,
            session,
            item.token,
            generation=item.generation,
            expected_intent_at=attempt.intent_at,
            expected_outcome_at=attempt.outcome_at,
            expected_failure_class=attempt.failure_class,
            admission=admission,
            approval_revision=revision,
            approval_check=current_approval_revision,
            reconciliation=(
                {**reconciliation, "approved_evidence_sha256": hashlib.sha256(
                    approved_evidence.encode()
                ).hexdigest()}
                if reconciliation is not None
                else None
            ),
        )
    except payment_journal.JournalError as exc:
        raise RetryRefused(f"retry not reserved: {exc}") from exc

    # Last read before money can move.
    try:
        unchanged = current_approval_revision() == revision
        reason = "approval changed after the reservation"
    except (RetryRefused, payment_approval.ApprovalError) as exc:
        unchanged = False
        reason = f"approval could not be confirmed after the reservation: {exc}"
    if unchanged and is_btc:
        # Last chain read before money can move: a late broadcast of the
        # first attempt aborts the reservation.
        try:
            _reconcile(explorer, handler, attempt)
        except RetryRefused as exc:
            unchanged = False
            reason = f"final chain check refused: {exc}"
    if not unchanged:
        payment_journal.abort_retry(journal_path, original, item.token, reason)
        return RetryResult(
            "aborted",
            original,
            retry,
            f"{reason}; reservation aborted, no payment attempted",
        )

    result = handler.execute(item.match)
    status = (
        str(result.get("status") or "unreported")
        if isinstance(result, dict)
        else "unreported"
    )
    payment_journal.record_retry_outcome(journal_path, original, result)
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
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "execute after a valid approval; requires an interactive TTY and "
            "typed amount/payee confirmation (a typo guard, not an identity "
            "control: the authenticated approval is the control)"
        ),
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
            execute=args.execute,
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
