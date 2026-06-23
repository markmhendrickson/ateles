#!/usr/bin/env python3
"""dispatch_report.py — render a session/dispatch report once, deliver two ways.

Used by the `/intake` skill's delivery phase. A single markdown report is
turned into ONE email-safe HTML render that serves both legs of the operator's
delivery choice:

  * EMAIL mode — the rendered HTML is mailed to the operator inline (the
    "rendered page rendered in the actual email"), with a "View online" banner
    linking to the hosted page that Neotoma `publish_rendered_page` mints for
    posterity.
  * page body — the same HTML (without document wrappers) is what you pass to
    `publish_rendered_page(html_body=...)` to create that hosted page.

Design constraints (match the hooks): stdlib only, fail-safe, operator-agnostic.
The recipient is sourced from --to or $OPERATOR_EMAIL, never hardcoded. Sending
is delegated to the operator's configured Gmail command ($ATELES_GMAIL_SEND_CMD,
per the gws-gmail rule) — this script never invents an unverified subcommand; if
no send command is configured it always leaves a recoverable .eml and prints the
exact command to run.

Usage:
  # 1) page body for publish_rendered_page (no <html>/<head>/<body> wrappers)
  dispatch_report.py --markdown report.md --title "Report" --page-html /tmp/page.html

  # 2) full email (document HTML), with the hosted link banner, written as .eml
  dispatch_report.py --markdown report.md --title "Report" --link "$URL" \
      --to "$OPERATOR_EMAIL" --eml /tmp/report.eml [--send]

  # render to stdout / self-test
  dispatch_report.py --markdown report.md --title "Report"
  dispatch_report.py --selftest
"""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

# Email-safe, self-contained styling. Inline-friendly, no external assets, no JS
# (CSP-safe so the identical body also renders as a published rendered_page).
PAGE_CSS = """
.ar-report{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  color:#1a1a1a;line-height:1.55;max-width:680px;margin:0 auto;padding:8px 4px;font-size:15px}
.ar-report h1{font-size:24px;margin:.4em 0 .3em;line-height:1.2}
.ar-report h2{font-size:19px;margin:1.1em 0 .35em;border-bottom:1px solid #ececec;padding-bottom:.2em}
.ar-report h3{font-size:16px;margin:1em 0 .3em}
.ar-report p{margin:.55em 0}
.ar-report ul,.ar-report ol{margin:.4em 0 .6em;padding-left:1.4em}
.ar-report li{margin:.18em 0}
.ar-report code{background:#f5f5f5;border-radius:4px;padding:.08em .35em;font-size:.92em;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.ar-report pre{background:#f7f7f8;border:1px solid #ececec;border-radius:8px;padding:12px 14px;
  overflow:auto;font-size:13px;line-height:1.45}
.ar-report pre code{background:none;padding:0}
.ar-report a{color:#1558d6;text-decoration:none}
.ar-report a:hover{text-decoration:underline}
.ar-report hr{border:0;border-top:1px solid #ececec;margin:1.2em 0}
.ar-report blockquote{margin:.6em 0;padding:.3em 0 .3em 1em;border-left:3px solid #dcdcdc;color:#555}
.ar-banner{background:#eef4ff;border:1px solid #cfe0ff;border-radius:8px;padding:10px 14px;
  margin:0 0 14px;font-size:14px}
.ar-banner a{font-weight:600}
"""

_INLINE = [
    (re.compile(r"`([^`]+)`"), lambda m: f"<code>{html.escape(m.group(1))}</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), lambda m: f"<strong>{m.group(1)}</strong>"),
    (re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)"), lambda m: f"<em>{m.group(1)}</em>"),
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"),
     lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>'),
]


def _inline(text: str) -> str:
    """Escape, then apply a small inline-markdown subset. Code spans are escaped
    inside the callback, so we escape the rest here and let the regexes inject
    the (trusted) tags."""
    out = html.escape(text)
    # html.escape turned literal markdown chars harmless; run inline patterns on
    # the escaped text (the patterns only add tags, links re-escape their href).
    for pat, repl in _INLINE:
        out = pat.sub(repl, out)
    return out


def markdown_to_body(md: str) -> str:
    """Convert a markdown report to an email-safe HTML fragment (no wrappers).

    Supports the subset reports actually use: ATX headings, unordered/ordered
    lists, fenced code blocks, blockquotes, horizontal rules, paragraphs, and
    inline code/bold/italic/links. Unknown constructs degrade to paragraphs.
    """
    lines = md.replace("\r\n", "\n").split("\n")
    html_parts: list[str] = []
    i = 0
    n = len(lines)

    def flush_para(buf: list[str]) -> None:
        if buf:
            html_parts.append("<p>" + _inline(" ".join(buf).strip()) + "</p>")
            buf.clear()

    para: list[str] = []
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith("```"):
            flush_para(para)
            i += 1
            code: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # consume closing fence
            html_parts.append(
                "<pre><code>" + html.escape("\n".join(code)) + "</code></pre>"
            )
            continue

        # Blank line ends a paragraph
        if not stripped:
            flush_para(para)
            i += 1
            continue

        # Horizontal rule
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            flush_para(para)
            html_parts.append("<hr>")
            i += 1
            continue

        # Heading
        m = re.match(r"(#{1,6})\s+(.*)", stripped)
        if m:
            flush_para(para)
            level = len(m.group(1))
            html_parts.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>")
            i += 1
            continue

        # Lists (consume a contiguous block)
        if re.match(r"[-*+]\s+", stripped) or re.match(r"\d+\.\s+", stripped):
            flush_para(para)
            ordered = bool(re.match(r"\d+\.\s+", stripped))
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while i < n:
                s = lines[i].strip()
                mo = re.match(r"\d+\.\s+(.*)", s) if ordered else re.match(r"[-*+]\s+(.*)", s)
                if not mo:
                    break
                items.append("<li>" + _inline(mo.group(1).strip()) + "</li>")
                i += 1
            html_parts.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue

        # Blockquote
        if stripped.startswith(">"):
            flush_para(para)
            quote: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            html_parts.append("<blockquote>" + _inline(" ".join(quote)) + "</blockquote>")
            continue

        para.append(stripped)
        i += 1

    flush_para(para)
    return "\n".join(html_parts)


def page_html_body(md: str, link: str | None = None) -> str:
    """The inner HTML for publish_rendered_page / the email body. No doc wrappers.
    Includes the "View online" banner when a hosted link is supplied."""
    banner = ""
    if link:
        safe = html.escape(link, quote=True)
        banner = (
            f'<div class="ar-banner">📄 This report is also published online — '
            f'<a href="{safe}">view it in your browser</a>.</div>'
        )
    return f'<div class="ar-report">{banner}\n{markdown_to_body(md)}</div>'


def full_email_html(md: str, title: str, link: str | None = None) -> str:
    """A complete HTML document for the email payload (wrappers + inline CSS)."""
    body = page_html_body(md, link)
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def build_eml(md: str, title: str, to_addr: str, subject: str,
              link: str | None = None, from_addr: str | None = None) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject or title
    msg["To"] = to_addr
    if from_addr:
        msg["From"] = from_addr
    msg["Date"] = formatdate(localtime=True)
    # Plain-text fallback = the raw markdown; HTML alternative = rendered report.
    msg.set_content(md)
    msg.add_alternative(full_email_html(md, title, link), subtype="html")
    return bytes(msg)


def is_third_party(recipient: str, operator_email: str) -> bool:
    """True when the recipient is someone other than the operator (a beneficiary
    / customer). Case-insensitive; empty operator_email is treated as 'unknown'
    → third-party (fail safe toward requiring approval)."""
    r = (recipient or "").strip().lower()
    o = (operator_email or "").strip().lower()
    if not r:
        return False
    if not o:
        return True
    return r != o


def send_allowed(recipient: str, operator_email: str, approved: bool) -> bool:
    """Beneficiary (third-party) delivery requires explicit operator approval;
    operator self-delivery is always allowed. This encodes the draft-don't-send
    guardrail at the tool boundary so a beneficiary report can never auto-send."""
    return (not is_third_party(recipient, operator_email)) or bool(approved)


def deliver(eml_path: Path, to_addr: str, subject: str) -> int:
    """Send the .eml via the operator-configured Gmail command, else explain.

    $ATELES_GMAIL_SEND_CMD is a template; supported placeholders: {eml} {to}
    {subject}. Example: 'gws gmail send --raw {eml} --to {to}'. We never guess a
    gws subcommand — if unset, the .eml is left in place and the operator/agent
    runs the send step explicitly (gws-gmail rule)."""
    tmpl = os.environ.get("ATELES_GMAIL_SEND_CMD", "").strip()
    if not tmpl:
        print(
            f"[dispatch_report] No $ATELES_GMAIL_SEND_CMD configured. Rendered "
            f"email left at {eml_path}. Send it with your gws gmail command, e.g.\n"
            f"  gws gmail <send-subcommand> --to {to_addr} --subject {subject!r} "
            f"--html-file <(unpack {eml_path})\n"
            f"or set ATELES_GMAIL_SEND_CMD='gws gmail ... {{eml}} {{to}}' and re-run --send.",
            file=sys.stderr,
        )
        return 0  # fail-safe: artifact preserved, not an error
    cmd = tmpl.format(eml=str(eml_path), to=to_addr, subject=subject)
    print(f"[dispatch_report] sending via: {cmd}", file=sys.stderr)
    proc = subprocess.run(cmd, shell=True)
    return proc.returncode


def _selftest() -> int:
    sample = (
        "# Title\n\nA **bold** intro with `code` and a [link](https://x.test).\n\n"
        "## Section\n\n- one\n- two\n\n1. first\n2. second\n\n```\nx = 1\n```\n\n> quote\n\n---\n"
    )
    body = page_html_body(sample, link="https://x.test/p")
    checks = {
        "h1": "<h1>Title</h1>" in body,
        "h2": "<h2>Section</h2>" in body,
        "bold": "<strong>bold</strong>" in body,
        "code": "<code>code</code>" in body,
        "link": '<a href="https://x.test">link</a>' in body,
        "ul": "<ul><li>one</li><li>two</li></ul>" in body,
        "ol": "<ol><li>first</li><li>second</li></ol>" in body,
        "pre": "<pre><code>x = 1</code></pre>" in body,
        "quote": "<blockquote>quote</blockquote>" in body,
        "hr": "<hr>" in body,
        "banner": "view it in your browser" in body,
        "no-script": "<script" not in body.lower(),
    }
    eml = build_eml(sample, "Title", "ops@test", "Sub", link="https://x.test/p",
                    from_addr="me@test")
    checks["eml-html"] = b"text/html" in eml and b"<strong>bold</strong>" in eml
    checks["eml-text"] = b"text/plain" in eml
    # beneficiary approval guard
    checks["operator_self_allowed"] = send_allowed("ops@test", "ops@test", approved=False)
    checks["third_party_blocked"] = not send_allowed("cust@x", "ops@test", approved=False)
    checks["third_party_approved_ok"] = send_allowed("cust@x", "ops@test", approved=True)
    checks["unknown_operator_needs_approval"] = not send_allowed("cust@x", "", approved=False)
    checks["is_third_party"] = is_third_party("cust@x", "ops@test") and not is_third_party("OPS@test", "ops@test")
    ok = all(checks.values())
    for k, v in checks.items():
        print(f"[{'PASS' if v else 'FAIL'}] {k}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a dispatch report; deliver inline-HTML + hosted-link email.")
    ap.add_argument("--markdown", help="path to the report markdown file ('-' for stdin)")
    ap.add_argument("--title", default="Session report")
    ap.add_argument("--link", help="hosted rendered_page URL (adds a 'View online' banner)")
    ap.add_argument("--page-html", help="write the page body HTML (for publish_rendered_page) to this path")
    ap.add_argument("--eml", help="write the full email (.eml) to this path")
    ap.add_argument("--to", default=os.environ.get("OPERATOR_EMAIL", ""),
                    help="recipient (default $OPERATOR_EMAIL)")
    ap.add_argument("--from", dest="from_addr", default=os.environ.get("OPERATOR_EMAIL", ""))
    ap.add_argument("--subject", default="")
    ap.add_argument("--send", action="store_true", help="deliver via $ATELES_GMAIL_SEND_CMD")
    ap.add_argument("--beneficiary", default="",
                    help="label for a beneficiary/customer report (delivery to a non-operator recipient)")
    ap.add_argument("--approved", action="store_true",
                    help="operator approval for sending a beneficiary (third-party) report")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.markdown:
        ap.error("--markdown is required (or use --selftest)")

    md = sys.stdin.read() if args.markdown == "-" else Path(args.markdown).read_text(encoding="utf-8")

    if args.page_html:
        Path(args.page_html).write_text(page_html_body(md, args.link), encoding="utf-8")
        print(f"[dispatch_report] page body → {args.page_html}")

    if args.eml or args.send:
        if not args.to:
            ap.error("--to (or $OPERATOR_EMAIL) required to build/send the email")
        subject = args.subject or args.title
        eml_bytes = build_eml(md, args.title, args.to, subject, args.link, args.from_addr or None)
        eml_path = Path(args.eml) if args.eml else Path("/tmp/ateles_report.eml")
        eml_path.write_text(eml_bytes.decode("utf-8", "replace"), encoding="utf-8")
        print(f"[dispatch_report] email → {eml_path}")
        if args.send:
            operator_email = args.from_addr or os.environ.get("OPERATOR_EMAIL", "")
            if not send_allowed(args.to, operator_email, args.approved):
                label = args.beneficiary or "beneficiary"
                print(
                    f"[dispatch_report] {args.to} is not the operator ({label} report) — "
                    f"third-party delivery requires operator approval. Re-run with --approved "
                    f"once approved. Email preserved at {eml_path}.",
                    file=sys.stderr,
                )
                return 0  # fail-safe: never auto-send to a beneficiary unapproved
            return deliver(eml_path, args.to, subject)

    if not (args.page_html or args.eml or args.send):
        # Default: print the page body to stdout.
        print(page_html_body(md, args.link))
    return 0


if __name__ == "__main__":
    sys.exit(main())
