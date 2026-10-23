"""Gmail send / inbox-scan / in-thread-reply plumbing for email approvals.

Thin wrappers over the `gws gmail` CLI. Agent-agnostic: no per-agent state files,
no domain types — the caller owns dedup state and decides what a token maps to.

Every function is FAIL-OPEN: a missing gws CLI, unset env, non-zero exit, or a
transport error logs a warning and returns a benign empty/false value. None of
them raise into the caller's daemon loop.

MAILBOX: every gws call here runs as the swarm's OWN mailbox — the subprocess
gets ``GOOGLE_WORKSPACE_CLI_CONFIG_DIR=$ATELES_SWARM_GWS_CONFIG_DIR`` in its
env only (the calling process's env is never mutated, so e.g. Monedula's
calendar read keeps the operator's default config). When the swarm mailbox is
not configured no gws call is made at all: consent never falls back to the
operator's mailbox (ateles#1221).

Env contract:
  ATELES_NOTIFY_EMAIL  "1" arms the channel; anything else → every call no-ops
  OPERATOR_EMAIL       request recipient AND the verified reply --to
  ATELES_SWARM_EMAIL   the swarm mailbox's address: From: of every request
  ATELES_SWARM_GWS_CONFIG_DIR  gws config dir signed in as the swarm mailbox;
                       every gws call here runs with it (see MAILBOX above)
  ATELES_MAIL_AUTHSERV_ID  authserv-id of the receiving server whose
                       Authentication-Results are trusted (default
                       ``mx.google.com``)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Literal

log = logging.getLogger("ateles.approval.email")


@dataclass(frozen=True)
class ReadRepliesOutcome:
    """Statusful result of an inbox reply sweep.

    Distinguishes three states that the fail-open ``read_replies`` list cannot:
      - ``ok``              — transport succeeded; ``texts`` may be empty (no reply yet)
      - ``transport_error`` — ANY gws fetch in the sweep failed (triage, auth
                              metadata, or body); ``texts`` is always ``[]``,
                              never a partial set
      - ``disabled``        — ``ATELES_NOTIFY_EMAIL`` is not armed

    Payment-consent callers MUST use this form and treat non-``ok`` as blocked
    (fail-closed approve). Non-payment callers may keep using ``read_replies``,
    which remains fail-open (returns ``[]`` on transport error / disabled).
    """

    kind: Literal["ok", "transport_error", "disabled"]
    texts: list[str]
    detail: str = ""


def _strip_html(html: str) -> str:
    """Reduce an HTML email part to plain text good enough for verdict parsing.

    Only used for the HTML-only fallback (a reply with no plaintext part). Drops
    <script>/<style> blocks, turns block-level tag boundaries into newlines so a
    verb keeps leading its line, removes the remaining tags, and unescapes the
    handful of entities that show up in real replies. Not a general HTML
    sanitizer — just enough that `<p>approve v0.20.0</p>` reads as
    `approve v0.20.0`.
    """
    if not html:
        return ""
    # Remove script/style content entirely.
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    # Block-level boundaries → newlines so lines don't run together.
    html = re.sub(r"(?i)<\s*(br|/p|/div|/li|/tr|/h[1-6])\s*/?>", "\n", html)
    # Drop all remaining tags.
    html = re.sub(r"<[^>]+>", "", html)
    # Unescape the common entities seen in plain replies.
    for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        html = html.replace(ent, ch)
    # Collapse runs of blank lines / trailing spaces.
    lines = [ln.strip() for ln in html.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def email_enabled() -> bool:
    """True only when the operator has armed the email channel."""
    return os.environ.get("ATELES_NOTIFY_EMAIL", "0") == "1"


def operator_email() -> str:
    """The operator's address, or '' if unset."""
    return os.environ.get("OPERATOR_EMAIL", "").strip()


def _swarm_from() -> str:
    return os.environ.get("ATELES_SWARM_EMAIL", "").strip()


_GWS_CONFIG_ENV = "GOOGLE_WORKSPACE_CLI_CONFIG_DIR"


def _swarm_config_dir() -> str:
    return os.path.expanduser(
        os.environ.get("ATELES_SWARM_GWS_CONFIG_DIR", "").strip()
    )


def swarm_mailbox_problems() -> list[str]:
    """What is missing for a separate swarm mailbox, one entry per variable.

    Empty list ⇔ ``swarm_mailbox_configured()``. Each entry names the exact
    env var and what is wrong with it, so an operator who fixed one sees the
    other rather than the same generic text. Never contains a value.
    """
    problems: list[str] = []
    raw_swarm = _swarm_from()
    swarm = _parse_address(raw_swarm)
    operator = _parse_address(operator_email())
    if not raw_swarm:
        problems.append("ATELES_SWARM_EMAIL is not set (the swarm mailbox's address)")
    elif not swarm:
        problems.append("ATELES_SWARM_EMAIL is not a valid email address")
    elif operator and swarm == operator:
        problems.append(
            "ATELES_SWARM_EMAIL is the operator's own address; it must be the "
            "swarm's separate mailbox"
        )
    if not operator:
        problems.append("OPERATOR_EMAIL is not set or not a valid email address")
    cfg = _swarm_config_dir()
    if not cfg:
        problems.append(
            "ATELES_SWARM_GWS_CONFIG_DIR is not set (the gws config directory "
            "signed in as the swarm mailbox)"
        )
    elif not os.path.isdir(cfg):
        problems.append(
            "ATELES_SWARM_GWS_CONFIG_DIR does not point to an existing directory"
        )
    return problems


def _gws_env() -> dict[str, str] | None:
    """Subprocess env for a gws call as the swarm mailbox, or None (no call).

    A copy of the process env with the gws config dir pointed at the swarm
    mailbox. The process's own env is untouched. None when the swarm mailbox
    is not configured — callers then make NO gws call, so nothing ever runs
    against the operator's default mailbox.
    """
    if not swarm_mailbox_configured():
        log.warning(
            "approval: swarm mailbox not configured — no gws call made "
            "(consent never uses the operator's mailbox)"
        )
        return None
    env = dict(os.environ)
    env[_GWS_CONFIG_ENV] = _swarm_config_dir()
    return env


def swarm_mailbox_configured() -> bool:
    """True only when a SEPARATE swarm mailbox is configured (ateles#1221).

    Reply authentication (``sender_domain_authenticated``) needs the evidence
    Gmail's receiving server stamps on mail that arrives from another mailbox.
    When the request is sent from, and replies are read in, the operator's own
    mailbox, the operator's replies are self-sent and carry no such evidence,
    so every genuine approval is held. Consent callers that depend on an
    authenticated reply MUST check this and surface a blocker instead of
    sending a request that cannot be answered.

    FAIL CLOSED. Requires all of:
      - ``ATELES_SWARM_EMAIL`` parses as an address
      - it differs from ``OPERATOR_EMAIL`` (same address = same mailbox)
      - ``ATELES_SWARM_GWS_CONFIG_DIR`` names an existing directory (the
        swarm mailbox's own gws credentials)
    """
    return not swarm_mailbox_problems()


def _gws() -> str | None:
    return shutil.which("gws")


def gws_json(args: list[str], timeout: int = 45) -> Any:
    """Run a `gws` command and parse its JSON, tolerating the banner preamble.

    gws prints a keyring/format banner before the JSON, so we start parsing at
    the first '{' or '['. Returns None on any failure (fail-open).
    """
    gws = _gws()
    if not gws:
        return None
    env = _gws_env()
    if env is None:
        return None
    try:
        r = subprocess.run([gws, *args], capture_output=True, text=True,
                           timeout=timeout, env=env)
        if r.returncode != 0:
            log.warning(f"gws {args[:2]} failed: {(r.stderr or '').strip()[:160]}")
            return None
        out = r.stdout or ""
        start = min([i for i in (out.find("{"), out.find("[")) if i >= 0] or [-1])
        if start < 0:
            return None
        return json.loads(out[start:])
    except Exception as exc:  # noqa: BLE001
        log.warning(f"gws json error: {exc}")
        return None


def send_request(subject: str, body: str, to: str | None = None) -> bool:
    """Send an approval-request email with an explicit subject (the token lives
    in the subject). Returns True on success, False on any failure (fail-open).

    `to` defaults to OPERATOR_EMAIL. Gated by ATELES_NOTIFY_EMAIL.
    """
    if not email_enabled():
        return False
    recipient = (to or operator_email()).strip()
    gws = _gws()
    if not gws or not recipient:
        return False
    env = _gws_env()
    if env is None:
        return False
    # From the swarm mailbox To the operator: the reply then arrives in the
    # swarm mailbox as genuinely inbound mail the receiving server authenticates.
    cmd = [gws, "gmail", "+send", "--to", recipient,
           "--subject", subject, "--body", body, "--from", _swarm_from()]
    try:
        r = subprocess.run(cmd, timeout=60, capture_output=True, text=True,
                           env=env)
        if r.returncode != 0:
            log.warning(f"approval email send failed (rc={r.returncode}): "
                        f"{(r.stderr or '').strip()[:160]}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"approval email error: {exc}")
        return False


def _parse_address(raw: str) -> str:
    """Return the bare email address from a From: header, lowercased.

    Handles the forms gws actually returns: a bare `op@example.com` and the
    display-name form `Mark H <op@example.com>`. Returns "" when no
    well-formed address can be extracted — the caller MUST treat "" as
    unverified, never as a match.

    Uses stdlib `parseaddr`, which implements the RFC 5322 grammar, rather than
    a hand-rolled regex: the address-in-display-name spoof
    (`op@example.com <attacker@evil.example>`) resolves to the REAL address
    under the grammar, and to the wrong one under naive substring matching.
    """
    _, addr = parseaddr(raw or "")
    addr = (addr or "").strip().lower()
    # parseaddr is lenient — it returns bare words like "not-an-address"
    # unchanged. Require the structural minimum of an address before we are
    # willing to compare it to the operator's.
    if addr.count("@") != 1:
        return ""
    local, _, domain = addr.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return ""
    return addr


def sender_is_operator(from_header: str) -> bool:
    """True only when `from_header` is provably the operator's address.

    FAIL CLOSED. Every uncertain case returns False:
      - OPERATOR_EMAIL unset or unparseable  → no principal is configured, so
        nothing can be authorized
      - From: absent, empty, or malformed    → the sender is unverifiable
      - address mismatch                     → not the operator

    Comparison is on the PARSED address, whole and exact, so neither a lookalike
    domain (`op@example.com.evil.example`) nor an address hidden in a display
    name can satisfy it.

    NOTE ON STRENGTH: this is the ADDRESS-BINDING half of sender identity
    only. A From header is sender-chosen text, so a match here is necessary
    but never sufficient: ``read_replies_with_status`` additionally requires
    ``sender_domain_authenticated`` (Gmail's own recorded DMARC/aligned-DKIM
    pass for the operator's domain) before a reply's verdict counts.
    """
    operator = _parse_address(operator_email())
    if not operator:
        log.warning("approval: OPERATOR_EMAIL unset or unparseable — refusing "
                    "to treat any reply as approved (fail closed)")
        return False
    sender = _parse_address(from_header)
    if not sender:
        return False
    return sender == operator


# The only receiving server whose authentication verdict we accept: by default
# Gmail's own inbound MTA, which stamps this authserv-id on mail it receives
# for the mailbox. Config, not code, so a move off Gmail is an env change.
DEFAULT_AUTHSERV_ID = "mx.google.com"


def trusted_authserv_id() -> str:
    """``ATELES_MAIL_AUTHSERV_ID`` (lowercased), or the Gmail default."""
    value = os.environ.get("ATELES_MAIL_AUTHSERV_ID", "").strip().lower()
    return value or DEFAULT_AUTHSERV_ID


def _strip_comments(value: str) -> str:
    """Remove RFC 5322 parenthesised comments (nesting-aware).

    Comments in Authentication-Results are free text; nothing inside one may
    count as a result. An unbalanced comment leaves the value unparseable,
    which the caller treats as not authenticated.
    """
    out: list[str] = []
    depth = 0
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return ""
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "" if depth else "".join(out)


def _auth_result_authorizes(header_value: str, domain: str) -> bool:
    """True only when ONE Authentication-Results value, stamped by Google's
    receiving server, records that ``domain`` authorized the message.

    Accepted evidence (either suffices):
      - ``dmarc=pass`` with ``header.from=<domain>``
      - ``dkim=pass`` with ``header.d=<domain>`` or ``header.i=...@<domain>``
        (exact domain — strict alignment with the From domain)

    Everything else — another authserv-id, failing or absent results, a pass
    for another domain, text only inside a comment, or a value that does not
    parse — is False.
    """
    if not header_value or not domain:
        return False
    value = _strip_comments(" ".join(header_value.split()))
    if not value:
        return False
    parts = [p.strip() for p in value.split(";")]
    # First element is the authserv-id, optionally followed by a version.
    authserv = (parts[0].split() or [""])[0].lower()
    expected = trusted_authserv_id()
    if authserv != expected:
        log.warning(
            f"approval: Authentication-Results stamped by {authserv[:80]!r}, "
            f"expected {expected!r} (ATELES_MAIL_AUTHSERV_ID) — not trusted"
        )
        return False
    for resinfo in parts[1:]:
        tokens = resinfo.split()
        if not tokens or "=" not in tokens[0]:
            continue
        method, _, result = tokens[0].lower().partition("=")
        if result != "pass":
            continue
        props: dict[str, str] = {}
        for tok in tokens[1:]:
            k, sep, v = tok.partition("=")
            if sep:
                props.setdefault(k.lower(), v.strip().strip('"').lower())
        if method == "dmarc" and props.get("header.from") == domain:
            return True
        if method == "dkim":
            if props.get("header.d") == domain:
                return True
            ident = props.get("header.i", "")
            if "@" in ident and ident.rpartition("@")[2] == domain:
                return True
    return False


def _fetch_auth_results(message_id: str) -> list[str] | None:
    """Read a message's Authentication-Results header values, in header order.

    Returns None when the metadata could not be read (transport failure or an
    unexpected response shape) — the caller treats that as a failed read, not
    as "unauthenticated". Returns [] when the message was read and carries no
    Authentication-Results header.
    """
    params = json.dumps({
        "userId": "me",
        "id": message_id,
        "format": "metadata",
        "metadataHeaders": ["Authentication-Results"],
    })
    data = gws_json(["gmail", "users", "messages", "get", "--params", params],
                    timeout=30)
    if not isinstance(data, dict):
        return None
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return None
    headers = payload.get("headers")
    if headers is None:
        headers = []
    if not isinstance(headers, list):
        return None
    values: list[str] = []
    for h in headers:
        if not isinstance(h, dict):
            return None
        if str(h.get("name") or "").strip().lower() == "authentication-results":
            values.append(str(h.get("value") or ""))
    return values


def sender_domain_authenticated(auth_results: list[str]) -> bool:
    """True only when the TOPMOST Authentication-Results header was stamped by
    Google's receiving server and records the operator's domain authorizing the
    message (DMARC pass, or DKIM pass aligned with the operator's domain).

    FAIL CLOSED: no header, a failing result, a non-Google or unparseable top
    header, or an unset/unparseable OPERATOR_EMAIL all return False. Only the
    topmost header is considered because the receiving server prepends its own;
    a lower header carrying the same authserv-id did not come from this receipt.
    """
    operator = _parse_address(operator_email())
    if not operator:
        return False
    domain = operator.rpartition("@")[2]
    if not auth_results:
        return False
    return _auth_result_authorizes(auth_results[0], domain)


def read_replies_with_status(
    tokens: list[str],
    max_msgs: int = 40,
    on_reply_message: Callable[[str, str], None] | None = None,
    on_sender_rejected: Callable[[], None] | None = None,
    on_unauthenticated_operator_reply: Callable[[], None] | None = None,
) -> ReadRepliesOutcome:
    """Statusful inbox sweep — distinguish empty-ok from transport failure.

    Same triage/+read flow as ``read_replies``, but returns ``ReadRepliesOutcome``
    so payment-consent callers can fail-closed on transport error without
    conflating it with "no reply yet".

    ``on_sender_rejected`` (optional) fires when a candidate reply fails
    ``sender_is_operator`` — no address argument (PII-safe for Monedula logs).

    ``on_unauthenticated_operator_reply`` (optional) fires when a reply's
    From: address IS the operator's but ``sender_domain_authenticated`` does
    not pass — a reply that may be genuine but cannot be proven, which is a
    different condition from a non-operator sender and needs a different
    operator-facing message. The reply is still ignored. When this callback
    is not given, ``on_sender_rejected`` fires instead (prior behaviour).
    """
    if not email_enabled():
        return ReadRepliesOutcome(kind="disabled", texts=[], detail="ATELES_NOTIFY_EMAIL")
    if not tokens:
        return ReadRepliesOutcome(kind="ok", texts=[], detail="")
    if not _gws():
        return ReadRepliesOutcome(
            kind="transport_error", texts=[], detail="gws_cli_missing"
        )
    if not swarm_mailbox_configured():
        # Replies are read only from the swarm mailbox, never from the
        # operator's (ateles#1221). Unconfigured is a failed read, not "none".
        return ReadRepliesOutcome(
            kind="transport_error", texts=[], detail="swarm_mailbox_unconfigured"
        )

    texts: list[str] = []
    seen_ids: set[str] = set()
    saw_transport_error = False
    transport_detail = ""

    for token in tokens:
        data = gws_json(["gmail", "+triage", "--format", "json", "--max",
                         str(max_msgs), "--query", f"newer_than:3d {token}"])
        if data is None:
            # gws_json fail-opens to None on any transport/parse failure.
            # For statusful callers that is distinct from "zero matching msgs".
            saw_transport_error = True
            transport_detail = "gws_triage_failed"
            continue
        msgs: list[dict] = []
        if isinstance(data, dict):
            msgs = data.get("messages") or data.get("results") or []
        elif isinstance(data, list):
            msgs = data
        for m in msgs:
            mid = str(m.get("id") or "")
            subject = str(m.get("subject") or "")
            if not mid or mid in seen_ids or not subject.upper().startswith("RE:"):
                continue
            # AUTHORIZATION, not merely authentication. The triage query is a
            # full-text mailbox search for the token, so ANY message carrying
            # the token string lands here — and the token rides in the subject
            # line, where every forward, auto-reply and CC'd thread carries it.
            # Possession of it proves the sender SAW the request, never that
            # they are the operator. Reject before the `+read` so an untrusted
            # body never enters the process and the callback never fires for it.
            sender = str(m.get("from") or m.get("sender") or m.get("From") or "")
            if not sender_is_operator(sender):
                log.warning(
                    "approval: ignoring reply — sender is not the "
                    "operator (token present but unverified sender)"
                )
                if on_sender_rejected is not None:
                    try:
                        on_sender_rejected()
                    except Exception as exc:  # noqa: BLE001
                        log.warning(f"on_sender_rejected callback failed: {exc}")
                continue
            # The From address is text the sender chose. Require Gmail's own
            # record that the operator's domain authorized this message before
            # its body is read or its verdict counted. Unreadable metadata is a
            # failed read (the whole sweep fails below); readable but absent or
            # failing authentication is not the operator.
            auth_results = _fetch_auth_results(mid)
            if auth_results is None:
                saw_transport_error = True
                transport_detail = "gws_auth_metadata_failed"
                continue
            if not sender_domain_authenticated(auth_results):
                log.warning(
                    "approval: ignoring reply — sender domain authentication "
                    "absent or not a pass (address matched, identity unknown)"
                )
                cb = on_unauthenticated_operator_reply or on_sender_rejected
                if cb is not None:
                    try:
                        cb()
                    except Exception as exc:  # noqa: BLE001
                        log.warning(f"reply-rejected callback failed: {exc}")
                continue
            seen_ids.add(mid)
            body_data = gws_json(["gmail", "+read", "--id", mid, "--headers",
                                  "--format", "json"], timeout=30)
            if body_data is None:
                saw_transport_error = True
                transport_detail = "gws_read_failed"
                continue
            body = ""
            if isinstance(body_data, dict):
                # Prefer plaintext (where the operator's verdict + the quoted
                # token live). Fall back to the HTML part only if that is all gws
                # returns — an HTML-only reply must not read as an empty body and
                # silently drop the approval (the ateles#286 failure mode: a live
                # release approval was lost because the reader missed the
                # plaintext key).
                body = str(body_data.get("body_text")
                           or body_data.get("body")
                           or body_data.get("text")
                           or body_data.get("plain") or "")
                if not body:
                    # HTML-only: strip tags so the verb leads its line, otherwise
                    # parse_verdict (which keys off the FIRST word of a line) sees
                    # "<p>approve" and returns None — a non-empty-but-unparseable
                    # body would still drop the approval. Tag-strip makes an
                    # HTML-only `approve <version>` reply actually register.
                    raw_html = str(body_data.get("body_html") or "")
                    if raw_html:
                        body = _strip_html(raw_html)
                    else:
                        body = str(body_data.get("snippet") or "")
            if on_reply_message is not None:
                try:
                    on_reply_message(token, mid)
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"on_reply_message callback failed: {exc}")
            texts.append(f"{subject}\n{body}")

    # Any fetch failure within the sweep fails the WHOLE read: a verdict set
    # built from only the messages that loaded is incomplete, and acting on it
    # could honour one reply while missing a later one that changes it.
    if saw_transport_error:
        return ReadRepliesOutcome(
            kind="transport_error", texts=[], detail=transport_detail or "gws_failed"
        )
    return ReadRepliesOutcome(kind="ok", texts=texts, detail="")


def read_replies(
    tokens: list[str],
    max_msgs: int = 40,
    on_reply_message: Callable[[str, str], None] | None = None,
) -> list[str]:
    """Return the full text (subject + body) of recent replies carrying any token.

    Thin fail-open wrapper over ``read_replies_with_status``: returns ``.texts``
    on ``ok``, else ``[]``. Prefer ``read_replies_with_status`` when the caller
    must distinguish transport failure from an empty inbox (payment consent).
    """
    outcome = read_replies_with_status(
        tokens, max_msgs=max_msgs, on_reply_message=on_reply_message
    )
    if outcome.kind == "ok":
        return outcome.texts
    return []


def reply_in_thread(message_id: str, body: str,
                    attachments: list[str] | None = None,
                    cwd: str | None = None) -> bool:
    """Reply to `message_id` so a confirmation lands in the SAME thread as the
    operator's approval. Uses `gws gmail +reply`, which handles threading.

    Passes --to OPERATOR_EMAIL EXPLICITLY: `+reply` can misaddress when replying
    to a message the operator themselves sent (a known gws quirk — it would send
    the confirmation back to the operator's own thread partner rather than the
    operator). Fail-open: returns False so the caller can fall back to a
    standalone email.

    `attachments` are staged into `cwd` (a temp dir under it) and passed as
    repo-relative --attach paths, then cleaned up.
    """
    if not email_enabled():
        return False
    recipient = operator_email()
    gws = _gws()
    if not gws or not message_id or not recipient:
        return False

    env = _gws_env()
    if env is None:
        return False

    base = Path(cwd) if cwd else Path.cwd()
    cmd = [gws, "gmail", "+reply", "--message-id", message_id,
           "--body", body, "--to", recipient, "--from", _swarm_from()]

    staged: list[Path] = []
    stage_dir = base / ".approval_attach_tmp"
    for src in (attachments or []):
        try:
            if not os.path.exists(src):
                continue
            stage_dir.mkdir(parents=True, exist_ok=True)
            dst = stage_dir / Path(src).name
            shutil.copyfile(src, dst)
            staged.append(dst)
            cmd += ["--attach", str(dst.relative_to(base))]
        except Exception as exc:  # noqa: BLE001
            log.warning(f"could not stage attachment {src}: {exc}")

    try:
        r = subprocess.run(cmd, timeout=60, capture_output=True, text=True,
                           cwd=str(base), env=env)
        if r.returncode != 0:
            log.warning(f"+reply failed (rc={r.returncode}): "
                        f"{(r.stderr or '').strip()[:160]}")
            return False
        log.info(f"confirmation replied in thread ({message_id}).")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"+reply error: {exc}")
        return False
    finally:
        for p in staged:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            if stage_dir.exists() and not any(stage_dir.iterdir()):
                stage_dir.rmdir()
        except OSError:
            pass
