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
    correction_only_replies: tuple[ApprovalReply, ...] = ()


_ATTRIBUTION = re.compile(
    r"(?i)^(?:on\s+\S.+\s+\S.+\s+wrote:|"
    r"le\s+\S.+\s+\S.+\s+a écrit\s*:|"
    r"el\s+\S.+\s+\S.+\s+escribió:)$"
)
_FORWARD = re.compile(r"(?i)^(?:-----Original Message-----|Begin forwarded message:)$")
_OUTLOOK_HEADERS = ("from:", "sent:", "to:", "subject:")

_NON_RENDERED = frozenset({
    "head", "title", "script", "style", "template", "noscript", "object",
    "iframe", "svg", "math", "canvas", "audio", "video", "select", "option",
    "datalist", "textarea", "meta", "menu", "slot", "noembed",
    "picture", "frameset",
})
_RENDERED_PROSE = frozenset({
    "html", "body", "p", "div", "span", "blockquote", "a", "b", "strong",
    "i", "em", "u", "s", "del", "ins", "mark", "small", "big", "sub", "sup",
    "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "font", "center", "pre", "code",
    "q", "cite", "address", "section", "article", "header", "footer", "main",
    "aside", "details", "summary", "dl", "dt", "dd",
    "dialog",
})
_HTML_ATTRS = frozenset({"style", "dir", "class", "id", "type", "href", "open"})
_QUOTE_CLASSES = frozenset({
    "gmail_quote", "gmail_extra", "yahoo_quoted", "protonmail_quote",
    "msoquote", "outlookmessageheader", "gmail_attr", "moz-cite-prefix",
})
_QUOTE_IDS = frozenset({"divrplyfwdmsg", "appendonsend"})
_DARK_NAMES = {"black": (0, 0, 0), "navy": (0, 0, 128),
               "darkblue": (0, 0, 139), "midnightblue": (25, 25, 112),
               "darkslategray": (47, 79, 79), "dimgray": (105, 105, 105)}
_LIGHT_NAMES = {"white": (255, 255, 255)}
_SAFE_STYLE = frozenset({"font-family", "font-size", "font-weight", "font-style",
                         "text-align", "text-decoration", "line-height", "white-space",
                         "word-wrap", "overflow-wrap", "direction", "color",
                         "background", "background-color"})
_SAFE_FONTS = frozenset({
    "arial", "helvetica", "helvetica neue", "verdana", "tahoma", "trebuchet ms",
    "calibri", "cambria", "candara", "segoe ui", "aptos", "georgia", "times",
    "times new roman", "garamond", "palatino", "courier", "courier new",
    "menlo", "monaco", "consolas", "roboto", "open sans", "lato",
    "system-ui", "-apple-system", "blinkmacsystemfont", "sans-serif", "serif",
    "monospace", "ui-sans-serif", "ui-serif", "ui-monospace",
})
_LAYOUT_VALUE = re.compile(r"(0|\d+(?:\.\d+)?)(px|pt|em|rem|%)?")
_LAYOUT_LIMITS = {"px": 100, "pt": 75, "em": 8, "rem": 8, "%": 10}


def _rgba(value: str) -> tuple[int, int, int, float] | None:
    """Parse only opaque, unambiguous CSS colours used by the approval policy."""
    value = value.strip().lower()
    if value in _DARK_NAMES | _LIGHT_NAMES:
        return (*(_DARK_NAMES | _LIGHT_NAMES)[value], 1.0)
    match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", value)
    if match:
        digits = match.group(1)
        if len(digits) == 3:
            return (*(int(c * 2, 16) for c in digits), 1.0)
        return (*(int(digits[i:i + 2], 16) for i in (0, 2, 4)), 1.0)
    match = re.fullmatch(
        r"rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})"
        r"\s*(?:[,/]\s*(1(?:\.0+)?|0?\.\d+|0))?\s*\)", value)
    if match:
        channels = tuple(int(match.group(i)) for i in (1, 2, 3))
        alpha = float(match.group(4)) if match.group(4) else 1.0
        if all(channel <= 255 for channel in channels):
            return (*channels, alpha)
    return None


def _safe_colour(value: str, *, background: bool) -> bool:
    if not background and value.strip().lower() in _DARK_NAMES:
        return True
    rgba = _rgba(value)
    return bool(rgba and rgba[3] == 1.0 and
                (all(c >= 224 for c in rgba[:3]) if background else
                 all(c <= 100 for c in rgba[:3])))


def _safe_font_family(value: str) -> bool:
    families = value.split(",")
    return bool(families) and all(
        (family.strip().strip('"\'').casefold() in _SAFE_FONTS and
         (family.strip()[0] not in '"\'' or family.strip()[-1] == family.strip()[0]))
        for family in families if family.strip()
    ) and all(family.strip() for family in families)


def _safe_style(style: str, parent_px: float = 16, relative_scale: float = 1) -> tuple[bool, float, float]:
    font_px = parent_px
    scale = relative_scale
    if re.search(r"(?i)/\*|[{}]|(?:var|calc|env)\s*\(", style):
        return False, font_px, scale
    for declaration in style.split(";"):
        if not declaration.strip():
            continue
        if declaration.count(":") != 1:
            return False, font_px, scale
        name, value = (part.strip().lower() for part in declaration.split(":", 1))
        if not value or "!important" in value:
            return False, font_px, scale
        if name in {"color", "background", "background-color"}:
            if not _safe_colour(value, background=name != "color"):
                return False, font_px, scale
        elif name == "font-family":
            if not _safe_font_family(value):
                return False, font_px, scale
        elif name in {"text-transform", "direction", "unicode-bidi"}:
            if value != {"text-transform": "none", "direction": "ltr",
                         "unicode-bidi": "normal"}[name]:
                return False, font_px, scale
        elif name == "font-size":
            keyword = {"small": 13, "medium": 16, "large": 18,
                       "smaller": .8, "larger": 1.2}
            size = re.fullmatch(r"(\d+(?:\.\d+)?)(px|pt|em|rem|%)", value)
            if value in {"smaller", "larger"}:
                factor = keyword[value]
                font_px *= factor
                scale *= factor
            elif value in {"small", "medium", "large"}:
                font_px = keyword[value]
            elif size:
                amount = float(size.group(1))
                unit = size.group(2)
                if unit in {"em", "%"}:
                    factor = amount if unit == "em" else amount / 100
                    font_px *= factor
                    scale *= factor
                else:
                    font_px = amount * {"px": 1, "pt": 4 / 3, "rem": 16}[unit]
            else:
                return False, font_px, scale
            if font_px < 8 or scale < .7:
                return False, font_px, scale
        elif name == "line-height":
            if value != "normal":
                measure = _LAYOUT_VALUE.fullmatch(value)
                if not measure or not .7 <= float(measure.group(1)) * {
                    None: 1, "px": 1 / font_px, "pt": 4 / (3 * font_px),
                    "em": 1, "rem": 16 / font_px, "%": .01,
                }[measure.group(2)] <= 3:
                    return False, font_px, scale
        elif name in {"margin", "padding"} or re.fullmatch(
                r"(?:margin|padding)-(?:top|right|bottom|left)", name):
            values = value.split()
            if not 1 <= len(values) <= (4 if name in {"margin", "padding"} else 1):
                return False, font_px, scale
            for item in values:
                measure = _LAYOUT_VALUE.fullmatch(item)
                if not measure or float(measure.group(1)) > _LAYOUT_LIMITS.get(
                        measure.group(2), 0):
                    return False, font_px, scale
        elif name.startswith("border"):
            if value not in {"none", "0", "0px"}:
                return False, font_px, scale
        elif name not in _SAFE_STYLE:
            return False, font_px, scale
    return True, font_px, scale


def _hiding_style(style: str) -> bool:
    return not _safe_style(style)[0]


def _authored_plain_checked(text: str) -> tuple[str, bool]:
    """Keep every authored line; remove only structurally proven quotations."""
    lines: list[str] = []
    source = text.splitlines()
    saw_quote = False
    for index, line in enumerate(source):
        stripped = line.strip()
        if _ATTRIBUTION.fullmatch(stripped) and all(
                not tail.strip() or tail.lstrip().startswith(">")
                for tail in source[index + 1:]):
            return "\n".join(lines), True
        if _FORWARD.fullmatch(stripped):
            headers = source[index + 1:index + 1 + len(_OUTLOOK_HEADERS)]
            if (len(headers) == len(_OUTLOOK_HEADERS) and
                    all(header.strip().casefold().startswith(name) and
                        header.strip()[len(name):].strip()
                        for header, name in zip(headers, _OUTLOOK_HEADERS))):
                tail = source[index + 1 + len(_OUTLOOK_HEADERS):]
                if all(not part.strip() or part.lstrip().startswith(">") for part in tail):
                    return "\n".join(lines), True
                return "\n".join(lines), False
        if stripped.startswith(">"):
            saw_quote = True
            continue
        if stripped:
            if saw_quote:
                return "\n".join([*lines, stripped]), False
            lines.append(stripped)
    return "\n".join(lines), True


def _authored_plain(text: str) -> str:
    body, safe = _authored_plain_checked(text)
    return body if safe else ""


class _AuthoredHTML(HTMLParser):
    """Extract visible authored text while discarding quoted subtrees."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.quoted_parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []
        self.quote_depth = 0
        self.non_rendered_depth = 0
        self.structured = False
        self.body_depth = 0
        self.boundary_seen = False
        self.terminal = False
        self.unsafe = False
        self.font_stack: list[tuple[float, float]] = []
        self.closed_dialog_stack: list[bool] = []

    _VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img",
                       "input", "link", "meta", "param", "source", "track", "wbr"})
    _MAX_STACK = 64

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = [name.lower() for name, _ in attrs]
        if len(names) != len(set(names)):
            self.unsafe = True
        attrs_by_name = dict(attrs)
        classes = set((attrs_by_name.get("class") or "").lower().split())
        element_id = (attrs_by_name.get("id") or "").lower()
        if (set(names) - _HTML_ATTRS or
                ("dir" in attrs_by_name and
                 (attrs_by_name["dir"] or "").lower() != "ltr") or
                ("class" in attrs_by_name and
                 (not classes or not classes <= _QUOTE_CLASSES)) or
                ("id" in attrs_by_name and element_id not in _QUOTE_IDS) or
                ("type" in attrs_by_name and
                 (tag != "div" or (attrs_by_name["type"] or "").lower() != "cite")) or
                ("href" in attrs_by_name and tag != "a") or
                ("open" in attrs_by_name and tag not in {"details", "dialog"})):
            self.unsafe = True
        parent_px, parent_scale = self.font_stack[-1] if self.font_stack else (16, 1)
        style_safe, font_px, scale = _safe_style(
            attrs_by_name.get("style") or "", parent_px, parent_scale)
        if tag == "small":
            font_px *= .8
            scale *= .8
        elif tag == "big":
            font_px *= 1.2
            scale *= 1.2
        if font_px < 8 or scale < .7:
            self.unsafe = True
        if tag not in _RENDERED_PROSE and tag not in _NON_RENDERED and tag not in self._VOID:
            self.unsafe = True
        if (not style_safe or
                (tag == "link" and "stylesheet" in (attrs_by_name.get("rel") or "").lower().split())):
            self.unsafe = True
        if tag in {"html", "head", "body"}:
            self.structured = True
        if tag == "body":
            self.body_depth += 1
        quote = (tag == "blockquote"
                 or bool(classes & (_QUOTE_CLASSES - {"gmail_attr", "moz-cite-prefix"}))
                 or element_id in _QUOTE_IDS
                 or (tag == "div" and (attrs_by_name.get("type") or "").lower() == "cite"))
        if ("gmail_attr" in classes or "moz-cite-prefix" in classes
                or element_id in _QUOTE_IDS):
            self.terminal = True
            self.boundary_seen = True
        if tag in self._VOID:
            if tag == "area":
                self.unsafe = True
            if tag == "hr" and self.quote_depth == 0 and not self.non_rendered_depth:
                self.parts.append("\n")
            elif tag == "br" and self.quote_depth == 0 and not self.non_rendered_depth:
                self.parts.append("\n")
            elif tag == "br" and self.quote_depth:
                self.quoted_parts.append("\n")
            return
        if len(self.stack) >= self._MAX_STACK:
            self.unsafe = True
            return
        if quote:
            self.quote_depth += 1
            self.boundary_seen = True
        if tag in _NON_RENDERED:
            self.non_rendered_depth += 1
        closed_dialog = tag == "dialog" and "open" not in attrs_by_name
        if closed_dialog:
            self.non_rendered_depth += 1
        self.stack.append((tag, quote))
        self.font_stack.append((font_px, scale))
        self.closed_dialog_stack.append(closed_dialog)
        if tag in ("p", "div", "li", "tr") and self.quote_depth == 0 and not self.non_rendered_depth:
            self.parts.append("\n")
        elif tag in ("p", "div", "li", "tr") and self.quote_depth:
            self.quoted_parts.append("\n")

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
        self.font_stack.pop()
        closed_dialog = self.closed_dialog_stack.pop()
        if quote:
            self.quote_depth -= 1
        if tag in _NON_RENDERED:
            self.non_rendered_depth -= 1
        if closed_dialog:
            self.non_rendered_depth -= 1
        if tag == "body":
            self.body_depth -= 1
        if tag in ("p", "div", "li", "tr") and self.quote_depth == 0 and not self.non_rendered_depth:
            self.parts.append("\n")
        elif tag in ("p", "div", "li", "tr") and self.quote_depth:
            self.quoted_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if data.strip() and (not self.stack or
                             (self.structured and not self.body_depth and
                              not self.non_rendered_depth)):
            self.unsafe = True
            return
        tags = [tag for tag, _ in self.stack]
        if data.strip() and (any(tag in {"menu", "slot", "noembed", "object",
                                 "picture", "audio", "video", "frameset"}
                                 for tag in tags) or
                             ("details" in tags and "summary" not in tags) or
                             "dialog" in tags):
            self.unsafe = True
        if self.stack and self.stack[-1][0] == "style":
            # Empty rules cannot affect rendering. A rule with declarations
            # can affect any descendant through selectors and cascade.
            if "@font-face" in data.casefold() or data.strip() and not re.fullmatch(
                r"(?:\s*[.#]?[a-zA-Z][\w-]*\s*\{\s*\}\s*)+", data):
                self.unsafe = True
        if self.quote_depth and not self.non_rendered_depth:
            self.quoted_parts.append(data)
        if self.quote_depth or self.non_rendered_depth or (self.structured and not self.body_depth):
            return
        if self.boundary_seen and data.strip():
            self.unsafe = True
        if self.terminal:
            return
        self.parts.append(data)

    def unknown_decl(self, data: str) -> None:
        self.unsafe = True


def _extract_authored_html(html: str, *, original_request: str | None = None,
                           known_quote_lines: tuple[str, ...] = (),
                           verify_quote: bool = False) -> tuple[str, bool]:
    """Return authored text only when the HTML can be separated safely."""
    parser = _AuthoredHTML()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — malformed HTML cannot grant approval
        return "", False
    if parser.unsafe or parser.stack or parser.rawdata:
        return "", False
    if verify_quote:
        originals = {" ".join(line.split()).casefold() for line in
                     (original_request or "").splitlines() if line.strip()}
        originals.update(" ".join(line.split()).casefold() for line in known_quote_lines)
        quoted = [" ".join(line.split()).casefold() for line in
                  "".join(parser.quoted_parts).splitlines() if line.strip()]
        if any(line not in originals for line in quoted):
            return "", False
    body, safe = _authored_plain_checked("".join(parser.parts))
    return (body if safe else ""), parser.boundary_seen


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

    gws can print a keyring/format banner before the JSON, including a banner
    beginning with '['. Parse only a complete JSON value at a line start.
    Returns None on any failure (fail-open).
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
        if len(out.encode("utf-8")) > 1048576:
            log.warning(f"gws {args[:2]} response exceeds approval read limit")
            return None
        for match in re.finditer(r"(?m)^[ \t]*[\[{]", out):
            try:
                return json.loads(out[match.start():])
            except json.JSONDecodeError:
                continue
        return None
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
    if "id" in data and data["id"] != message_id:
        return None
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return None
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return None
    values: list[str] = []
    for h in headers:
        if (not isinstance(h, dict) or not isinstance(h.get("name"), str) or
                not isinstance(h.get("value"), str)):
            return None
        if h["name"].strip().lower() == "authentication-results":
            values.append(h["value"])
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


def _complete_triage_rows(data: Any, max_msgs: int) -> tuple[list[dict] | None, str]:
    """Accept only a complete, structurally valid mailbox collection."""
    if not isinstance(data, (dict, list)):
        return None, "gws_triage_invalid_response"
    if len(json.dumps(data).encode("utf-8")) > 1048576:
        return None, "gws_triage_oversized"
    if isinstance(data, dict) and "messages" in data and "results" in data:
        return None, "gws_triage_invalid_response"
    rows = data if isinstance(data, list) else data.get("messages", data.get("results"))
    if not isinstance(rows, list):
        return None, "gws_triage_invalid_response"
    if any(not isinstance(row, dict) or
           not isinstance(row.get("id"), str) or not row["id"] or
           not isinstance(row.get("subject"), str) or
           ("date" in row and not isinstance(row["date"], str)) or
           not isinstance(row.get("from", row.get("sender", row.get("From"))), str)
           for row in rows):
        return None, "gws_triage_invalid_response"
    if isinstance(data, list):
        return (rows, "") if len(rows) < max_msgs else (None, "gws_triage_truncated")
    if "query" in data and not isinstance(data["query"], str):
        return None, "gws_triage_invalid_response"
    if "complete" in data and not isinstance(data["complete"], bool):
        return None, "gws_triage_invalid_response"
    if data.get("complete") is False or len(rows) >= max_msgs:
        return None, "gws_triage_truncated"
    for key in ("nextPageToken", "next_page_token", "nextPage", "next_page",
                "continuation", "continuationToken"):
        if key in data and data[key] is not None and not isinstance(data[key], str):
            return None, "gws_triage_invalid_response"
        if key in data and data[key] not in (None, ""):
            return None, "gws_triage_truncated"
    for key in ("has_more", "hasMore", "truncated"):
        if key in data and not isinstance(data[key], bool):
            return None, "gws_triage_invalid_response"
    if data.get("has_more") or data.get("hasMore") or data.get("truncated"):
        return None, "gws_triage_truncated"
    for key in ("total", "totalCount", "resultSizeEstimate"):
        if key in data:
            if type(data[key]) is not int or data[key] < len(rows):
                return None, "gws_triage_invalid_response"
            if data[key] > len(rows):
                return None, "gws_triage_truncated"
    complete = (len(rows) < max_msgs or data.get("complete") is True or
                data.get("has_more") is False or data.get("hasMore") is False or
                data.get("truncated") is False or
                any(key in data and data[key] in (None, "") for key in
                    ("nextPageToken", "next_page_token", "nextPage", "next_page",
                     "continuation", "continuationToken")) or
                any(key in data and data[key] == len(rows) for key in
                    ("total", "totalCount")))
    if not complete:
        return None, "gws_triage_truncated"
    return rows, ""


def sent_request_body(token: str, reply_subject: str, *, max_msgs: int = 40) -> str | None:
    """Read the matching sent notice; missing or ambiguous evidence proves no quote."""
    # The target is in the reply subject, while the token is in the sent body.
    subject = re.sub(r"^\s*re:\s*", "", reply_subject, flags=re.IGNORECASE).strip()
    if not subject or token not in {"release-approve:", "swarm-approve:"}:
        return None
    target = (re.search(r"\bRelease\s+(v[0-9][\w.+-]*)", subject, re.IGNORECASE)
              if token == "release-approve:" else
              re.search(r"\bPR\s+([A-Za-z0-9._-]+/[A-Za-z0-9._-]+#\d+)", subject))
    if target is None:
        return None
    query = f'in:sent "{token} {target.group(1)}"'
    data = gws_json(["gmail", "+triage", "--format", "json", "--max",
                     str(max_msgs), "--query", query])
    rows, _ = _complete_triage_rows(data, max_msgs)
    if rows is None:
        return None
    swarm = os.environ.get("ATELES_SWARM_EMAIL", "").strip().casefold()
    candidates = [row for row in rows
                  if row["subject"].strip().casefold() == subject.casefold()
                  and _parse_address(str(row.get("from") or "")).casefold() == swarm]
    if len(candidates) != 1:
        return None
    sent = gws_json(["gmail", "+read", "--id", candidates[0]["id"],
                     "--headers", "--format", "json"], timeout=30)
    if not isinstance(sent, dict):
        return None
    body = sent.get("body_text")
    if not isinstance(body, str):
        html = sent.get("body_html")
        body = _extract_authored_html(html)[0] if isinstance(html, str) else None
    if not body or f"{token} {target.group(1)}".casefold() not in body.casefold():
        return None
    return body


def read_replies_with_status(
    tokens: list[str],
    max_msgs: int = 40,
    on_reply_message: Callable[[str, str], None] | None = None,
    on_sender_rejected: Callable[[], None] | None = None,
    on_unauthenticated_operator_reply: Callable[[], None] | None = None,
    require_enabled: bool = True,
    correction_queries: list[str] | None = None,
    request_bodies: dict[str, str] | Callable[[str, str], str | None] | None = None,
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
    if not tokens and not correction_queries:
        return ReadRepliesOutcome(kind="ok", texts=[], detail="")
    if type(max_msgs) is not int or max_msgs < 1:
        return ReadRepliesOutcome(kind="transport_error", texts=[], detail="gws_triage_invalid_limit")
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
    correction_only_replies: list[ApprovalReply] = []
    callback_pairs: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    saw_transport_error = False
    transport_detail = ""

    for token in [*tokens, *(correction_queries or [])]:
        correction_query = token in (correction_queries or [])
        query = f'newer_than:3d subject:"{token}"' if correction_query else f"newer_than:3d {token}"
        data = gws_json(["gmail", "+triage", "--format", "json", "--max",
                         str(max_msgs), "--query", query])
        if data is None:
            # gws_json fail-opens to None on any transport/parse failure.
            # For statusful callers that is distinct from "zero matching msgs".
            saw_transport_error = True
            transport_detail = "gws_triage_failed"
            continue
        msgs, error = _complete_triage_rows(data, max_msgs)
        if msgs is None:
            saw_transport_error = True
            transport_detail = error
            continue
        for m in msgs:
            mid = m["id"]
            subject = m["subject"]
            if len(mid) > 256 or len(subject) > 8192:
                saw_transport_error = True
                transport_detail = "gws_triage_oversized"
                continue
            if not mid or mid in seen_ids or not subject.upper().startswith("RE:"):
                continue
            if correction_query and token.casefold() not in subject.casefold():
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
            if len(json.dumps(body_data).encode("utf-8")) > 262144:
                saw_transport_error = True
                transport_detail = "gws_read_oversized"
                unreadable_replies.append(ApprovalReply(mid, subject))
                continue
            # A triage snippet, subject, preview, or header can contain both
            # verdict and token, but none proves that the full reply was read.
            plain_keys = [key for key in ("body_text", "body", "text", "plain")
                          if key in body_data]
            if (any(not isinstance(body_data[key], str) for key in plain_keys) or
                    len({body_data[key] for key in plain_keys}) > 1):
                saw_transport_error = True
                transport_detail = "gws_read_invalid_response"
                unreadable_replies.append(ApprovalReply(mid, subject))
                continue
            plain_key = plain_keys[0] if plain_keys else None
            plain_present = plain_key is not None
            plain = body_data[plain_key] if plain_present else ""
            body, plain_safe = _authored_plain_checked(plain)
            raw_html = body_data.get("body_html")
            # Preserve the target from the matched request token before quote
            # removal. The affirmative must still come from authored text.
            raw_source = plain + "\n" + (raw_html if isinstance(raw_html, str) else "")
            token_pattern = (r"release-approve:\s*(v[0-9][0-9A-Za-z.\-+]*)"
                             if token == "release-approve:" else
                             r"swarm-approve:\s*([A-Za-z0-9._-]+/[A-Za-z0-9._-]+#\d+)")
            targets = set(re.findall(token_pattern, raw_source, re.IGNORECASE))
            correlation_target = next(iter(targets)) if len(targets) == 1 else None
            html_body = ""
            html_present = "body_html" in body_data
            if isinstance(raw_html, str) and html_present:
                known = tuple(
                    f"{prefix}: {target}" for prefix, target in
                    (("release-approve", correlation_target),
                     ("swarm-approve", correlation_target)) if target
                )
                known += tuple(f"[APPROVE-{candidate}]" for candidate in tokens
                               if re.fullmatch(r"[0-9A-F]{8}", candidate, re.IGNORECASE))
                if callable(request_bodies):
                    try:
                        original_request = request_bodies(token, subject)
                    except Exception:  # noqa: BLE001 — missing proof cannot approve
                        original_request = None
                else:
                    original_request = (request_bodies or {}).get(token)
                html_body, boundary_seen = _extract_authored_html(
                    raw_html, original_request=original_request,
                    known_quote_lines=known, verify_quote=True)
                if html_body and not boundary_seen and _unmarked_notification_copy(html_body):
                    html_body = ""
            if not plain_safe or (plain_present and html_present and
                                  (not html_body.strip() or
                                   [line.casefold() for line in body.splitlines()] !=
                                   [line.casefold() for line in html_body.splitlines()])):
                saw_transport_error = True
                transport_detail = "reply_quote_boundary_unknown"
                unreadable_replies.append(ApprovalReply(mid, subject))
                continue
            if not plain_present:
                body = html_body
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
            reply = ApprovalReply(mid, subject, body,
                                  correlation_target=correlation_target)
            has_token = any(candidate.casefold() in (subject + "\n" + raw_source).casefold()
                            for candidate in tokens)
            if correction_query or not has_token:
                correction_only_replies.append(reply)
            else:
                texts.append(f"{subject}\n{body}")
                callback_pairs.append((token, mid))
                replies.append(reply)

    # Any fetch failure within the sweep fails the WHOLE read: a verdict set
    # built from only the messages that loaded is incomplete, and acting on it
    # could honour one reply while missing a later one that changes it.
    if saw_transport_error:
        return ReadRepliesOutcome(
            kind="transport_error", texts=[], detail=transport_detail or "gws_failed",
            replies=tuple(replies),
            rejected_replies=tuple(rejected_replies),
            unreadable_replies=tuple(unreadable_replies),
            correction_only_replies=tuple(correction_only_replies),
        )
    if on_reply_message is not None:
        for token, mid in callback_pairs:
            try:
                on_reply_message(token, mid)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"on_reply_message callback failed: {exc}")
                return ReadRepliesOutcome(
                    kind="transport_error", texts=[], detail="reply_callback_failed",
                    replies=tuple(replies), rejected_replies=tuple(rejected_replies),
                    unreadable_replies=tuple(unreadable_replies),
                    correction_only_replies=tuple(correction_only_replies),
                )
    return ReadRepliesOutcome(kind="ok", texts=texts, detail="",
                              replies=tuple(replies),
                              rejected_replies=tuple(rejected_replies),
                              correction_only_replies=tuple(correction_only_replies))


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
