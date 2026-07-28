"""Shared operator email reply-to-approve pattern for the Ateles swarm.

Extracted from Monedula's proven payment-approval flow (PR #249) so every agent
that needs an operator's per-item approval (payments, releases, …) uses ONE
implementation of the safety-critical bits — token scoping, verdict parsing,
and the gws send/scan plumbing — instead of drifting copies.

Design (operator-approved 2026-07-27, plan ent_5502c7abd91c589966bcbeb6):
  - email-primary + Telegram break-glass everywhere (gated by ATELES_NOTIFY_EMAIL)
  - unified APPROVE / SKIP verbs with a stable per-item token [APPROVE-<token>]
    carried in the subject line, so the operator never types a code
  - SKIP wins on ambiguity; quoted-original text is stripped before parsing
  - confirmations reply IN-THREAD to the operator's approval message

Env contract (read at call time, never hardcoded):
  ATELES_NOTIFY_EMAIL  "1" to arm the email channel; anything else disables it
  OPERATOR_EMAIL       recipient of approval requests + the verified reply --to
  ATELES_SWARM_EMAIL   optional From: for outbound (the swarm's own address)

Every function is FAIL-OPEN: a missing gws CLI, unset env, or a transport error
returns a benign empty/false value and logs a warning — it never raises into the
caller's daemon loop.
"""

from .tokens import token_for, parse_verdict
from .email_channel import (
    email_enabled,
    operator_email,
    send_request,
    read_replies,
    reply_in_thread,
)

__all__ = [
    "token_for",
    "parse_verdict",
    "email_enabled",
    "operator_email",
    "send_request",
    "read_replies",
    "reply_in_thread",
]
