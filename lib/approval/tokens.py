"""Approval tokens and reply-verdict parsing.

The token identifies exactly ONE approvable item-instance. `parse_verdict` reads
the operator's free-text reply and decides APPROVE / SKIP / no-verdict, with the
safety rules that a payment/release must never be approved by accident.
"""

from __future__ import annotations

import hashlib
import re

# Subject-line marker that carries the token, e.g. "[APPROVE-1A2B3C4D]".
# The operator replies APPROVE / SKIP in the body; the token rides along in the
# quoted subject of their reply, so they never type it.
SUBJECT_MARKER = "APPROVE-"


def token_for(entity_id: str, session: str = "") -> str:
    """Return a short, stable token identifying one item-instance.

    Stable across runs for the same (entity_id, session), so a re-sent request
    matches the operator's earlier reply — but DIFFERENT for the next session.

    The `session` component is essential for a RECURRING obligation. Hashing the
    entity id alone yields one token for all time, and because replies are matched
    from the inbox over a rolling window, a prior instance's "APPROVE" reply still
    sitting in the inbox would re-approve the NEXT instance — something the
    operator never consented to (observed 2026-07-22 in Monedula: a stale reply
    re-approved a later session at a different amount). Callers with recurrence
    MUST pass a per-instance discriminator (e.g. a due_date) as `session`.

    Omitting `session` preserves the legacy entity-only token — use only where no
    recurrence is in scope (a one-shot release approval, say).
    """
    basis = f"{entity_id}|{session}" if session else entity_id
    return hashlib.sha256(basis.encode()).hexdigest()[:8].upper()


def subject_marker(token: str) -> str:
    """The bracketed subject tag for a token, e.g. '[APPROVE-1A2B3C4D]'."""
    return f"[{SUBJECT_MARKER}{token}]"


def parse_verdict(text: str, token: str) -> bool | None:
    """Return True (approve), False (skip), or None (no verdict) for a reply.

    Accepts a bare "APPROVE"/"SKIP"/"YES" (the token is matched separately, from
    the quoted subject), or the explicit target-bound forms. Any other authored
    text makes the reply ambiguous and cannot approve.

    The verb must fill the operator's own (unquoted) instruction line; a
    longer sentence is not a verdict. Quoted instructions never count.

    `text` should be the full reply text (subject + body); matching is
    case-insensitive, including the token.
    """
    up = text.upper()
    tok = (token or "").upper()

    # Strip the quoted original. Gmail plain-text replies put the operator's own
    # words FIRST, then "On <date> ... wrote:" followed by the quoted request
    # (which itself contains the words APPROVE and SKIP). Only read the part the
    # operator actually typed, or a quoted instruction would count as a verdict.
    body = up
    for marker in ("\nON ", "\n-----ORIGINAL", "\n________"):
        idx = body.find(marker)
        if idx > 0:
            body = body[:idx]
            break

    lines: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith(">") or "JUST HIT REPLY" in s or "TO DECLINE" in s:
            continue
        # Ignore the subject line itself (it always contains "APPROVE-<token>").
        if s.startswith("RE:") or s.startswith("[ATELES]"):
            continue
        if s:
            lines.append(s)
    if not lines:
        return None
    target = re.escape(tok)
    optional_v = rf"V?{re.escape(tok.lstrip('V'))}" if tok.startswith("V") else target
    suffix = rf"(?:{target}|{optional_v})"
    approve = rf"(?:APPROVE|YES)(?:\s+{suffix})?[.!]?|APPROVE-{target}[.!]?"
    skip = rf"SKIP(?:\s+{suffix})?[.!]?|SKIP-{target}[.!]?"
    if len(lines) != 1:
        return False if any(re.fullmatch(skip, s) for s in lines) else None
    if re.fullmatch(skip, lines[0]):
        return False
    if re.fullmatch(approve, lines[0]):
        return True
    return None
