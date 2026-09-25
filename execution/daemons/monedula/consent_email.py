"""Monedula email payment-consent adapter over ``lib.approval``.

Owns pending-set fingerprinting, Design/UX reply grammar (attendance + marker
bind), and fail-closed approve semantics. Does NOT invent a second Gmail send
path — every outbound/inbound call goes through ``lib.approval.email_channel``.

State file (``.monedula_consent_email.json`` beside strandings state) stores
only ``{fingerprint, generation, sent_at}`` — no addresses, bodies, or
financial IDs. It is cleared when a pending set ends; it is NOT where payment
execution is recorded. That lives in the durable, never-cleared
``payment_journal`` (obligation-keyed intent/outcome + consumed tokens).

Consent binding (``docs/foundation/payments.md#a-payments-approver-is-shown-exactly-what-the-verifier-signed``):
each approval token is derived from the obligation key — handler, period /
instance, payee, amount, currency — plus the request generation issued by the
journal. An approval therefore matches only the exact terms it was shown, and
only the current request; changed terms or a superseded request produce new
tokens, so an old reply cannot authorize anything and a fresh request is sent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from lib.approval.email_channel import (
    ReadRepliesOutcome,
    read_replies_with_status,
    send_request,
    swarm_mailbox_configured,
)
from lib.approval.tokens import parse_verdict, subject_marker, token_for

import payment_journal

log = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / ".monedula_consent_email.json"
CORRECTION_DEDUPE_FILE = Path(__file__).parent / ".monedula_consent_correction.json"
UNAUTH_DEDUPE_NAME = ".monedula_consent_unauthenticated.json"

# Email consent cannot succeed without a separate swarm mailbox (ateles#1221):
# a reply is accepted only with authentication evidence, which the operator's
# own self-sent replies never carry. Held with this reason instead of sending
# a request nobody can answer.
REASON_NEEDS_SWARM_MAILBOX = "consent_email_needs_swarm_mailbox"
NEEDS_SWARM_MAILBOX_HINT = (
    "email consent needs a separate swarm mailbox; see ateles#1221 "
    "(set ATELES_SWARM_EMAIL and ATELES_SWARM_GWS_CONFIG_DIR to the swarm's "
    "own mailbox). No consent request was sent; payments held"
)

# A reply from the operator's address that could not be authenticated.
# Distinct from a non-operator sender: it may be a genuine reply.
REASON_REPLY_UNAUTHENTICATED = "consent_reply_unauthenticated"

# Every Monedula profile settles in EUR (``amount_eur``; the Wise leg quotes
# EUR→EUR). Bound explicitly so a future non-EUR profile changes the key.
CURRENCY = "EUR"

# The first generation a fresh journal issues. Only a default for callers that
# build items to inspect tokens; ``request_and_collect`` always passes the
# generation it actually issued.
FIRST_GENERATION = 1


def journal_path(state_path: Path | None = None) -> Path:
    """The durable payment journal lives beside the consent state."""
    return (state_path or STATE_FILE).parent / payment_journal.JOURNAL_NAME

HandlerState = Literal[
    "awaiting_approval", "approved", "skipped", "blocked"
]


@dataclass
class PendingItem:
    handler_name: str
    label: str
    amount_eur: int
    payment_date: str
    is_calendar: bool
    token: str
    match_id: str = ""
    match: Any = None
    handler: Any = None
    # Hash of (handler, period/instance, payee, amount, currency) — the dedup
    # key payments.md requires. Never contains raw payee/account data.
    obligation: str = ""
    generation: int = 0


@dataclass
class ConsentEmailResult:
    """Per-match (token) states plus aggregate reason and execute sets for ``main()``.

    Keys in ``states`` / ``approved`` / ``skipped`` / ``blocked`` / ``awaiting``
    are ``PendingItem.token`` values — one identity per marked mail line, not
    per handler. Multi-match handlers therefore authorize siblings independently.
    """

    states: dict[str, HandlerState] = field(default_factory=dict)
    reason_code: str | None = None
    approved: set[str] = field(default_factory=set)
    skipped: set[str] = field(default_factory=set)
    blocked: set[str] = field(default_factory=set)
    awaiting: set[str] = field(default_factory=set)
    send_count: int = 0
    channel_ok: bool = True
    pending_items: list[PendingItem] = field(default_factory=list)
    generation: int = 0
    # Replies from the operator's address that failed authentication this sweep.
    unauthenticated_replies: int = 0


def match_key(item: PendingItem) -> str:
    """Stable identity shared by mail markers and the execute loop."""
    return item.token


def _load_mark(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_mark(path: Path, fingerprint: str, sent_at: float, *, generation: int) -> None:
    payload: dict[str, Any] = {
        "fingerprint": fingerprint,
        "generation": int(generation),
        "sent_at": sent_at,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _clear_mark(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        log.warning(f"could not clear consent mark: {exc}")


def clear_consent_state(state_path: Path | None = None) -> None:
    """Clear pending-set fingerprint mark (empty triggered set)."""
    _clear_mark(state_path or STATE_FILE)


def pending_fingerprint(items: list[PendingItem]) -> str:
    """Stable hash of the pending set's terms — changes when any item's
    handler, period, payee, amount, currency or label does. Independent of the
    request generation, so an unchanged set is not re-sent."""
    parts = sorted(f"{i.obligation}|{i.label}" for i in items)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def payee_identity(handler: Any) -> str:
    """The payee this handler would pay, as a string for hashing only.

    Uses the handler's own ``payee_identity()`` (which resolves the payee the
    same way ``execute`` does) when present; otherwise every payee-bearing
    profile field. An unresolvable payee binds as ``unresolved`` — if it later
    resolves to anything, the terms differ and consent must be re-requested.
    Never logged or persisted raw; only its hash leaves this function's caller.
    """
    fn = getattr(handler, "payee_identity", None)
    if callable(fn):
        try:
            value = fn()
        except Exception:
            value = None
        return f"resolved|{value}" if value else "unresolved"
    profile = getattr(handler, "profile", None)
    fields = (
        "payment_type",
        "btc_address",
        "wise_iban",
        "wise_recipient_name",
        "contact_id",
        "contact_category",
        "contact_platform",
    )
    return "profile|" + "|".join(str(getattr(profile, f, "") or "") for f in fields)


def obligation_key(
    handler: Any,
    payment_date: str,
    match_id: str,
) -> str:
    """Dedup key for one obligation: handler, period/instance, payee, amount,
    currency (``docs/foundation/payments.md#the-dedup-key-and-what-it-is-keyed-on``).
    Recomputed from the live handler, so it also serves as the execute-time
    check that the terms consented to are still the terms that would be paid."""
    _label, amount, _is_cal = _profile_fields(handler)
    basis = "|".join(
        [
            str(handler.name),
            str(payment_date),
            str(match_id),
            payee_identity(handler),
            str(amount),
            CURRENCY,
        ]
    )
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def _profile_fields(handler: Any) -> tuple[str, int, bool]:
    profile = getattr(handler, "profile", None)
    label = str(getattr(profile, "label", None) or handler.name)
    amount = int(getattr(profile, "amount_eur", 0) or 0)
    keywords = getattr(profile, "calendar_keywords", None) or []
    is_calendar = bool(keywords)
    return label, amount, is_calendar


def build_pending_items(
    triggered: list[tuple[Any, list]],
    yesterday_str: str,
    generation: int = FIRST_GENERATION,
) -> list[PendingItem]:
    """One item per match. Each token binds the obligation's exact terms and
    the request generation, so it approves nothing else."""
    items: list[PendingItem] = []
    for handler, matches in triggered:
        label, amount, is_calendar = _profile_fields(handler)
        match_list = matches or [None]
        multi = len(match_list) > 1
        for idx, match in enumerate(match_list):
            match_id = ""
            if multi:
                match_id = str(
                    (match or {}).get("event_id")
                    or (match or {}).get("id")
                    or idx
                )
            session = yesterday_str if not match_id else f"{yesterday_str}:{match_id}"
            obligation = obligation_key(handler, yesterday_str, match_id)
            token = token_for(
                handler.name,
                session=f"{session}|{obligation}|g{int(generation)}",
            )
            items.append(
                PendingItem(
                    handler_name=handler.name,
                    label=label,
                    amount_eur=amount,
                    payment_date=yesterday_str,
                    is_calendar=is_calendar,
                    token=token,
                    match_id=match_id,
                    match=match,
                    handler=handler,
                    obligation=obligation,
                    generation=int(generation),
                )
            )
    return items


def build_request_body(
    items: list[PendingItem],
    *,
    superseded: bool,
    settled: set[str] | None = None,
    unknown: set[str] | None = None,
) -> tuple[str, str]:
    """Plain-text subject + body per Design copy. Never instructs PAID."""
    first_marker = subject_marker(items[0].token) if items else ""
    subject = f"[Ateles] Monedula consent {items[0].payment_date if items else ''} {first_marker}".strip()
    lines: list[str] = []
    if superseded:
        lines.append(
            "This is the CURRENT consent request. Earlier requests for a "
            "different pending set are superseded and must not authorize payment."
        )
        lines.append("")
    lines.append("Pending payment(s) — reply to approve or skip. No payment moves until you reply.")
    lines.append("")
    settled = settled or set()
    unknown = unknown or set()
    for item in items:
        marker = subject_marker(item.token)
        note = ""
        if item.obligation in settled:
            note = " | already paid — no reply needed"
        elif item.obligation in unknown:
            note = " | outcome unknown — held for review, will not be retried"
        lines.append(
            f"- {item.handler_name} | {item.payment_date} | {item.label} | "
            f"{CURRENCY} {item.amount_eur} | {marker}{note}"
        )
    lines.append("")
    multi = len(items) > 1
    if multi:
        lines.append(
            "Multi-item replies: every line MUST include its marker, e.g. "
            "ATTENDED [APPROVE-…] / APPROVE [APPROVE-…] / SKIP [APPROVE-…]."
        )
        lines.append(
            "Each marked line authorizes or skips only that match; other matches "
            "for the same handler stay blocked until their own line."
        )
        lines.append(
            "Bare verbs without a marker are ignored (no payment executes)."
        )
    else:
        lines.append(
            "Single-item: reply ATTENDED (calendar) or APPROVE (one-off) or SKIP; "
            "the subject carries the marker."
        )
        lines.append("Calendar items require ATTENDED — bare APPROVE/YES will not approve.")
    lines.append("Do not use PAID as a pre-execution approval verb.")
    lines.append(
        "Silence, unread inbox, or channel failure keeps payments blocked "
        "(not declined / not skipped)."
    )
    return subject, "\n".join(lines)


_MARKER_RE = re.compile(r"\[APPROVE-([0-9A-F]+)\]", re.IGNORECASE)


def _operator_lines(text: str) -> list[str]:
    """Unquoted operator lines from a reply (subject + body)."""
    up = text
    body = up
    for marker in ("\nOn ", "\nON ", "\n-----Original", "\n-----ORIGINAL", "\n________"):
        idx = body.find(marker)
        if idx > 0:
            body = body[:idx]
            break
    lines: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith(">"):
            continue
        if s.upper().startswith("RE:") or s.upper().startswith("[ATELES]"):
            continue
        if "JUST HIT REPLY" in s.upper() or "TO DECLINE" in s.upper():
            continue
        lines.append(s)
    return lines


def _line_marker_token(line: str) -> str | None:
    m = _MARKER_RE.search(line)
    return m.group(1).upper() if m else None


def overlay_verdicts(
    items: list[PendingItem],
    texts: list[str],
    unrecognized_lines: list[str] | None = None,
) -> tuple[dict[str, HandlerState], bool]:
    """Apply Design/UX verdict overlay on top of ``parse_verdict``.

    Returns (match_token → state, unrecognized_seen).
    SKIP is decisive per match token: once skipped, a later APPROVE/ATTENDED
    for the same token cannot revive it.

    ``unrecognized_lines``, when given, collects each operator line that was
    not understood (in order, without duplicates) so the correction mail can
    quote it back.
    """
    states: dict[str, HandlerState] = {
        match_key(i): "awaiting_approval" for i in items
    }
    decisions: dict[str, HandlerState] = {}
    unrecognized = False
    multi = len(items) > 1
    by_token = {i.token.upper(): i for i in items}

    def _flag(line: str) -> None:
        nonlocal unrecognized
        unrecognized = True
        if unrecognized_lines is not None and line not in unrecognized_lines:
            unrecognized_lines.append(line)

    def _decide(item: PendingItem, state: HandlerState) -> None:
        key = match_key(item)
        if decisions.get(key) == "skipped":
            return
        if state == "skipped":
            decisions[key] = "skipped"
            return
        decisions[key] = state

    for text in texts:
        # Subject may carry a marker for single-item bare verbs.
        subject_tok = None
        for line in text.splitlines()[:3]:
            if line.upper().startswith("RE:") or "[APPROVE-" in line.upper():
                subject_tok = _line_marker_token(line)
                break

        for line in _operator_lines(text):
            first = (line.split() or [""])[0].upper().rstrip(".!")
            line_tok = _line_marker_token(line)
            bound_tok = line_tok or (None if multi else subject_tok)

            if first == "PAID":
                _flag(line)
                continue

            if first == "SKIP":
                if multi and not line_tok:
                    _flag(line)
                    continue
                if not bound_tok or bound_tok not in by_token:
                    _flag(line)
                    continue
                item = by_token[bound_tok]
                _decide(item, "skipped")
                continue

            if first == "ATTENDED":
                if multi and not line_tok:
                    _flag(line)
                    continue
                if not bound_tok or bound_tok not in by_token:
                    _flag(line)
                    continue
                item = by_token[bound_tok]
                if not item.is_calendar:
                    _flag(line)
                    continue
                _decide(item, "approved")
                continue

            if first in ("APPROVE", "YES"):
                if multi and not line_tok:
                    _flag(line)
                    continue
                if not bound_tok or bound_tok not in by_token:
                    _flag(line)
                    continue
                item = by_token[bound_tok]
                if item.is_calendar:
                    # Calendar requires ATTENDED — bare APPROVE/YES holds.
                    _flag(line)
                    continue
                # Use parse_verdict for non-calendar approve forms.
                verdict = parse_verdict(text, item.token)
                if verdict is True:
                    _decide(item, "approved")
                elif verdict is False:
                    _decide(item, "skipped")
                else:
                    _flag(line)
                continue

            # Authenticated but unparseable content on a line that looks like
            # an attempt (contains APPROVE marker or known verbs mid-line).
            if line_tok or first:
                if any(
                    w in line.upper()
                    for w in ("APPROVE", "SKIP", "ATTENDED", "YES", "PAID")
                ):
                    _flag(line)

    for key, state in decisions.items():
        states[key] = state
    return states, unrecognized


def request_and_collect(
    triggered: list[tuple[Any, list]],
    yesterday_str: str,
    *,
    state_path: Path | None = None,
    clear_failure_dedupe: Callable[[], None] | None = None,
    on_unauthenticated_reply: Callable[[str, str], None] | None = None,
) -> ConsentEmailResult:
    """Send-once consent email + statusful reply sweep for the pending set.

    ``on_unauthenticated_reply(message, dedupe_key)`` is called at most ONCE
    per consent request (pending-set fingerprint + generation) when a reply
    from the operator's address fails authentication, so the operator learns
    the reply was received but not accepted. It never approves anything.
    """
    path = state_path or STATE_FILE
    result = ConsentEmailResult()

    if not triggered:
        clear_consent_state(path)
        if clear_failure_dedupe is not None:
            clear_failure_dedupe()
        return result

    jpath = journal_path(path)

    def _block_all(items_: list[PendingItem], reason: str) -> ConsentEmailResult:
        for item in items_:
            key = match_key(item)
            result.states[key] = "blocked"
            result.blocked.add(key)
        result.reason_code = reason
        result.channel_ok = False
        return result

    # Terms (and therefore the fingerprint) do not depend on the generation.
    probe = build_pending_items(triggered, yesterday_str)

    # Without a separate swarm mailbox no reply can ever be authenticated, so
    # a request would only pretend to be actionable. Hold, send nothing, and
    # let the caller surface one deduped blocker (ateles#1221).
    if not swarm_mailbox_configured():
        log.error(
            f"{REASON_NEEDS_SWARM_MAILBOX} — request and replies would share "
            "the operator's mailbox, so no reply can be authenticated; "
            "no request sent, payments held"
        )
        result.pending_items = probe
        return _block_all(probe, REASON_NEEDS_SWARM_MAILBOX)
    fp = pending_fingerprint(probe)
    mark = _load_mark(path)
    prior_fp = str(mark.get("fingerprint") or "")

    # The journal is the durable record of what was paid and the source of
    # request generations. Unreadable → hold everything (principles.md#5).
    try:
        journal = payment_journal.load(jpath)
        if not jpath.exists() and prior_fp:
            # A consent request is outstanding but the record of payments
            # made against it is gone: nothing can prove what already paid.
            raise payment_journal.JournalError(
                "payment journal missing while a consent request is outstanding"
            )
    except payment_journal.JournalError as exc:
        log.error(f"payment_journal_unreadable — payments held: {exc}")
        result.pending_items = probe
        return _block_all(probe, "payment_journal_unreadable")

    try:
        mark_gen = int(mark.get("generation") or 0)
    except (TypeError, ValueError):
        mark_gen = 0
    # Reuse the outstanding request only if it is for these exact terms AND
    # is the latest generation the journal issued. Anything else (terms
    # changed, request superseded, mark lost or damaged) re-requests under a
    # fresh generation, which no earlier reply can match.
    already_sent = (
        bool(prior_fp)
        and prior_fp == fp
        and mark_gen > 0
        and mark_gen == int(journal["generation"])
    )

    settled = {
        i.obligation
        for i in probe
        if payment_journal.obligation_state(journal, i.obligation)
        == payment_journal.STATE_DONE
    }
    unknown = {
        i.obligation
        for i in probe
        if payment_journal.obligation_state(journal, i.obligation)
        == payment_journal.STATE_INTENT
    }

    if already_sent:
        generation = mark_gen
        items = build_pending_items(triggered, yesterday_str, generation)
        result.pending_items = items
        result.generation = generation
        log.info("consent_request_suppressed reason=unchanged_pending_set")
    else:
        if prior_fp:
            _clear_mark(path)
        try:
            generation = payment_journal.next_generation(jpath)
        except payment_journal.JournalError as exc:
            log.error(f"payment_journal_unwritable — payments held: {exc}")
            result.pending_items = probe
            return _block_all(probe, "payment_journal_unreadable")
        items = build_pending_items(triggered, yesterday_str, generation)
        result.pending_items = items
        result.generation = generation
        subject, body = build_request_body(
            items,
            superseded=bool(prior_fp),
            settled=settled,
            unknown=unknown,
        )
        ok = send_request(subject, body)
        result.send_count = 1
        if not ok:
            log.error("consent_request_send_failed — payments remain blocked; retry next tick")
            return _block_all(items, "consent_request_send_failed")
        # Ordering invariant: persist mark ONLY after send_request returns True.
        _save_mark(path, fp, time.time(), generation=generation)

    tokens = [i.token for i in items]
    sender_rejected = {"n": 0}
    unauthenticated = {"n": 0}

    def _on_rejected() -> None:
        sender_rejected["n"] += 1
        log.info("consent_reply_sender_rejected")

    def _on_unauthenticated() -> None:
        unauthenticated["n"] += 1
        log.warning(REASON_REPLY_UNAUTHENTICATED)

    outcome: ReadRepliesOutcome = read_replies_with_status(
        tokens,
        on_sender_rejected=_on_rejected,
        on_unauthenticated_operator_reply=_on_unauthenticated,
    )

    if outcome.kind in ("transport_error", "disabled"):
        log.error(
            f"consent_reply_read_failed kind={outcome.kind} detail={outcome.detail!r}"
        )
        for item in items:
            key = match_key(item)
            result.states[key] = "blocked"
            result.blocked.add(key)
        result.reason_code = "consent_reply_read_failed"
        result.channel_ok = False
        return result

    # kind == ok
    if unauthenticated["n"]:
        result.unauthenticated_replies = unauthenticated["n"]
        result.reason_code = REASON_REPLY_UNAUTHENTICATED
        _maybe_notify_unauthenticated(
            items,
            generation,
            path.parent,
            on_unauthenticated_reply,
        )

    if not outcome.texts:
        for item in items:
            key = match_key(item)
            result.states[key] = "awaiting_approval"
            result.awaiting.add(key)
        log.info(f"awaiting_approval count={len(items)}")
        return result

    unrecognized_lines: list[str] = []
    states, unrecognized = overlay_verdicts(
        items, outcome.texts, unrecognized_lines=unrecognized_lines
    )
    result.states = states
    for key, state in states.items():
        if state == "approved":
            result.approved.add(key)
        elif state == "skipped":
            result.skipped.add(key)
        elif state == "blocked":
            result.blocked.add(key)
        else:
            result.awaiting.add(key)

    if unrecognized:
        result.reason_code = "consent_reply_unrecognized"
        _maybe_send_correction(
            items,
            unrecognized_lines,
            path.parent,
            no_reply_needed=settled | unknown,
        )

    return result


def unauthenticated_reply_message(payment_date: str) -> str:
    """Operator-facing notice: a reply arrived but was not accepted."""
    return (
        f"monedula: a reply to the payment consent request for {payment_date} "
        "came from your address but could not be authenticated, so it was NOT "
        "accepted and no payment was made. Payments stay held. "
        "Most likely the request was sent from, and your reply read in, the "
        "same mailbox; email consent needs a separate swarm mailbox (see "
        "ateles#1221 — ATELES_SWARM_EMAIL and ATELES_SWARM_GWS_CONFIG_DIR). "
        "Once that is set up, reply again to the current consent request from "
        "your own mailbox."
    )


def _maybe_notify_unauthenticated(
    items: list[PendingItem],
    generation: int,
    state_dir: Path,
    notify: Callable[[str, str], None] | None,
) -> None:
    """Notify at most once per consent request (fingerprint + generation)."""
    fp = pending_fingerprint(items)
    dedupe_path = state_dir / UNAUTH_DEDUPE_NAME
    prior = _load_mark(dedupe_path)
    if prior.get("fingerprint") == fp and prior.get("generation") == int(generation):
        return
    if notify is None:
        return
    dedupe_key = f"monedula:{REASON_REPLY_UNAUTHENTICATED}:{fp}:g{int(generation)}"
    try:
        notify(unauthenticated_reply_message(items[0].payment_date), dedupe_key)
    except Exception as exc:  # noqa: BLE001 — a notify failure must not approve
        log.warning(f"{REASON_REPLY_UNAUTHENTICATED} notify failed: {exc}")
        return
    try:
        _save_mark(dedupe_path, fp, time.time(), generation=generation)
    except OSError as exc:
        log.warning(f"could not persist unauthenticated-reply dedupe: {exc}")


_QUOTED_LINES_MAX = 5
_QUOTED_LINE_CHARS = 200


def accepted_reply_form(item: PendingItem) -> str:
    """The exact reply lines that answer one item: its verb plus its marker."""
    marker = subject_marker(item.token)
    verb = "ATTENDED" if item.is_calendar else "APPROVE"
    return f"{verb} {marker}   or   SKIP {marker}"


def build_correction_body(
    items: list[PendingItem],
    unrecognized_lines: list[str],
    *,
    no_reply_needed: set[str] | None = None,
) -> tuple[str, str]:
    """Operator-facing correction: what was not understood, that nothing was
    paid, and the exact accepted form for each item still awaiting a reply."""
    date = items[0].payment_date if items else ""
    subject = f"[Ateles] Monedula consent correction {date}: reply not understood"
    skip = no_reply_needed or set()
    open_items = [i for i in items if i.obligation not in skip] or list(items)

    lines: list[str] = [
        f"Your reply to the Monedula payment consent request for {date} "
        "was not understood, so no payment was made. The payments below are "
        "still waiting for your answer.",
        "",
    ]
    quoted = [ln for ln in unrecognized_lines if ln.strip()][:_QUOTED_LINES_MAX]
    if quoted:
        lines.append("Not understood:")
        for ln in quoted:
            text = ln.strip()
            if len(text) > _QUOTED_LINE_CHARS:
                text = text[: _QUOTED_LINE_CHARS - 1] + "…"
            lines.append(f"  > {text}")
        lines.append("")
    lines.append(
        "To answer, reply to the consent request with one of these lines for "
        "each payment, copied exactly (keep the code in brackets):"
    )
    lines.append("")
    for item in open_items:
        lines.append(f"{item.label} ({CURRENCY} {item.amount_eur}):")
        lines.append(f"  {accepted_reply_form(item)}")
    lines.append("")
    if any(i.is_calendar for i in open_items):
        lines.append(
            "Sessions from your calendar need ATTENDED; APPROVE or YES will "
            "not approve them."
        )
    if any(not i.is_calendar for i in open_items):
        lines.append("One-off payments need APPROVE.")
    lines.append(
        "SKIP means do not pay. PAID is not an answer. "
        "Until you reply, nothing is paid."
    )
    return subject, "\n".join(lines)


def _maybe_send_correction(
    items: list[PendingItem],
    unrecognized_lines: list[str],
    state_dir: Path,
    *,
    no_reply_needed: set[str] | None = None,
) -> None:
    """One deduped correction for unrecognized authenticated replies."""
    dedupe_path = state_dir / ".monedula_consent_correction.json"
    fp = pending_fingerprint(items)
    prior = _load_mark(dedupe_path)
    if prior.get("fingerprint") == fp:
        return
    # reply_in_thread needs a message id we do not always have, so the
    # correction goes out as its own mail.
    subject, body = build_correction_body(
        items, unrecognized_lines, no_reply_needed=no_reply_needed
    )
    sent = send_request(subject, body)
    if sent:
        _save_mark(dedupe_path, fp, time.time(), generation=0)
        log.info("consent_reply_unrecognized — correction sent (deduped)")
    # Silence if send fails; items stay held.
