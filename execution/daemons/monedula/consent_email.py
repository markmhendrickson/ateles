"""Monedula email payment-consent adapter over ``lib.approval``.

Owns pending-set fingerprinting, Design/UX reply grammar (attendance + marker
bind), and fail-closed approve semantics. Does NOT invent a second Gmail send
path — every outbound/inbound call goes through ``lib.approval.email_channel``.

State file (``.monedula_consent_email.json`` beside strandings state) stores
only ``{fingerprint, sent_at}`` — no addresses, bodies, or financial IDs.
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
)
from lib.approval.tokens import parse_verdict, subject_marker, token_for

log = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / ".monedula_consent_email.json"
CORRECTION_DEDUPE_FILE = Path(__file__).parent / ".monedula_consent_correction.json"

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


def match_key(item: PendingItem) -> str:
    """Stable identity shared by mail markers and the execute loop."""
    return item.token


def _load_mark(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_mark(
    path: Path,
    fingerprint: str,
    sent_at: float,
    *,
    executed: list[str] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "fingerprint": fingerprint,
        "sent_at": sent_at,
    }
    if executed is not None:
        payload["executed"] = sorted(set(executed))
    else:
        prior = _load_mark(path)
        prior_exec = prior.get("executed")
        if isinstance(prior_exec, list) and prior.get("fingerprint") == fingerprint:
            payload["executed"] = sorted({str(x) for x in prior_exec})
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def load_executed_tokens(path: Path | None = None) -> set[str]:
    """Tokens already paid for the current pending-set fingerprint."""
    mark = _load_mark(path or STATE_FILE)
    raw = mark.get("executed")
    if not isinstance(raw, list):
        return set()
    return {str(x) for x in raw}


def record_executed_tokens(
    tokens: set[str],
    *,
    state_path: Path | None = None,
) -> None:
    """Append successfully executed match tokens onto the consent mark."""
    path = state_path or STATE_FILE
    mark = _load_mark(path)
    fp = str(mark.get("fingerprint") or "")
    if not fp:
        return
    prior = load_executed_tokens(path)
    sent_at = float(mark.get("sent_at") or time.time())
    _save_mark(path, fp, sent_at, executed=sorted(prior | tokens))


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
    """Stable hash of the ordered pending set — changes only when the set does."""
    parts = sorted(
        f"{i.handler_name}|{i.amount_eur}|{i.label}|{i.payment_date}|{i.match_id}"
        for i in items
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


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
) -> list[PendingItem]:
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
            token = token_for(handler.name, session=session)
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
                )
            )
    return items


def build_request_body(
    items: list[PendingItem],
    *,
    superseded: bool,
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
    for item in items:
        marker = subject_marker(item.token)
        lines.append(
            f"- {item.handler_name} | {item.payment_date} | {item.label} | "
            f"EUR {item.amount_eur} | {marker}"
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
) -> tuple[dict[str, HandlerState], bool]:
    """Apply Design/UX verdict overlay on top of ``parse_verdict``.

    Returns (match_token → state, unrecognized_seen).
    SKIP is decisive per match token: once skipped, a later APPROVE/ATTENDED
    for the same token cannot revive it.
    """
    states: dict[str, HandlerState] = {
        match_key(i): "awaiting_approval" for i in items
    }
    decisions: dict[str, HandlerState] = {}
    unrecognized = False
    multi = len(items) > 1
    by_token = {i.token.upper(): i for i in items}

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
                unrecognized = True
                continue

            if first == "SKIP":
                if multi and not line_tok:
                    unrecognized = True
                    continue
                if not bound_tok or bound_tok not in by_token:
                    unrecognized = True
                    continue
                item = by_token[bound_tok]
                _decide(item, "skipped")
                continue

            if first == "ATTENDED":
                if multi and not line_tok:
                    unrecognized = True
                    continue
                if not bound_tok or bound_tok not in by_token:
                    unrecognized = True
                    continue
                item = by_token[bound_tok]
                if not item.is_calendar:
                    unrecognized = True
                    continue
                _decide(item, "approved")
                continue

            if first in ("APPROVE", "YES"):
                if multi and not line_tok:
                    unrecognized = True
                    continue
                if not bound_tok or bound_tok not in by_token:
                    unrecognized = True
                    continue
                item = by_token[bound_tok]
                if item.is_calendar:
                    # Calendar requires ATTENDED — bare APPROVE/YES holds.
                    unrecognized = True
                    continue
                # Use parse_verdict for non-calendar approve forms.
                verdict = parse_verdict(text, item.token)
                if verdict is True:
                    _decide(item, "approved")
                elif verdict is False:
                    _decide(item, "skipped")
                else:
                    unrecognized = True
                continue

            # Authenticated but unparseable content on a line that looks like
            # an attempt (contains APPROVE marker or known verbs mid-line).
            if line_tok or first:
                if any(
                    w in line.upper()
                    for w in ("APPROVE", "SKIP", "ATTENDED", "YES", "PAID")
                ):
                    unrecognized = True

    for key, state in decisions.items():
        states[key] = state
    return states, unrecognized


def request_and_collect(
    triggered: list[tuple[Any, list]],
    yesterday_str: str,
    *,
    state_path: Path | None = None,
    clear_failure_dedupe: Callable[[], None] | None = None,
) -> ConsentEmailResult:
    """Send-once consent email + statusful reply sweep for the pending set."""
    path = state_path or STATE_FILE
    result = ConsentEmailResult()

    if not triggered:
        clear_consent_state(path)
        if clear_failure_dedupe is not None:
            clear_failure_dedupe()
        return result

    items = build_pending_items(triggered, yesterday_str)
    result.pending_items = items
    fp = pending_fingerprint(items)
    mark = _load_mark(path)
    prior_fp = str(mark.get("fingerprint") or "")
    already_sent = prior_fp == fp and bool(prior_fp)

    if already_sent:
        log.info("consent_request_suppressed reason=unchanged_pending_set")
    else:
        if prior_fp and prior_fp != fp:
            _clear_mark(path)
        subject, body = build_request_body(items, superseded=bool(prior_fp and prior_fp != fp))
        ok = send_request(subject, body)
        result.send_count = 1
        if not ok:
            log.error("consent_request_send_failed — payments remain blocked; retry next tick")
            for item in items:
                key = match_key(item)
                result.states[key] = "blocked"
                result.blocked.add(key)
            result.reason_code = "consent_request_send_failed"
            result.channel_ok = False
            return result
        # Ordering invariant: persist mark ONLY after send_request returns True.
        _save_mark(path, fp, time.time(), executed=[])

    tokens = [i.token for i in items]
    sender_rejected = {"n": 0}

    def _on_rejected() -> None:
        sender_rejected["n"] += 1
        log.info("consent_reply_sender_rejected")

    outcome: ReadRepliesOutcome = read_replies_with_status(
        tokens,
        on_sender_rejected=_on_rejected,
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
    if not outcome.texts:
        for item in items:
            key = match_key(item)
            result.states[key] = "awaiting_approval"
            result.awaiting.add(key)
        log.info(f"awaiting_approval count={len(items)}")
        return result

    states, unrecognized = overlay_verdicts(items, outcome.texts)
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
        _maybe_send_correction(items, outcome.texts, path.parent)

    return result


def _maybe_send_correction(
    items: list[PendingItem],
    texts: list[str],
    state_dir: Path,
) -> None:
    """One deduped in-thread correction for unrecognized authenticated replies."""
    dedupe_path = state_dir / ".monedula_consent_correction.json"
    fp = pending_fingerprint(items)
    prior = _load_mark(dedupe_path)
    if prior.get("fingerprint") == fp:
        return
    # Best-effort: reply_in_thread needs a message id we do not always have.
    # Use send_request as fallback with Design correction copy (no PII).
    body = (
        "no payment was made; identify the unrecognized line and show the "
        "accepted form. Multi-item: ATTENDED/APPROVE/SKIP plus the item's "
        "[APPROVE-…] marker. Calendar items require ATTENDED."
    )
    # Prefer in-thread if we can discover nothing better — fallback to new mail.
    sent = send_request(
        f"[Ateles] Monedula consent correction {items[0].payment_date}",
        body,
    )
    if sent:
        _save_mark(dedupe_path, fp, time.time())
        log.info("consent_reply_unrecognized — correction sent (deduped)")
    # Silence if send fails; items stay held.
