"""Per-payment approval records for Monedula (operator ruling, 2026-11-12;
agent_policy ent_a8eabeafc9a3ae6eaa8ff86c).

Payment consent no longer travels over Telegram or email replies. For each
payment Monedula writes ONE pending Neotoma entity of type ``payment_approval``
and pays only when it later reads that same entity back as approved, with a
recorded operator answer that passes the attendance parser.

WHY A DEDICATED ENTITY TYPE (not a task checkpoint_brief)
---------------------------------------------------------
Approving a ``checkpoint_brief`` releases its task to the Apis dispatcher. A
payment approval must be consumed by Monedula alone, so it lives in a type
Apis never reads: Apis subscribes only to ``task`` and ``checkpoint_brief``
(``SUBSCRIBE_ENTITY_TYPES``), and it carries no task reference at all. There is
no task to re-dispatch, so resolving it cannot start any other agent.
``test_payment_approval.py`` pins that subscription list.

THE RECORD
----------
Identity is the obligation key (``payment_key``): the hash of handler, session
date, event id, payee identity, amount and currency. Fields:

  entity_type            payment_approval
  payment_key            obligation key (hash; no payee data)
  handler, session, session_date
  amount, currency, rail ("wise" | "btc"), payee_display_name, reference
  status                 awaiting_operator -> approved | rejected
  operator_answer        the operator's LITERAL answer, written by the resolver
  blocking, confidence_threshold (1.0)
  title                  "Payment approval: ... [<short key>]"

It NEVER carries an IBAN, BTC address or recipient id. BTC payments carry no
memo/OP_RETURN, so ``reference`` is empty for them.

WHAT THE COORDINATOR WATCHES AND DOES
-------------------------------------
Watch:   entity_type ``payment_approval`` with ``status == "awaiting_operator"``
         (POST /entities/query {"entity_type": "payment_approval", ...}).
Resolve: put the operator's literal answer to him, then correct two fields on
         that entity, answer FIRST: ``operator_answer`` = the literal text,
         then ``status`` = ``approved`` or ``rejected``.
         Monedula pays only for ``approved`` plus an answer such as
         ``attended <handler>`` / ``yes <handler>``; a bare ``yes`` does not pay.

FAIL CLOSED
-----------
No record, unresolved, rejected, unparseable answer, a record that does not
match the payment about to be made (amount, currency, payee, session, rail,
reference), or Neotoma unreachable: no payment. Writes are read back
(principle 2): a record that cannot be read back with every field intact is an
error, never a pending request.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

ENTITY_TYPE = "payment_approval"
STATUS_AWAITING = "awaiting_operator"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0"
QUERY_LIMIT = 200

# Fields compared between the stored record and the payment about to be made.
BOUND_FIELDS = (
    "handler",
    "session",
    "session_date",
    "amount",
    "currency",
    "rail",
    "payee_display_name",
    "reference",
)

# Never written. Asserted by tests so a future field cannot leak them.
FORBIDDEN_FIELD_NAMES = frozenset(
    {"iban", "wise_iban", "btc_address", "recipient_id", "wise_recipient_id", "memo"}
)


class ApprovalError(Exception):
    """Neotoma could not be read or written, or a write did not read back."""


@dataclass
class Verdict:
    """What the record says about one payment."""

    state: str  # "awaiting" | "approved" | "skipped" | "blocked"
    reason: str = ""


def _request(method: str, path: str, body: dict | None = None) -> Any:
    """One Neotoma REST call. Injectable seam: tests replace this function."""
    base = os.environ.get("NEOTOMA_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise ApprovalError("NEOTOMA_BASE_URL is not set")
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if bearer and "localhost" not in base and "127.0.0.1" not in base:
        headers["Authorization"] = f"Bearer {bearer}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}", data=data, method=method, headers=headers
    )
    req.add_header("User-Agent", NEOTOMA_USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read() or b"null")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ApprovalError(f"Neotoma request failed: {type(exc).__name__}") from exc


def payee_display_name(handler: Any) -> str:
    fn = getattr(handler, "payee_display_name", None)
    if callable(fn):
        return str(fn() or "")
    profile = getattr(handler, "profile", None)
    return str(
        getattr(profile, "wise_recipient_name", "") or getattr(profile, "label", "") or ""
    )


def expected_fields(item: Any) -> dict[str, Any]:
    """The bound fields of the payment ``item`` would make right now, taken
    from the live handler (the same source ``execute`` pays from)."""
    from consent_email import CURRENCY

    handler = item.handler
    profile = getattr(handler, "profile", None)
    rail = str(getattr(profile, "payment_type", "") or "wise")
    # BTC payments never carry a memo/OP_RETURN, so no reference is recorded.
    reference = (
        "" if rail == "btc" else str(getattr(profile, "wise_reference", "") or "")
    )
    return {
        "handler": str(item.handler_name),
        "session": str(item.match_id or item.payment_date),
        "session_date": str(item.payment_date),
        "amount": int(item.amount_eur),
        "currency": CURRENCY,
        "rail": rail,
        "payee_display_name": payee_display_name(handler),
        "reference": reference,
    }


def _snapshot(entity: dict) -> dict:
    snap = entity.get("snapshot") or entity.get("fields") or entity
    return snap if isinstance(snap, dict) else {}


def _title(item: Any) -> str:
    return (
        f"Payment approval: {item.label} {item.payment_date} "
        f"[{str(item.obligation)[:12]}]"
    )


def find_record(payment_key: str) -> dict | None:
    """The stored snapshot for ``payment_key``, or None if there is none.

    Raises :class:`ApprovalError` when Neotoma cannot be read: an unreadable
    record is never "no record" (that would re-raise and could mask a prior
    approval), and never an approval.
    """
    data = _request(
        "POST",
        "/entities/query",
        {"entity_type": ENTITY_TYPE, "limit": QUERY_LIMIT, "include_snapshots": True},
    )
    if isinstance(data, dict):
        rows = data.get("entities") or data.get("items") or data.get("results") or []
    elif isinstance(data, list):
        rows = data
    else:
        raise ApprovalError("Neotoma query returned an unexpected shape")
    if not isinstance(rows, list):
        raise ApprovalError("Neotoma query returned an unexpected shape")
    for row in rows:
        if isinstance(row, dict) and _snapshot(row).get("payment_key") == payment_key:
            return _snapshot(row)
    return None


def raise_request(item: Any) -> dict:
    """Write the pending record for ``item`` and read it back.

    Idempotent on the obligation key. Raises :class:`ApprovalError` unless the
    record reads back with every bound field and ``awaiting_operator`` status.
    """
    fields = expected_fields(item)
    entity = {
        "entity_type": ENTITY_TYPE,
        "payment_key": str(item.obligation),
        "title": _title(item),
        "status": STATUS_AWAITING,
        "blocking": True,
        "confidence_threshold": 1.0,
        **fields,
    }
    assert not (FORBIDDEN_FIELD_NAMES & set(entity)), "payment_approval leaks a payee detail"
    _request(
        "POST",
        "/store",
        {
            "entities": [entity],
            "idempotency_key": f"monedula-payment-approval-{item.obligation}",
        },
    )
    back = find_record(str(item.obligation))
    if back is None:
        raise ApprovalError("payment_approval did not read back after write")
    for name, want in fields.items():
        if back.get(name) != want:
            raise ApprovalError(f"payment_approval field {name!r} did not persist")
    if back.get("status") != STATUS_AWAITING:
        raise ApprovalError("payment_approval did not read back as awaiting_operator")
    return back


def judge(item: Any, record: dict, parse_reply: Callable[[str | None, list[str]], set[str]]) -> Verdict:
    """Decide what a stored record permits for ``item``. Pure; no I/O."""
    status = str(record.get("status") or "").strip().lower()
    if status == STATUS_AWAITING or status == "":
        return Verdict("awaiting")
    if status == STATUS_REJECTED:
        return Verdict("skipped", "rejected")
    if status != STATUS_APPROVED:
        return Verdict("blocked", f"unrecognised status {status!r}")

    want = expected_fields(item)
    if record.get("payment_key") != str(item.obligation):
        return Verdict("blocked", "record is for a different payment")
    for name in BOUND_FIELDS:
        if record.get(name) != want[name]:
            return Verdict("blocked", f"approval does not match the payment ({name})")

    answer = record.get("operator_answer")
    if not isinstance(answer, str) or not answer.strip():
        return Verdict("blocked", "approved with no recorded operator answer")
    if str(item.handler_name) not in parse_reply(answer, [str(item.handler_name)]):
        return Verdict(
            "blocked",
            "recorded answer does not name the attended session (a bare yes does not approve)",
        )
    return Verdict("approved")


def collect(
    triggered: list[tuple[Any, list]],
    yesterday_str: str,
    parse_reply: Callable[[str | None, list[str]], set[str]],
):
    """Raise one pending record per payment (once) and read each back.

    Returns a ``ConsentEmailResult`` so the existing execute loop in
    ``monedula.main`` is reused unchanged: ``approved`` only for an approved
    record whose bound fields match and whose recorded answer parses. Any
    Neotoma failure blocks the whole pending set (``channel_ok`` False): no
    payment, and the pending sessions stay in the journal for the next tick.
    """
    import payment_journal
    from consent_email import (
        ConsentEmailResult,
        build_pending_items,
        journal_path,
        match_key,
    )

    result = ConsentEmailResult()
    jpath = journal_path()
    try:
        generation = payment_journal.current_generation(jpath)
    except payment_journal.JournalError:
        generation = 0
    items = build_pending_items(triggered, yesterday_str, generation)
    result.pending_items = items
    result.generation = generation

    def block_all(reason: str) -> Any:
        for item in items:
            key = match_key(item)
            result.states[key] = "blocked"
            result.blocked.add(key)
        result.reason_code = reason
        result.channel_ok = False
        return result

    for item in items:
        key = match_key(item)
        try:
            record = find_record(str(item.obligation))
            if record is None:
                record = raise_request(item)
                log.info(
                    f"payment_approval_raised handler={item.handler_name} "
                    f"session={item.match_id or item.payment_date}"
                )
        except ApprovalError as exc:
            log.error(f"payment_approval_unavailable — payments held: {exc}")
            return block_all("payment_approval_unavailable")
        verdict = judge(item, record, parse_reply)
        if verdict.state == "approved":
            result.states[key] = "approved"
            result.approved.add(key)
        elif verdict.state == "skipped":
            result.states[key] = "skipped"
            result.skipped.add(key)
        elif verdict.state == "blocked":
            log.error(
                f"payment_approval_not_accepted handler={item.handler_name} — "
                f"{verdict.reason}; holding"
            )
            result.states[key] = "blocked"
            result.blocked.add(key)
            result.reason_code = "payment_approval_not_accepted"
        else:
            result.states[key] = "awaiting_approval"
            result.awaiting.add(key)
    return result
