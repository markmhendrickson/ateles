#!/usr/bin/env python3
"""PreToolUse hook — block Gmail writes that can SEND without operator approval.

The hazard (observed 2026-07-08, recurred 2026-07-31): `gws gmail users drafts
update` is NOT a safe staging operation. Re-supplying `raw` + `threadId` on an
update can consume the draft and deliver it — the message leaves without any
explicit send command. On 2026-07-31 a reply to an external contact was created
as a draft, reported to the operator as staged and unsent, and then sent by a
later `drafts update` when the operator asked for edits. The operator had given
no send approval.

The standing rule is that sending on the operator's behalf requires an explicit
per-message yes in chat. Instructions alone did not hold that line: the guidance
existed in memory (feedback_gws_draft_update_can_send) and was skipped anyway,
because the send is a side effect of an operation that reads as safe.

This hook makes the failure structurally impossible rather than
instruction-dependent. It blocks, at PreToolUse:

  - `gws gmail users drafts update`  — the misfiring call; edit by building a
    NEW draft instead (drafts create), which cannot deliver.
  - `gws gmail users drafts send`    — an explicit send, still operator-gated.
  - `gws gmail users messages send`  — likewise.
  - `gws gmail +send` / `+reply` / `+reply-all` / `+forward` — helper wrappers
    that deliver.

Explicitly NOT covered (allowed):
  - `gws gmail users drafts create` / `get` / `list` — staging and reads.
  - All Gmail reads (`+read`, `messages list`, `messages get`, `threads get`).
  - Every non-Gmail `gws` service (calendar, drive, sheets, ...).

Compound commands (`&&`, `;`, `|`, newlines) are split into segments and
evaluated independently, so a send hidden after an innocuous first segment is
still caught.

Approval flow: when the operator HAS approved this specific message in chat, set
ATELES_ALLOW_GMAIL_SEND=1 for that one invocation, e.g.

    ATELES_ALLOW_GMAIL_SEND=1 gws gmail users messages send --params ... --json ...

That keeps the gate on by default while leaving an explicit, auditable path for
an approved send. The env var is deliberately per-command, not exported.

Fail-open: any error or unparseable input → exit 0 (never block a session on our
own bug).
"""
import json
import os
import re
import sys

OVERRIDE_ENV = "ATELES_ALLOW_GMAIL_SEND"

# Each pattern matches a Gmail invocation that can deliver mail.
# Matched against a whitespace-normalized segment.
SENDING_PATTERNS = [
    (
        re.compile(r"\bgws\b.*\bgmail\b.*\busers\b.*\bdrafts\b.*\bupdate\b"),
        "drafts update",
        "`drafts update` can CONSUME the draft and send it — this is the call that "
        "delivered an unapproved reply on 2026-07-31. To edit a staged draft, build a "
        "NEW draft with `drafts create` (optionally deleting the old one afterwards); "
        "create cannot deliver.",
    ),
    (
        re.compile(r"\bgws\b.*\bgmail\b.*\busers\b.*\bdrafts\b.*\bsend\b"),
        "drafts send",
        "This delivers the draft.",
    ),
    (
        re.compile(r"\bgws\b.*\bgmail\b.*\busers\b.*\bmessages\b.*\bsend\b"),
        "messages send",
        "This delivers a message.",
    ),
    (
        re.compile(r"\bgws\b.*\bgmail\b.*\+(send|reply-all|reply|forward)\b"),
        "gmail helper send",
        "The `+send`/`+reply`/`+reply-all`/`+forward` helpers deliver mail.",
    ),
]

SEGMENT_SPLIT = re.compile(r"&&|[;\n|]")

# An override must PREFIX the sending segment itself (optionally after `env`),
# which is the documented usage. Anchored at the segment start so an override
# on an unrelated earlier segment cannot vouch for a later send.
OVERRIDE_PREFIX = re.compile(rf"^(?:env\s+)?{OVERRIDE_ENV}=1\b")


def log(msg: str) -> None:
    try:
        print(f"[gmail_send_gate] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — logging must never block
        pass


def deny(reason: str) -> int:
    """Emit a PreToolUse deny and block."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 2


def guidance(label: str, why: str) -> str:
    return (
        f"Refused: `{label}` can send mail on the operator's behalf without an "
        f"explicit approval.\n\n{why}\n\n"
        f"Sending requires a per-message yes from the operator in chat — approval for "
        f"one message never carries to the next. Once the operator has approved THIS "
        f"message, re-run the command with the override prefixed:\n"
        f"  {OVERRIDE_ENV}=1 <the same command>\n\n"
        f"Background: ateles memory `feedback_gws_draft_update_can_send` "
        f"(hazard 2026-07-08, recurred 2026-07-31)."
    )


def find_sending_call(command: str):
    """Return (label, why) for the first unapproved sending segment, or None.

    The override is evaluated PER SEGMENT: it must prefix the sending segment
    itself. An override prefixing some earlier, unrelated segment does not
    vouch for a later send — otherwise
    `ATELES_ALLOW_GMAIL_SEND=1 echo ok && gws gmail ... send` would smuggle a
    send past the gate.
    """
    for segment in SEGMENT_SPLIT.split(command):
        normalized = " ".join(segment.split())
        if not normalized:
            continue
        for pattern, label, why in SENDING_PATTERNS:
            if pattern.search(normalized):
                if OVERRIDE_PREFIX.match(normalized):
                    log(f"inline override prefixes this {label}; allowing")
                    break
                return label, why
    return None


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return 0
    if not raw.strip():
        return 0

    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or "gmail" not in command:
        return 0

    if os.environ.get(OVERRIDE_ENV, "").strip() == "1":
        log("override set in the hook environment; allowing an approved send")
        return 0

    # The inline form (ATELES_ALLOW_GMAIL_SEND=1 gws ...) is the documented
    # approval path. It is evaluated per segment inside find_sending_call so
    # that it only vouches for the segment it actually prefixes.
    hit = find_sending_call(command)
    if hit is None:
        return 0

    label, why = hit
    log(f"blocking {label}")
    return deny(guidance(label, why))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never break a session
        log(f"internal error, failing open: {exc}")
        sys.exit(0)
