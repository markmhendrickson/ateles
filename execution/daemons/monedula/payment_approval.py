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
Identity: both corrections must be made by the resolver principal configured as
         ``MONEDULA_PAYMENT_APPROVER_SUB`` / ``MONEDULA_PAYMENT_APPROVER_JKT``.

WHO MAY APPROVE (authenticated, not trusted from the snapshot)
--------------------------------------------------------------
A snapshot value is reducer output: it says what value won, not who wrote it,
so any principal able to write Neotoma could otherwise approve a payment.
``status`` and ``operator_answer`` are therefore each read through the
observation their field provenance names, using the SAME verifier the
checkpoint release path uses
(``lib.daemon_runtime.gating.read_authenticated_field_value``): the observation
must carry the configured approver sub and key thumbprint, a trusted AAuth
attribution tier and the entity's tenant. Missing, untrusted, wrong-principal
or unconfigured approver identity holds the payment. A later write to either
field by anyone else moves the provenance to that write and withdraws the
approval.

BOUND FIELDS ARE FINAL
----------------------
If a profile's amount, payee, reference, session or rail changes after the record
was raised, an approved record no longer matches and the item is held
("blocked") permanently: a bound field is never re-bound in place. The operator
rejects the stale record and a fresh record is raised under the new obligation
key once the terms are stable (a changed amount or payee changes the key).

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
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

ENTITY_TYPE = "payment_approval"
STATUS_AWAITING = "awaiting_operator"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0"
# An identifier lookup, not a listing: at most a handful of rows can share a key.
LOOKUP_LIMIT = 5

# Resolver identity (set by the coordinator session). Both are required; an
# unset value means no approval can ever be authenticated.
APPROVER_SUB_ENV = "MONEDULA_PAYMENT_APPROVER_SUB"
APPROVER_JKT_ENV = "MONEDULA_PAYMENT_APPROVER_JKT"

# The channel label ("checkpoint" before the rename; kept accepted as an alias).
CHANNEL = "payment_approval"
LEGACY_CHANNEL_ALIAS = "checkpoint"

# Fields the resolver writes, and the rest of the record's contract. The schema
# registration script derives its field list from these (no second copy).
STATUS_FIELD = "status"
ANSWER_FIELD = "operator_answer"
KEY_FIELD = "payment_key"
RAISED_AT_FIELD = "raised_at"

# Operator-visible alerting. A raised record that nobody resolves must never
# wait silently: one blocker when it is raised, one more when it has been
# awaiting longer than this many hours (default 24).
STALE_HOURS_ENV = "MONEDULA_APPROVAL_STALE_HOURS"
DEFAULT_STALE_HOURS = 24.0
Notify = Callable[..., None]  # notify(message, priority=..., dedupe_key=...)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def stale_hours() -> float:
    try:
        value = float(os.environ.get(STALE_HOURS_ENV, "") or DEFAULT_STALE_HOURS)
    except ValueError:
        return DEFAULT_STALE_HOURS
    return value if value > 0 else DEFAULT_STALE_HOURS

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


# Field -> (type, description). The record contract: the schema registration
# script and the contract test both read this, and raise_request builds records
# from the same names, so registration and writes cannot drift.
FIELD_SPECS: dict[str, tuple[str, str]] = {
    KEY_FIELD: ("string", "Obligation key (hash) identifying the payment; no payee data"),
    "handler": ("string", "Monedula handler (payment profile) name"),
    "session": ("string", "Calendar event id, or the session date when there is none"),
    "session_date": ("string", "Session date (Madrid-local, ISO)"),
    "amount": ("number", "Whole-unit amount to pay"),
    "currency": ("string", "ISO currency code"),
    "rail": ("string", "wise or btc"),
    "payee_display_name": ("string", "Payee name for display; never an IBAN or address"),
    "reference": ("string", "Payment reference; empty for BTC (no memo/OP_RETURN)"),
    STATUS_FIELD: ("string", "awaiting_operator, approved or rejected; written by the resolver"),
    ANSWER_FIELD: ("string", "The operator's literal answer; written by the resolver before status"),
    RAISED_AT_FIELD: ("string", "UTC ISO time the record was raised; drives the stale alert"),
    "blocking": ("boolean", "Always true"),
    "confidence_threshold": ("number", "Always 1.0"),
    "title": ("string", "Human-readable title"),
}
CANONICAL_NAME_FIELDS = [KEY_FIELD]
ENTITY_SCHEMA_VERSION = "1.0"


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
    if isinstance(snap, dict) and isinstance(snap.get("snapshot"), dict):
        snap = snap["snapshot"]  # /entities/{id} nests the snapshot one level
    return snap if isinstance(snap, dict) else {}


def _title(item: Any) -> str:
    return (
        f"Payment approval: {item.label} {item.payment_date} "
        f"[{str(item.obligation)[:12]}]"
    )


def find_entity(payment_key: str) -> dict | None:
    """The full stored entity (with ``entity_id`` and field provenance) for
    ``payment_key``, or None if there is none.

    A server-side filter on ``payment_key``, never a client-side scan of a
    capped listing. Raises :class:`ApprovalError` when Neotoma cannot be read,
    on an unexpected shape, or when more than one entity claims the key: an
    unreadable or ambiguous record is never "no record" (that would re-raise
    and could mask a prior approval), and never an approval.
    """
    data = _request(
        "POST",
        "/entities/query",
        {
            "entity_type": ENTITY_TYPE,
            # The API takes {"op", "value"} per field; a bare value is a 400.
            "snapshot_filters": {KEY_FIELD: {"op": "eq", "value": payment_key}},
            "limit": LOOKUP_LIMIT,
            "include_snapshots": True,
        },
    )
    if isinstance(data, dict):
        rows = data.get("entities") or data.get("items") or data.get("results") or []
    elif isinstance(data, list):
        rows = data
    else:
        raise ApprovalError("Neotoma query returned an unexpected shape")
    if not isinstance(rows, list):
        raise ApprovalError("Neotoma query returned an unexpected shape")
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and _snapshot(row).get(KEY_FIELD) == payment_key
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise ApprovalError("more than one payment_approval carries this payment_key")
    row = matches[0]
    entity_id = str(row.get("entity_id") or "")
    if not entity_id:
        raise ApprovalError("payment_approval row has no entity_id")
    # The listing row may not carry field provenance; the entity read does.
    full = _request("GET", f"/entities/{entity_id}")
    if not isinstance(full, dict):
        raise ApprovalError("Neotoma entity read returned an unexpected shape")
    full.setdefault("entity_id", entity_id)
    if _snapshot(full).get(KEY_FIELD) != payment_key:
        raise ApprovalError("payment_approval read does not match the key looked up")
    return full


def find_record(payment_key: str) -> dict | None:
    """The stored snapshot for ``payment_key`` (see :func:`find_entity`)."""
    entity = find_entity(payment_key)
    return None if entity is None else _snapshot(entity)


def approver_identity() -> tuple[str, str] | None:
    """The configured resolver (sub, JKT), or None when either is unset or the
    thumbprint is not an RFC 7638 SHA-256 value."""
    sub = os.environ.get(APPROVER_SUB_ENV, "").strip()
    jkt = os.environ.get(APPROVER_JKT_ENV, "").strip()
    if not sub or not re.fullmatch(r"[A-Za-z0-9_-]{43}", jkt):
        return None
    return sub, jkt


def authenticated_resolution(entity: dict) -> tuple[str, str | None] | None:
    """``(status, answer)`` read through each field's provenance observation,
    or None unless both came from the configured resolver.

    Reuses the checkpoint path's verifier
    (``gating.read_authenticated_field_value``). ``answer`` is None for an
    ``awaiting_operator`` record, which needs none.
    """
    from lib.daemon_runtime import gating

    identity = approver_identity()
    if identity is None:
        return None
    sub, jkt = identity
    entity_id = str(entity.get("entity_id") or "")
    if not entity_id:
        return None
    owner = gating.read_verified_entity_owner(entity_id, entity)
    if not owner:
        return None

    def read(field: str) -> str | None:
        return gating.read_authenticated_field_value(
            entity_id,
            entity,
            field=field,
            required_approver_sub=sub,
            required_approver_jkt=jkt,
            expected_user_id=owner,
        )

    status = read(STATUS_FIELD)
    if status is None:
        return None
    status = status.strip().lower()
    if status not in (STATUS_APPROVED, STATUS_REJECTED):
        return None
    answer = read(ANSWER_FIELD) if status == STATUS_APPROVED else None
    if status == STATUS_APPROVED and answer is None:
        return None
    return status, answer


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
        RAISED_AT_FIELD: _now().isoformat(),
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


def judge(item: Any, entity: dict, parse_reply: Callable[[str | None, list[str]], set[str]]) -> Verdict:
    """Decide what a stored entity permits for ``item``. No Neotoma writes.

    A resolution (approved or rejected) counts only when authenticated as the
    configured resolver; anything else holds the payment.
    """
    record = _snapshot(entity)
    status = str(record.get(STATUS_FIELD) or "").strip().lower()
    if status == STATUS_AWAITING or status == "":
        return Verdict("awaiting")
    if status not in (STATUS_APPROVED, STATUS_REJECTED):
        return Verdict("blocked", f"unrecognised status {status!r}")

    resolution = authenticated_resolution(entity)
    if resolution is None:
        return Verdict(
            "blocked",
            "resolution is not authenticated as the configured approver "
            f"({APPROVER_SUB_ENV}/{APPROVER_JKT_ENV}) — missing, untrusted, "
            "wrong principal, or approver not configured",
        )
    auth_status, answer = resolution
    if auth_status == STATUS_REJECTED:
        return Verdict("skipped", "rejected")

    want = expected_fields(item)
    if record.get(KEY_FIELD) != str(item.obligation):
        return Verdict("blocked", "record is for a different payment")
    for name in BOUND_FIELDS:
        if record.get(name) != want[name]:
            return Verdict("blocked", f"approval does not match the payment ({name})")

    if not isinstance(answer, str) or not answer.strip():
        return Verdict("blocked", "approved with no recorded operator answer")
    if str(item.handler_name) not in parse_reply(answer, [str(item.handler_name)]):
        return Verdict(
            "blocked",
            "recorded answer does not name the attended session (a bare yes does not approve)",
        )
    return Verdict("approved")


def _currency() -> str:
    from consent_email import CURRENCY

    return CURRENCY


def _is_stale(entity: dict) -> bool:
    raised = _snapshot(entity).get(RAISED_AT_FIELD)
    if not isinstance(raised, str) or not raised:
        return False
    try:
        when = datetime.fromisoformat(raised)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (_now() - when).total_seconds() > stale_hours() * 3600


def collect(
    triggered: list[tuple[Any, list]],
    yesterday_str: str,
    parse_reply: Callable[[str | None, list[str]], set[str]],
    notify: Notify | None = None,
):
    """Raise one pending record per payment (once) and read each back.

    ``notify`` (the daemon's notifier) makes every state the operator must act
    on visible: a blocker when a record is raised, a stale blocker when it has
    awaited longer than ``MONEDULA_APPROVAL_STALE_HOURS``, a blocker per held
    payment with the reason and the record's short key, and an info when a
    rejection skips a session. All are deduped per ``payment_key``.

    Returns a ``ConsentEmailResult`` so the existing execute loop in
    ``monedula.main`` is reused unchanged: ``approved`` only for an approved
    record whose bound fields match and whose recorded answer parses. Any
    Neotoma failure is isolated to its own item: that payment is held and the
    others proceed. Only when EVERY item is unreadable is the channel treated
    as down (``channel_ok`` False) so the tick is not claimed and the next one
    retries; the pending sessions stay in the journal.
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

    def say(item: Any, kind: str, message: str, priority: str) -> None:
        if notify is None:
            return
        try:
            notify(
                message,
                priority=priority,
                dedupe_key=f"monedula:payment_approval_{kind}:{item.obligation}",
                email_eligible=True,
            )
        except Exception as exc:  # noqa: BLE001 - alerting must not break the tick
            log.error(f"payment_approval notify failed: {type(exc).__name__}")

    def describe(item: Any) -> str:
        return (
            f"{item.handler_name} session {item.payment_date} "
            f"{item.amount_eur} {_currency()} [{str(item.obligation)[:12]}]"
        )

    unavailable = 0
    for item in items:
        key = match_key(item)
        raised_now = False
        try:
            entity = find_entity(str(item.obligation))
            if entity is None:
                raise_request(item)
                raised_now = True
                log.info(
                    f"payment_approval_raised handler={item.handler_name} "
                    f"session={item.match_id or item.payment_date}"
                )
                entity = find_entity(str(item.obligation))
                if entity is None:
                    raise ApprovalError("payment_approval did not read back after write")
            if raised_now:
                say(
                    item,
                    "raised",
                    f"monedula: payment approval needed for {describe(item)}. "
                    "It awaits your answer via the coordinator session; nothing "
                    "is paid until you answer.",
                    "blocker",
                )
        except ApprovalError as exc:
            # Fail closed for THIS item only; unrelated payees are unaffected.
            log.error(
                f"payment_approval_unavailable handler={item.handler_name} — "
                f"this payment held: {exc}"
            )
            unavailable += 1
            result.states[key] = "blocked"
            result.blocked.add(key)
            result.reason_code = "payment_approval_unavailable"
            continue
        verdict = judge(item, entity, parse_reply)
        if verdict.state == "approved":
            result.states[key] = "approved"
            result.approved.add(key)
        elif verdict.state == "skipped":
            result.states[key] = "skipped"
            result.skipped.add(key)
            say(
                item,
                "skipped",
                f"monedula: payment skipped (rejected) for {describe(item)}. "
                "No payment was made.",
                "info",
            )
        elif verdict.state == "blocked":
            log.error(
                f"payment_approval_not_accepted handler={item.handler_name} — "
                f"{verdict.reason}; holding"
            )
            result.states[key] = "blocked"
            result.blocked.add(key)
            if result.reason_code != "payment_approval_unavailable":
                result.reason_code = "payment_approval_not_accepted"
            say(
                item,
                "not_accepted",
                f"monedula: payment approval does not permit payment for "
                f"{describe(item)}: {verdict.reason}. That payment is held; "
                "others are unaffected. Fix the same record (for status "
                "approved with no answer, correct operator_answer on it).",
                "blocker",
            )
        else:
            result.states[key] = "awaiting_approval"
            result.awaiting.add(key)
            if _is_stale(entity):
                say(
                    item,
                    "stale",
                    f"monedula: payment approval for {describe(item)} has been "
                    f"awaiting your answer for over {stale_hours():g}h. Is the "
                    "coordinator session watching payment_approval records?",
                    "blocker",
                )
    if items and unavailable == len(items):
        # Nothing could be read at all (Neotoma down): treat as a channel
        # failure so the tick is not claimed and the next one retries.
        return block_all("payment_approval_unavailable")
    return result
