"""minimal_markdown.py — a small, dependency-free Markdown-to-HTML converter.

WHY A HAND-ROLLED CONVERTER: this repo's generator scripts
(render_plan_docs.py, render_agent_docs.py, the hooks) run stdlib-only Python
with no third-party dependency, and no Markdown library is pinned in
pyproject.toml / scripts/requirements.txt. Adding one for a single generator
would be the first third-party dependency this class of script has ever
needed, so this module covers the actual subset of Markdown that the
positioning mirrors and the repo README use: headings, paragraphs,
bold/italic, inline and fenced code, links, blockquotes, ordered/unordered
lists, rules, and simple pipe tables. It is NOT a general CommonMark
implementation; reach for a real dependency if the source content grows
past this bounded subset.

Frontmatter (a leading `---\\n...\\n---` block) is stripped by the caller
before this module sees the body, since frontmatter parsing belongs to the
positioning-mirror reader, not to Markdown rendering.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin, urlparse

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^[-*]\s+(.*)$")
_ORDERED = re.compile(r"^\d+\.\s+(.*)$")
_FENCE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")
_TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_RULE = re.compile(r"^\s*(?:---+|___+|\*\*\*+)\s*$")


def _resolved_href(href: str, link_base: str | None) -> str:
    parsed = urlparse(href)
    if not link_base or parsed.scheme or parsed.netloc or href.startswith(("#", "/")):
        return href
    return urljoin(link_base, href)


def _inline(text: str, link_base: str | None = None) -> str:
    text = html.escape(text, quote=True)
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)

    def link(match: re.Match) -> str:
        label, href = match.groups()
        return f'<a href="{html.escape(_resolved_href(href, link_base), quote=True)}">{label}</a>'

    text = _LINK.sub(link, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    return text


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"[`*_]", "", text).lower()).strip("-")


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def to_html(markdown_text: str, link_base: str | None = None) -> str:
    """Convert the supported Markdown subset to HTML fragments (no wrapping
    <html>/<body> — caller embeds this into a page template)."""
    lines = markdown_text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para_buf: list[str] = []
    list_buf: list[str] = []
    ordered_buf: list[str] = []
    quote_buf: list[str] = []

    def flush_para():
        if para_buf:
            out.append(f"<p>{_inline(' '.join(para_buf), link_base)}</p>")
            para_buf.clear()

    def flush_list():
        if list_buf:
            items = "".join(f"<li>{_inline(i, link_base)}</li>" for i in list_buf)
            out.append(f"<ul>{items}</ul>")
            list_buf.clear()

    def flush_ordered():
        if ordered_buf:
            items = "".join(
                f"<li>{_inline(item, link_base)}</li>" for item in ordered_buf
            )
            out.append(f"<ol>{items}</ol>")
            ordered_buf.clear()

    def flush_quote():
        if quote_buf:
            out.append(
                f"<blockquote><p>{_inline(' '.join(quote_buf), link_base)}</p></blockquote>"
            )
            quote_buf.clear()

    def flush_blocks():
        flush_para()
        flush_list()
        flush_ordered()
        flush_quote()

    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line = raw_line.rstrip()
        if not line.strip():
            flush_blocks()
            i += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            flush_blocks()
            language = fence.group(1)
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i].rstrip()):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            lang_attr = (
                f' class="language-{html.escape(language, quote=True)}"'
                if language
                else ""
            )
            out.append(
                f"<pre><code{lang_attr}>{html.escape(chr(10).join(code_lines), quote=False)}</code></pre>"
            )
            continue

        if (
            "|" in line
            and i + 1 < len(lines)
            and _TABLE_DIVIDER.match(lines[i + 1])
        ):
            flush_blocks()
            headers = _table_cells(line)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_table_cells(lines[i]))
                i += 1
            head = "".join(
                f"<th>{_inline(cell, link_base)}</th>" for cell in headers
            )
            body = "".join(
                "<tr>"
                + "".join(f"<td>{_inline(cell, link_base)}</td>" for cell in row)
                + "</tr>"
                for row in rows
            )
            out.append(
                f'<div class="table-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
            )
            continue

        if line.lstrip().startswith(">"):
            flush_para()
            flush_list()
            flush_ordered()
            quote_buf.append(line.lstrip()[1:].lstrip())
            i += 1
            continue
        flush_quote()

        if _RULE.match(line):
            flush_blocks()
            out.append('<hr class="rule">')
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_blocks()
            level = len(heading.group(1))
            heading_text = heading.group(2)
            out.append(
                f'<h{level} id="{_slug(heading_text)}">{_inline(heading_text, link_base)}</h{level}>'
            )
            i += 1
            continue
        bullet = _BULLET.match(line)
        if bullet:
            flush_para()
            flush_ordered()
            list_buf.append(bullet.group(1))
            i += 1
            continue
        ordered = _ORDERED.match(line)
        if ordered:
            flush_para()
            flush_list()
            ordered_buf.append(ordered.group(1))
            i += 1
            continue
        flush_list()
        flush_ordered()
        para_buf.append(line.strip())
        i += 1

    flush_blocks()
    return "\n".join(out)


_LEADING_HTML_COMMENT = re.compile(r"^\s*<!--.*?-->\s*\n?", re.DOTALL)


def strip_frontmatter(text: str) -> tuple[dict, str]:
    """Split a `---\\nkey: value\\n---\\nbody` file into (frontmatter_dict,
    body). Frontmatter parsing here is intentionally minimal (flat
    `key: value` scalars only, plus `key:` followed by indented `- item`
    lists) — enough for the render_positioning_docs.py / render_agent_docs.py
    frontmatter shape without pulling in a YAML dependency this script class
    has never needed.

    Every projection generator in this repo (render_plan_docs.py,
    render_agent_docs.py, render_positioning_docs.py) leads its output with
    a "do not edit this file directly" HTML comment BEFORE the YAML
    frontmatter block, so that comment is stripped first -- otherwise the
    leading `<!--` means the file does not start with `---` and this
    function silently treats the whole file (comment, frontmatter, and all)
    as body text, which is exactly what a first pass of this module did."""
    text = _LEADING_HTML_COMMENT.sub("", text, count=1)
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        return {}, text
    fm_block = parts[0][3:].strip("\n")
    body = parts[1].lstrip("\n")

    fm: dict = {}
    current_list_key: str | None = None
    for raw_line in fm_block.split("\n"):
        if not raw_line.strip():
            continue
        if raw_line.startswith("  - ") and current_list_key:
            fm.setdefault(current_list_key, [])
            fm[current_list_key].append(raw_line.strip()[2:].strip())
            continue
        if ":" in raw_line and not raw_line.startswith(" "):
            key, _, value = raw_line.partition(":")
            key = key.strip()
            value = value.strip()
            if value:
                fm[key] = value.strip('"')
                current_list_key = None
            else:
                current_list_key = key
    return fm, body
