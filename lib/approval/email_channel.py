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
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Literal

log = logging.getLogger("ateles.approval.email")


@dataclass(frozen=True)
class ApprovalReply:
    message_id: str
    subject: str
    body: str = ""
    authenticated: bool = True
    correlation_target: str | None = None


@dataclass(frozen=True)
class ReadRepliesOutcome:
    """Statusful result of an inbox reply sweep.

    Distinguishes three states that the fail-open ``read_replies`` list cannot:
      - ``ok``              — transport succeeded; ``texts`` may be empty (no reply yet)
      - ``transport_error`` — ANY gws fetch in the sweep failed (triage, auth
                              metadata, or body); ``texts`` is always ``[]``,
                              never a partial set. ``replies`` may hold readable,
                              authenticated messages for correction-only handling.
      - ``disabled``        — ``ATELES_NOTIFY_EMAIL`` is not armed

    Payment-consent callers MUST use this form and treat non-``ok`` as blocked
    (fail-closed approve). Non-payment callers may keep using ``read_replies``,
    which remains fail-open (returns ``[]`` on transport error / disabled).
    """

    kind: Literal["ok", "transport_error", "disabled"]
    texts: list[str]
    detail: str = ""
    replies: tuple[ApprovalReply, ...] = ()
    rejected_replies: tuple[ApprovalReply, ...] = ()
    unreadable_replies: tuple[ApprovalReply, ...] = ()


_QUOTE_HEADER = re.compile(r"(?i)^(?:from|sent|to|subject):\s*\S")
_ATTRIBUTION = re.compile(r"(?i)^on\s+.+\bwrote:\s*$")
_FORWARD = re.compile(
    r"(?i)^(?:-{2,}\s*(?:original|forwarded) message\s*-{2,}|"
    r"begin forwarded message:|_{5,})"
)

_NON_RENDERED = frozenset({
    "head", "title", "script", "style", "template", "noscript", "object",
    "iframe", "svg", "math", "canvas", "audio", "video", "select", "option",
    "datalist", "textarea", "meta",
})
_RENDERED_PROSE = frozenset({
    "html", "body", "p", "div", "span", "blockquote", "a", "b", "strong",
    "i", "em", "u", "s", "del", "ins", "mark", "small", "big", "sub", "sup",
    "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "font", "center", "pre", "code",
    "q", "cite", "address", "section", "article", "header", "footer", "main",
    "aside", "details", "summary", "dl", "dt", "dd",
})
_HIDING_CSS = re.compile(
    r"(?i)(?:^|[;{])\s*(?:display\s*:\s*none\b|"
    r"visibility\s*:\s*(?:hidden|collapse)\b|"
    r"(?:opacity|font-size|max-height|max-width|text-indent)\s*:\s*"
    r"(?:0(?:px|pt|em|rem|%)?|-[1-9]\d{2,}(?:px|pt|em|rem)?)\b|"
    r"mso-hide\s*:\s*all\b|(?:clip|clip-path|content-visibility)\s*:|"
    r"(?:color|-webkit-text-fill-color)\s*:\s*transparent\b|"
    r"line-height\s*:\s*0(?:px|pt|em|rem|%)?\b|"
    r"filter\s*:\s*opacity\s*\(\s*0(?:%)?\s*\)|"
    r"transform\s*:\s*(?:scale\s*\(\s*0\s*\)|translate[XY]?\s*\(\s*-?[1-9]\d{2,})|"
    r"@media\b|@import\b)"
)


def _hiding_style(style: str) -> bool:
    """Reject CSS that can conceal authored text without a renderer."""
    css = re.sub(r"/\*.*?\*/", "", style, flags=re.DOTALL).lower()
    if _HIDING_CSS.search(css):
        return True
    declarations = dict((name.strip(), value.strip().removesuffix("!important").strip())
                        for name, value in re.findall(r"(?:^|[;{])\s*([\w-]+)\s*:\s*([^;{}]+)", css))
    if (declarations.get("overflow") in {"hidden", "clip"} and
            any(declarations.get(dim, "").startswith("0") for dim in ("height", "width"))):
        return True
    if declarations.get("position") in {"absolute", "fixed"} and any(
            re.match(r"-[1-9]\d{2,}(?:px|pt|em|rem)?\b", declarations.get(side, ""))
            for side in ("left", "top", "right", "bottom")):
        return True
    foreground = declarations.get("color")
    background = declarations.get("background-color") or declarations.get("background")
    if foreground and background and foreground == background:
        return True
    whites = {"white", "#fff", "#ffffff", "rgb(255,255,255)", "rgb(255, 255, 255)"}
    return bool(foreground in whites and background in whites)


def _authored_plain(text: str) -> str:
    """Keep authored lines; an attribution, forward or signature ends the reply."""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if (_ATTRIBUTION.match(stripped) or _FORWARD.match(stripped)
                or _QUOTE_HEADER.match(stripped) or stripped in ("--", "-- ")):
            break
        if stripped.startswith(">"):
            continue
        if stripped:
            lines.append(stripped)
    return "\n".join(lines)


class _AuthoredHTML(HTMLParser):
    """Extract visible authored text while discarding quoted subtrees."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []
        self.quote_depth = 0
        self.non_rendered_depth = 0
        self.structured = False
        self.body_depth = 0
        self.boundary_seen = False
        self.terminal = False
        self.hr_at: int | None = None
        self.unsafe = False

    _VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img",
                       "input", "link", "meta", "param", "source", "track", "wbr"})
    _MAX_STACK = 64

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_by_name = dict(attrs)
        if tag not in _RENDERED_PROSE and tag not in _NON_RENDERED and tag not in self._VOID:
            self.unsafe = True
        if ("hidden" in attrs_by_name or
                (attrs_by_name.get("aria-hidden") or "").strip().lower() == "true" or
                _hiding_style(attrs_by_name.get("style") or "") or
                (tag == "link" and "stylesheet" in (attrs_by_name.get("rel") or "").lower().split())):
            self.unsafe = True
        if tag in {"html", "head", "body"}:
            if not self.structured:
                self.parts.clear()
            self.structured = True
        if tag == "body":
            self.body_depth += 1
        classes = set((attrs_by_name.get("class") or "").lower().split())
        element_id = (attrs_by_name.get("id") or "").lower()
        quote = (tag == "blockquote"
                 or bool(classes & {"gmail_quote", "gmail_extra", "yahoo_quoted",
                                    "protonmail_quote", "msoquote", "outlookmessageheader"})
                 or element_id in {"divrplyfwdmsg", "appendonsend"}
                 or (tag == "div" and (attrs_by_name.get("type") or "").lower() == "cite"))
        if ("gmail_attr" in classes or "moz-cite-prefix" in classes
                or element_id in {"divrplyfwdmsg", "appendonsend"}):
            self.terminal = True
            self.boundary_seen = True
        if tag in self._VOID:
            if tag == "hr" and self.quote_depth == 0 and not self.non_rendered_depth:
                self.hr_at = len(self.parts)
                self.parts.append("\n")
            elif tag == "br" and self.quote_depth == 0 and not self.non_rendered_depth:
                self.parts.append("\n")
            return
        if len(self.stack) >= self._MAX_STACK:
            self.unsafe = True
            return
        if quote:
            self.quote_depth += 1
            self.boundary_seen = True
        if tag in _NON_RENDERED:
            self.non_rendered_depth += 1
        self.stack.append((tag, quote))
        if tag in ("p", "div", "li", "tr") and self.quote_depth == 0 and not self.non_rendered_depth:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # HTML ignores the slash on a non-void element: `<blockquote/>` OPENS a
        # blockquote. Treat it as a start tag only, so a quote container written
        # that way keeps hiding what follows (and an unclosed one is unreadable).
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._VOID:
            return
        if not self.stack or self.stack[-1][0] != tag:
            self.unsafe = True
            return
        _, quote = self.stack.pop()
        if quote:
            self.quote_depth -= 1
        if tag in _NON_RENDERED:
            self.non_rendered_depth -= 1
        if tag == "body":
            self.body_depth -= 1
        if tag in ("p", "div", "li", "tr") and self.quote_depth == 0 and not self.non_rendered_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1][0] == "style" and _hiding_style(data):
            self.unsafe = True
        if self.terminal or self.quote_depth or self.non_rendered_depth or (self.structured and not self.body_depth):
            return
        if self.hr_at is not None and _QUOTE_HEADER.search(data.strip()):
            self.parts = self.parts[:self.hr_at]
            self.terminal = True
            self.boundary_seen = True
            return
        self.parts.append(data)

    def unknown_decl(self, data: str) -> None:
        self.unsafe = True


def _extract_authored_html(html: str) -> tuple[str, bool]:
    """Return authored text only when the HTML can be separated safely."""
    parser = _AuthoredHTML()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — malformed HTML cannot grant approval
        return "", False
    if parser.unsafe or parser.stack or parser.rawdata:
        return "", False
    return _authored_plain("".join(parser.parts)), parser.boundary_seen


def _strip_html(html: str) -> str:
    """Plain authored text from HTML, retained for callers of this helper."""
    return _extract_authored_html(html)[0]


def _unmarked_notification_copy(body: str) -> bool:
    """An HTML-only copy of an instruction with its token has no safe boundary."""
    return bool(re.search(
        r"(?i)\breply\s+(?:approve|attended|to approve|with one line per payment)\b",
        body,
    )
                and re.search(r"(?i)(?:swarm-approve:|release-approve:|\[APPROVE-[0-9A-F]+\])", body))


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


def gws_config_dir_env(*, allow_ambient: bool = False,
                       require_existing: bool = False) -> dict[str, str]:
    """The gws config selection for a child process, as an env fragment.

    Non-approval reads may explicitly opt into the ambient directory. Approval
    sends pass ``require_existing=True`` so a missing
    swarm directory cannot select the operator's mailbox or gws default.
    ``_gws_env`` separately checks the full swarm mailbox configuration.
    """
    swarm_dir = _swarm_config_dir()
    if swarm_dir and (not require_existing or os.path.isdir(swarm_dir)):
        return {_GWS_CONFIG_ENV: swarm_dir}
    if not allow_ambient:
        return {}
    ambient = os.environ.get(_GWS_CONFIG_ENV, "")
    if require_existing and not os.path.isdir(os.path.expanduser(ambient)):
        return {}
    return {_GWS_CONFIG_ENV: ambient} if ambient else {}


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
    # Configured implies ATELES_SWARM_GWS_CONFIG_DIR is set, so the shared
    # mapping always selects the swarm mailbox here.
    env.update(gws_config_dir_env())
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
    require_enabled: bool = True,
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
    if require_enabled and not email_enabled():
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
    replies: list[ApprovalReply] = []
    rejected_replies: list[ApprovalReply] = []
    unreadable_replies: list[ApprovalReply] = []
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
            seen_ids.add(mid)
            # The From address is text the sender chose. Require Gmail's own
            # record that the operator's domain authorized this message before
            # its body is read or its verdict counted. Unreadable metadata is a
            # failed read (the whole sweep fails below); readable but absent or
            # failing authentication is not the operator.
            auth_results = _fetch_auth_results(mid)
            if auth_results is None:
                saw_transport_error = True
                transport_detail = "gws_auth_metadata_failed"
                unreadable_replies.append(ApprovalReply(mid, subject))
                continue
            if not sender_domain_authenticated(auth_results):
                log.warning(
                    "approval: ignoring reply — sender domain authentication "
                    "absent or not a pass (address matched, identity unknown)"
                )
                rejected_replies.append(ApprovalReply(mid, subject, authenticated=False))
                cb = on_unauthenticated_operator_reply or on_sender_rejected
                if cb is not None:
                    try:
                        cb()
                    except Exception as exc:  # noqa: BLE001
                        log.warning(f"reply-rejected callback failed: {exc}")
                continue
            body_data = gws_json(["gmail", "+read", "--id", mid, "--headers",
                                  "--format", "json"], timeout=30)
            if not isinstance(body_data, dict):
                saw_transport_error = True
                transport_detail = "gws_read_failed"
                unreadable_replies.append(ApprovalReply(mid, subject))
                continue
            # A triage snippet, subject, preview, or header can contain both
            # verdict and token, but none proves that the full reply was read.
            plain = next((value for key in ("body_text", "body", "text", "plain")
                         if isinstance((value := body_data.get(key)), str)
                         and value.strip()), "")
            body = _authored_plain(plain)
            raw_html = body_data.get("body_html")
            # Preserve the target from the matched request token before quote
            # removal. The affirmative must still come from authored text.
            raw_source = plain + "\n" + (raw_html if isinstance(raw_html, str) else "")
            token_pattern = (r"release-approve:\s*(v[0-9][0-9A-Za-z.\-+]*)"
                             if token == "release-approve:" else
                             r"swarm-approve:\s*([A-Za-z0-9._-]+/[A-Za-z0-9._-]+#\d+)")
            targets = set(re.findall(token_pattern, raw_source, re.IGNORECASE))
            correlation_target = next(iter(targets)) if len(targets) == 1 else None
            if not body and isinstance(raw_html, str):
                body, boundary_seen = _extract_authored_html(raw_html)
                if body and not boundary_seen and _unmarked_notification_copy(body):
                    body = ""
            # A blank authored reply above a recognizable quote is readable:
            # it is a non-approval, so Turdus owes a correction.
            blank_quoted_reply = bool(plain and not body and
                                      (any(line.lstrip().startswith(">") for line in plain.splitlines())
                                       or any(_ATTRIBUTION.match(line.strip()) or _FORWARD.match(line.strip())
                                              for line in plain.splitlines())))
            if not body.strip() and not blank_quoted_reply:
                saw_transport_error = True
                transport_detail = "reply_quote_boundary_unknown"
                unreadable_replies.append(ApprovalReply(mid, subject))
                continue
            if on_reply_message is not None:
                try:
                    on_reply_message(token, mid)
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"on_reply_message callback failed: {exc}")
            texts.append(f"{subject}\n{body}")
            replies.append(ApprovalReply(mid, subject, body,
                                         correlation_target=correlation_target))

    # Any fetch failure within the sweep fails the WHOLE read: a verdict set
    # built from only the messages that loaded is incomplete, and acting on it
    # could honour one reply while missing a later one that changes it.
    if saw_transport_error:
        return ReadRepliesOutcome(
            kind="transport_error", texts=[], detail=transport_detail or "gws_failed",
            replies=tuple(replies),
            rejected_replies=tuple(rejected_replies),
            unreadable_replies=tuple(unreadable_replies),
        )
    return ReadRepliesOutcome(kind="ok", texts=texts, detail="",
                              replies=tuple(replies),
                              rejected_replies=tuple(rejected_replies))


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
