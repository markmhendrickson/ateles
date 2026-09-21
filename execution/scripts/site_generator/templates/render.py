"""Render source-backed product sites with distinct record/seal identities."""

from __future__ import annotations

import html as html_mod
import re

from . import minimal_markdown as mdlib


def _esc(value: str) -> str:
    return html_mod.escape(str(value or ""), quote=True)


def _css_vars(mode_tokens: dict) -> str:
    order = (
        "ink",
        "ink_2",
        "ink_3",
        "paper",
        "paper_2",
        "line",
        "line_2",
        "accent",
        "accent_2",
        "accent_wash",
        "on_accent",
        "correction",
        "grant",
        "revoked",
        "warn",
    )
    return "\n    ".join(
        f"--{key.replace('_', '-')}: {mode_tokens[key]};"
        for key in order
        if key in mode_tokens
    )


def _token_number(source: dict, key: str, fallback: str) -> str:
    text = " ".join(str(value) for value in (source or {}).values())
    match = re.search(rf"--{re.escape(key)}\s*:\s*([^;}}]+)", text)
    return match.group(1).strip() if match else fallback


def build_css(tokens: dict, product_slug: str | None = None) -> str:
    identity = tokens.get("identity", tokens.get("shared", {}))
    product = tokens["product"]
    palette = product["color_palette"]
    type_scale = product["type_scale"]
    light = palette.get("light", {})
    dark = palette.get("dark", {})
    heading_font = type_scale.get("heading_font_family", "Georgia, serif")
    body_font = type_scale.get("body_font_family", "system-ui, sans-serif")
    code_font = type_scale.get("code_font_family", "ui-monospace, monospace")
    body_size = type_scale.get("body_base_size", "16px")
    heading_weight = type_scale.get("heading_weight", 700)
    h1_size = type_scale.get("h1_size", "clamp(2.8rem, 7vw, 5.8rem)")
    h2_size = type_scale.get("h2_size", "clamp(2rem, 4vw, 3.6rem)")
    max_w = _token_number(identity.get("spacing_system") or {}, "maxw", "1180px")
    root_radius = _token_number(identity.get("border_radius") or {}, "radius", "10px")

    return f"""
:root {{
  {_css_vars(light)}
  --radius: {root_radius}; --maxw: {max_w}; --heading: {heading_font};
  --body: {body_font}; --mono: {code_font};
  --shadow-soft: 0 20px 60px rgba(18, 22, 19, .08);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{ color-scheme: dark; {_css_vars(dark)} --shadow-soft: 0 20px 60px rgba(0, 0, 0, .28); }}
}}
:root[data-theme="dark"] {{ color-scheme: dark; {_css_vars(dark)} --shadow-soft: 0 20px 60px rgba(0, 0, 0, .28); }}
html {{ color-scheme: light dark; scroll-behavior: smooth; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--paper); color: var(--ink); font-family: var(--body); font-size: {body_size}; line-height: 1.62; -webkit-font-smoothing: antialiased; }}
a {{ color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: .2em; }}
a:hover {{ text-decoration: underline; }}
code {{ font-family: var(--mono); font-size: .88em; background: var(--paper-2); padding: .12em .35em; border-radius: 4px; }}
.wrap {{ width: min(100%, var(--maxw)); margin-inline: auto; padding-inline: clamp(20px, 4vw, 56px); }}
main > section {{ padding-block: clamp(64px, 9vw, 112px); }}
.rule {{ border: 0; border-top: 1px solid var(--line); margin: 0; }}
h1, h2, h3 {{ font-family: var(--heading); margin: 0; text-wrap: balance; }}
h1 {{ font-size: {h1_size}; line-height: .98; letter-spacing: -.045em; max-width: 13ch; font-weight: {heading_weight}; }}
h2 {{ font-size: {h2_size}; line-height: 1.02; letter-spacing: -.035em; max-width: 18ch; font-weight: {heading_weight}; }}
h3 {{ font-size: clamp(1.05rem, 2vw, 1.35rem); line-height: 1.2; }}
p {{ margin: 0 0 1rem; }}
.lede {{ max-width: 66ch; color: var(--ink-2); font-size: clamp(1.05rem, 2vw, 1.28rem); }}
.eyebrow, .scope-chip {{ color: var(--accent); font-family: var(--mono); font-size: .72rem; font-weight: 650; letter-spacing: .1em; text-transform: uppercase; }}
.muted {{ color: var(--ink-3); }} .small {{ font-size: .84rem; }}
header.nav {{ position: sticky; top: 0; z-index: 30; border-bottom: 1px solid transparent; transition: background .22s, border-color .22s, box-shadow .22s; }}
header.nav.scrolled {{ background: color-mix(in srgb, var(--paper) 84%, transparent); border-color: var(--line); box-shadow: 0 10px 30px rgba(0,0,0,.05); backdrop-filter: blur(16px) saturate(1.25); }}
.nav-in {{ width: 100%; min-height: 68px; padding: 10px clamp(18px, 3vw, 52px); display: flex; align-items: center; gap: 24px; flex-wrap: nowrap; }}
.brand {{ color: var(--ink); font-family: var(--heading); font-weight: {heading_weight}; font-size: 1.16rem; white-space: nowrap; }}
.brand:hover {{ text-decoration: none; }}
.brand small {{ margin-left: 9px; color: var(--ink-3); font-family: var(--mono); font-size: .62rem; font-weight: 500; letter-spacing: .08em; text-transform: uppercase; }}
.nav-links {{ display: flex; align-items: center; gap: clamp(12px, 2vw, 28px); margin-left: auto; overflow-x: auto; white-space: nowrap; font-size: .9rem; }}
.nav-links a {{ color: var(--ink-2); }}
.cta-row {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 28px; }}
.btn {{ display: inline-flex; align-items: center; justify-content: center; min-height: 44px; padding: 10px 18px; border: 1px solid var(--accent); border-radius: 8px; background: var(--accent); color: var(--on-accent, #fff); font-weight: 700; text-decoration: none; }}
.btn:hover {{ text-decoration: none; filter: brightness(1.05); }}
.btn.ghost {{ color: var(--ink); background: transparent; border-color: var(--line); }}
.hero {{ min-height: min(760px, calc(100svh - 68px)); display: grid; align-items: center; }}
.hero-copy .category {{ margin-bottom: 22px; }} .hero-copy .lede {{ margin-top: 24px; }}
.proof-line {{ margin-top: 24px; font-family: var(--mono); font-size: .78rem; color: var(--ink-3); }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 250px), 1fr)); gap: 18px; margin-top: 34px; }}
.card, .panel {{ border: 1px solid var(--line); border-radius: var(--radius); background: var(--paper-2); padding: clamp(20px, 3vw, 30px); }}
.card p, .panel p {{ color: var(--ink-2); }} .card .meta {{ color: var(--ink-3); font-family: var(--mono); font-size: .72rem; margin-top: 18px; }}
.source-section > .source-inner {{ max-width: 900px; }} .source-heading {{ margin-bottom: 28px; }}
.source-note {{ margin-top: 30px; padding-top: 14px; border-top: 1px solid var(--line); color: var(--ink-3); font-family: var(--mono); font-size: .72rem; overflow-wrap: anywhere; }}
.markdown-body {{ min-width: 0; overflow-wrap: anywhere; }}
.markdown-body code {{ white-space: normal; overflow-wrap: anywhere; word-break: break-word; }}
.markdown-body > :first-child {{ margin-top: 0; }} .markdown-body :is(h1,h2,h3) {{ margin-top: 1.5em; margin-bottom: .55em; }}
.markdown-body p, .markdown-body li {{ color: var(--ink-2); }} .markdown-body ul, .markdown-body ol {{ padding-left: 1.4rem; }}
.markdown-body blockquote {{ margin: 24px 0; padding: 16px 20px; border-left: 3px solid var(--accent); background: var(--paper-2); }}
.markdown-body pre {{ overflow-x: auto; padding: 18px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--paper-2); }}
.table-scroll {{ overflow-x: auto; margin: 24px 0; }} table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
th, td {{ padding: 11px 13px; border: 1px solid var(--line); text-align: left; vertical-align: top; }} th {{ background: var(--paper-2); font-family: var(--heading); }}
.pain-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1px; margin-top: 34px; background: var(--line); border: 1px solid var(--line); }}
.pain-card {{ padding: clamp(24px, 4vw, 42px); background: var(--paper); }} .pain-card p {{ color: var(--ink-2); }}
.pain-card .pain-kind {{ font-family: var(--mono); color: var(--accent); font-size: .7rem; letter-spacing: .1em; text-transform: uppercase; }}
.steps {{ display: grid; gap: 1px; margin-top: 34px; border: 1px solid var(--line); background: var(--line); }}
.step {{ display: grid; grid-template-columns: 88px 1fr; gap: 20px; padding: 24px; background: var(--paper); }} .step-num {{ font-family: var(--mono); color: var(--accent); }}
.status-note, .blocker {{ margin-top: 26px; padding: 16px 18px; border-left: 3px solid var(--accent); background: var(--paper-2); color: var(--ink-2); }}
.blocker {{ color: var(--revoked, #9c4a3a); border-color: currentColor; font-family: var(--mono); }}
.tw-toggle {{ display: flex; gap: 2px; flex: 0 0 auto; padding: 3px; border: 1px solid var(--line); border-radius: 999px; background: var(--paper-2); }}
.tw-toggle input {{ position: absolute; opacity: 0; pointer-events: none; }}
.tw-toggle label {{ display: grid; place-items: center; min-width: 28px; height: 26px; padding-inline: 7px; border-radius: 999px; color: var(--ink-3); cursor: pointer; font-size: .68rem; }}
body:has(#tw-light:checked) .tw-toggle label[for="tw-light"], body:has(#tw-dark:checked) .tw-toggle label[for="tw-dark"], body:has(#tw-system:checked) .tw-toggle label[for="tw-system"] {{ color: var(--on-accent, #fff); background: var(--accent); }}
body:has(#tw-light:checked) {{ {_css_vars(light)} color-scheme: light; }} body:has(#tw-dark:checked) {{ {_css_vars(dark)} color-scheme: dark; }}
footer {{ border-top: 1px solid var(--line); padding-block: 42px; }} .footer-in {{ display: flex; justify-content: space-between; gap: 30px; flex-wrap: wrap; }}
.footer-noun {{ max-width: 44ch; color: var(--ink-2); }} .flinks {{ display: flex; gap: 18px; flex-wrap: wrap; font-size: .86rem; }}

/* Neotoma: an editorial record with persistent provenance marginalia. */
.product-neotoma .record-hero {{ grid-template-columns: minmax(0, 1.15fr) minmax(300px, .85fr); gap: clamp(38px, 7vw, 96px); }}
.product-neotoma .record-demo {{ position: relative; border-top: 3px double var(--ink); border-bottom: 1px solid var(--line); padding: 28px 0 12px 68px; }}
.product-neotoma .record-demo::before {{ content: "PROVENANCE"; position: absolute; left: 0; top: 30px; writing-mode: vertical-rl; transform: rotate(180deg); color: var(--ink-3); font-family: var(--mono); font-size: .62rem; letter-spacing: .14em; }}
.record-row {{ padding: 18px 0; border-bottom: 1px solid var(--line); }} .record-row:last-child {{ border-bottom: 0; }}
.record-key {{ font-family: var(--mono); font-size: .7rem; color: var(--ink-3); }} .record-value {{ display: block; margin-top: 5px; font-family: var(--heading); font-size: 1.16rem; }}
.record-old {{ color: var(--correction, var(--accent)); text-decoration: line-through; text-decoration-thickness: 2px; }} .record-current {{ color: var(--ink); }}
.record-stamp {{ margin-top: 4px; color: var(--ink-3); font-family: var(--mono); font-size: .68rem; }}
.product-neotoma .source-section {{ position: relative; display: grid; grid-template-columns: 148px minmax(0, 1fr); column-gap: 42px; }}
.product-neotoma .source-section::before {{ content: attr(data-source-label); grid-column: 1; align-self: start; padding-top: 8px; color: var(--ink-3); border-top: 1px solid var(--line); font-family: var(--mono); font-size: .67rem; letter-spacing: .06em; overflow-wrap: anywhere; }}
.product-neotoma .source-section > .source-inner {{ grid-column: 2; }}
.product-neotoma .capability-list {{ margin-top: 38px; border-top: 1px solid var(--ink); }}
.product-neotoma .capability {{ display: grid; grid-template-columns: 150px 1fr; gap: 28px; padding: 28px 0; border-bottom: 1px solid var(--line); }}
.product-neotoma .capability .meta {{ font-family: var(--mono); color: var(--ink-3); font-size: .7rem; }}
.product-neotoma .correction-demo {{ margin-top: 34px; padding: 24px; border: 1px solid var(--line); background: var(--paper-2); font-family: var(--mono); }}

/* Ateles: bounded authority, stamped grants, and a visible chain of scope. */
.product-ateles .nav {{ border-top: 4px solid var(--accent); }}
.product-ateles .seal-hero {{ grid-template-columns: minmax(0, 1.25fr) minmax(300px, .75fr); gap: clamp(42px, 8vw, 110px); }}
.seal-board {{ border: 1px solid var(--line); border-radius: var(--radius); padding: clamp(24px, 4vw, 42px); background: var(--paper-2); box-shadow: var(--shadow-soft); }}
.seal-mark {{ width: 116px; color: var(--accent); transform: rotate(-7deg); }} .seal-mark svg {{ display: block; width: 100%; height: auto; }}
.grant-row {{ display: grid; grid-template-columns: 1fr auto; gap: 20px; padding: 14px 0; border-bottom: 1px solid var(--line); }} .grant-row:last-child {{ border-bottom: 0; }}
.grant-state {{ color: var(--grant, var(--accent)); font-family: var(--mono); font-size: .7rem; }}
.product-ateles .source-section > .source-inner {{ padding-left: clamp(20px, 5vw, 72px); border-left: 4px solid var(--accent); }}
.product-ateles .capability-list {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; margin-top: 38px; }}
.product-ateles .capability {{ position: relative; min-height: 220px; padding: 28px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--paper-2); }}
.product-ateles .capability::after {{ content: "AUTHORIZED"; position: absolute; right: 18px; bottom: 16px; color: var(--accent); border: 1px solid currentColor; border-radius: 999px; padding: 3px 8px; font-family: var(--mono); font-size: .58rem; letter-spacing: .08em; transform: rotate(-4deg); opacity: .7; }}
.scope-line {{ margin-top: 16px; color: var(--ink-3); font-family: var(--mono); font-size: .72rem; }}
.hierarchy {{ display: grid; grid-template-columns: repeat(5, 1fr); margin-top: 38px; border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }}
.hierarchy-item {{ min-width: 0; padding: 22px 14px; background: var(--paper-2); border-right: 1px solid var(--line); text-align: center; font-family: var(--heading); }} .hierarchy-item:last-child {{ border-right: 0; }}
.hierarchy-arrow {{ display: block; margin-top: 8px; color: var(--accent); font-family: var(--mono); font-size: .68rem; }}
.quorum {{ display: inline-flex; align-items: center; gap: 8px; margin-top: 22px; color: var(--grant, var(--accent)); font-family: var(--mono); font-size: .72rem; }}
@media (max-width: 820px) {{
  .nav-in {{ gap: 12px; }} .nav-in > .btn {{ display: none; }} .brand small {{ display: none; }}
  .product-neotoma .record-hero, .product-ateles .seal-hero {{ grid-template-columns: 1fr; }}
  .product-neotoma .source-section {{ display: block; }} .product-neotoma .source-section::before {{ display: block; margin-bottom: 22px; }}
  .product-neotoma .source-section > .source-inner {{ display: block; }}
  .pain-grid, .product-ateles .capability-list {{ grid-template-columns: 1fr; }}
  .hierarchy {{ grid-template-columns: 1fr; }} .hierarchy-item {{ border-right: 0; border-bottom: 1px solid var(--line); text-align: left; }} .hierarchy-item:last-child {{ border-bottom: 0; }}
}}
@media (max-width: 620px) {{
  .nav-in {{ padding-inline: 16px; }} .nav-links {{ gap: 14px; }} .tw-toggle {{ display: none; }}
  .product-neotoma .record-demo {{ padding-left: 44px; }} .product-neotoma .capability {{ grid-template-columns: 1fr; gap: 8px; }} .step {{ grid-template-columns: 1fr; gap: 6px; }}
}}
""".strip()


def _extract_markdown_sections(body: str, headings: list[str]) -> str:
    if not headings:
        return body
    wanted = {heading.casefold() for heading in headings}
    parts = re.split(r"(?m)^(##\s+.+)$", body)
    selected: list[str] = []
    for index in range(1, len(parts), 2):
        heading_line = parts[index]
        heading = re.sub(r"^##\s+", "", heading_line).strip()
        if heading.casefold() in wanted:
            selected.extend(
                (heading_line, parts[index + 1] if index + 1 < len(parts) else "")
            )
    return "\n".join(selected).strip()


def _source_note(resolved: dict) -> str:
    entity = resolved.get("source_entity_id")
    entity_part = f" · {_esc(entity)}" if entity else ""
    return f'<p class="source-note">SOURCE · {_esc(resolved["source_path"])}{entity_part}</p>'


def _pain_cards(body: str) -> str:
    found: dict[str, str] = {}
    for name in ("Chronic", "Acute"):
        match = re.search(
            rf"(?ms)^\s*{name}:\s*(.+?)(?=^\s*(?:Chronic|Acute|Leads):|\Z)", body
        )
        if match:
            found[name] = match.group(1).strip()
    if len(found) != 2:
        return mdlib.to_html(body)
    cards = []
    for name, label in (
        ("Chronic", "The daily tax"),
        ("Acute", "The converting failure"),
    ):
        cards.append(
            f'<article class="pain-card"><p class="pain-kind">{label}</p><div class="markdown-body">{mdlib.to_html(found[name])}</div></article>'
        )
    return f'<div class="pain-grid">{"".join(cards)}</div>'


def _render_source_section(section_id: str, resolved: dict) -> str:
    section = resolved.get("section", {})
    _, body = mdlib.strip_frontmatter(resolved["markdown"])
    selected = _extract_markdown_sections(body, section.get("headings", []))
    content = (
        _pain_cards(selected or body)
        if section.get("presentation") == "pain_cards"
        else mdlib.to_html(selected or body, link_base=resolved.get("link_base"))
    )
    intro = (
        f'<p class="eyebrow">{_esc(section.get("eyebrow"))}</p>'
        if section.get("eyebrow")
        else ""
    )
    intro += f"<h2>{_esc(section['heading'])}</h2>" if section.get("heading") else ""
    intro += (
        f'<p class="lede" style="margin-top:18px">{_esc(section["lede"])}</p>'
        if section.get("lede")
        else ""
    )
    label = section.get(
        "source_label", resolved.get("source_entity_id", resolved["source_path"])
    )
    return f'<section class="wrap source-section" id="{_esc(section_id)}" data-source-label="{_esc(label)}"><div class="source-inner"><div class="source-heading">{intro}</div><div class="markdown-body">{content}</div>{_source_note(resolved)}</div></section>'


def _hero_ctas(page: dict, data: dict) -> str:
    primary = data.get("primary_cta") or page.get("primary_cta") or {}
    links = []
    if primary:
        links.append(
            f'<a class="btn" href="{_esc(primary.get("href", "#"))}">{_esc(primary.get("label", "Continue"))}</a>'
        )
    secondary = data.get("secondary_cta")
    if secondary:
        links.append(
            f'<a class="btn ghost" href="{_esc(secondary["href"])}">{_esc(secondary["label"])}</a>'
        )
    return f'<div class="cta-row">{"".join(links)}</div>' if links else ""


def _record_hero(page: dict, data: dict) -> str:
    proof = data.get("proof", {})
    return f"""<section class="wrap hero record-hero" id="hero"><div class="hero-copy">
<p class="eyebrow category">{_esc(data.get("category"))}</p><h1>{_esc(data["headline"])}</h1>
<p class="lede">{_esc(data["body"])}</p>{_hero_ctas(page, data)}<p class="proof-line">{_esc(data.get("footnote"))}</p></div>
<aside class="record-demo" aria-label="A corrected fact with provenance">
<div class="record-row"><span class="record-key">{_esc(proof.get("field", "decision.status"))}</span><span class="record-value record-old">{_esc(proof.get("old", "approved"))}</span></div>
<div class="record-row"><span class="record-key">current value</span><span class="record-value record-current">{_esc(proof.get("current", "superseded"))}</span><p class="record-stamp">{_esc(proof.get("provenance", "source · correction · effective time"))}</p></div>
<div class="record-row"><span class="record-key">read policy</span><span class="record-value record-current">{_esc(proof.get("read", "current as of now"))}</span></div></aside></section>"""


def _seal_svg() -> str:
    return """<svg viewBox="0 0 120 120" role="img" aria-label="Bounded authority seal"><circle cx="60" cy="60" r="52" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="60" cy="60" r="43" fill="none" stroke="currentColor" stroke-width="1" stroke-dasharray="2 5"/><path d="M38 74 60 31l22 43M47 58h26" fill="none" stroke="currentColor" stroke-width="6" stroke-linecap="square"/><path d="m28 87 9 3 5 9M92 87l-9 3-5 9" fill="none" stroke="currentColor" stroke-width="2"/></svg>"""


def _seal_hero(page: dict, data: dict) -> str:
    grants = "".join(
        f'<div class="grant-row"><span>{_esc(item["label"])}</span><span class="grant-state">{_esc(item["state"])}</span></div>'
        for item in data.get("grants", [])
    )
    return f"""<section class="wrap hero seal-hero" id="hero"><div class="hero-copy">
<p class="eyebrow category">{_esc(data.get("category"))}</p><h1>{_esc(data["headline"])}</h1><p class="lede">{_esc(data["body"])}</p>
{_hero_ctas(page, data)}<p class="proof-line">{_esc(data.get("footnote"))}</p></div>
<aside class="seal-board" aria-label="Example bounded delegation"><div class="seal-mark">{_seal_svg()}</div><p class="scope-chip">{_esc(data.get("scope", "scope · action · time"))}</p>{grants}
<div class="quorum" aria-label="Two of three approval threshold"><span>●</span><span>●</span><span>○</span><span>quorum · 2 / 3</span></div></aside></section>"""


def _render_capabilities(section_id: str, data: dict) -> str:
    items = "".join(
        f'<article class="capability"><p class="meta">{_esc(item.get("label", f"0{index}"))}</p><h3>{_esc(item["title"])}</h3><p>{_esc(item["body"])}</p><p class="scope-line">{_esc(item.get("terms"))}</p></article>'
        for index, item in enumerate(data.get("items", []), start=1)
    )
    correction = ""
    if data.get("correction"):
        correction = f'<div class="correction-demo"><span class="record-old">{_esc(data["correction"]["old"])}</span> → <span>{_esc(data["correction"]["current"])}</span><p class="small muted">{_esc(data["correction"]["note"])}</p></div>'
    status = (
        f'<div class="status-note">{_esc(data["status_note"])}</div>'
        if data.get("status_note")
        else ""
    )
    return f'<section class="wrap" id="{_esc(section_id)}"><p class="eyebrow">{_esc(data.get("eyebrow"))}</p><h2>{_esc(data["headline"])}</h2><p class="lede" style="margin-top:18px">{_esc(data.get("lede"))}</p><div class="capability-list">{items}</div>{correction}{status}</section>'


def _render_hierarchy(section_id: str, data: dict) -> str:
    chain = "".join(
        f'<div class="hierarchy-item">{_esc(item)}<span class="hierarchy-arrow">{"↓" if index < len(data["levels"]) - 1 else "act"}</span></div>'
        for index, item in enumerate(data["levels"])
    )
    return f'<section class="wrap" id="{_esc(section_id)}"><p class="eyebrow">{_esc(data.get("eyebrow"))}</p><h2>{_esc(data["headline"])}</h2><p class="lede" style="margin-top:18px">{_esc(data["lede"])}</p><div class="hierarchy" aria-label="Planning hierarchy">{chain}</div><p class="source-note">{_esc(data.get("source_note"))}</p></section>'


def _render_steps(section_id: str, data: dict) -> str:
    steps = "".join(
        f'<article class="step"><span class="step-num">{index:02d}</span><div><h3>{_esc(item["title"])}</h3><p>{_esc(item["body"])}</p></div></article>'
        for index, item in enumerate(data.get("items", []), start=1)
    )
    return f'<section class="wrap" id="{_esc(section_id)}"><p class="eyebrow">{_esc(data.get("eyebrow"))}</p><h2>{_esc(data["headline"])}</h2><p class="lede" style="margin-top:18px">{_esc(data.get("lede"))}</p><div class="steps">{steps}</div></section>'


def _render_cards(section_id: str, data: dict) -> str:
    cards = "".join(
        f'<article class="card"><h3>{_esc(item["title"])}</h3><p>{_esc(item["body"])}</p>'
        + (f'<p class="meta">{_esc(item["meta"])}</p>' if item.get("meta") else "")
        + (
            f'<a href="{_esc(item["href"])}">{_esc(item.get("link_label", "Read more"))}</a>'
            if item.get("href")
            else ""
        )
        + "</article>"
        for item in data.get("items", [])
    )
    return f'<section class="wrap" id="{_esc(section_id)}"><p class="eyebrow">{_esc(data.get("eyebrow"))}</p><h2>{_esc(data["headline"])}</h2><p class="lede" style="margin-top:18px">{_esc(data.get("lede"))}</p><div class="cards">{cards}</div></section>'


def _render_page_specific(product: str, page: dict, section_id: str, data: dict) -> str:
    layout = data.get("layout", section_id)
    if layout == "hero":
        return f'<section class="wrap hero" id="hero"><div class="hero-copy"><h1>{_esc(data["headline"])}</h1><p class="lede">{_esc(data.get("subheadline"))}</p><p>{_esc(data.get("body"))}</p>{_hero_ctas(page, data)}</div></section>'
    if layout == "record_hero":
        return _record_hero(page, data)
    if layout == "seal_hero":
        return _seal_hero(page, data)
    if layout == "capabilities":
        return _render_capabilities(section_id, data)
    if layout == "hierarchy":
        return _render_hierarchy(section_id, data)
    if layout == "steps":
        return _render_steps(section_id, data)
    if layout in ("cards", "comparison_teaser", "sibling"):
        return _render_cards(section_id, data)
    if layout == "cta_banner":
        return f'<section class="wrap" id="{_esc(section_id)}"><div class="panel"><p class="eyebrow">{_esc(data.get("eyebrow"))}</p><h2>{_esc(data["headline"])}</h2><p class="lede" style="margin-top:18px">{_esc(data.get("body"))}</p>{_hero_ctas(page, data)}</div></section>'
    import json as _json

    return f'<section class="wrap" id="{_esc(section_id)}"><div class="blocker">No renderer for page-specific layout {_esc(layout)}</div><pre>{_esc(_json.dumps(data, indent=2))}</pre></section>'


def _render_section(product: str, page: dict, section_id: str, resolved: dict) -> str:
    if not resolved["_resolved"]:
        return f'<section class="wrap" id="{_esc(section_id)}"><div class="blocker">BLOCKED — {_esc(resolved["_blocker"])}</div></section>'
    if resolved["origin"] == "page_specific":
        return _render_page_specific(product, page, section_id, resolved["data"])
    if resolved["origin"] in ("authored", "positioning_mirror"):
        return _render_source_section(section_id, resolved)
    return f'<section class="wrap" id="{_esc(section_id)}"><div class="blocker">Unknown content origin.</div></section>'


def _font_link(product: str) -> str:
    if product == "neotoma":
        family = "Fraunces:opsz,wght@9..144,500;9..144,600&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600"
    else:
        family = "Space+Grotesk:wght@500;600;700&family=Source+Sans+3:wght@400;500;600&family=IBM+Plex+Mono:wght@400;600"
    return f'<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family={family}&display=swap" rel="stylesheet">'


def render_page(
    product: str,
    page: dict,
    site_pages: list[dict],
    sections: list[tuple[str, dict]],
    tokens: dict,
) -> str:
    css = build_css(tokens, product)
    body_sections = '\n<hr class="rule">\n'.join(
        _render_section(product, page, section_id, resolved)
        for section_id, resolved in sections
    )
    primary_cta = page.get("primary_cta") or {}
    nav_cta = (
        f'<a class="btn" href="{_esc(primary_cta.get("href"))}">{_esc(primary_cta.get("label"))}</a>'
        if primary_cta
        else ""
    )
    site_nav = "".join(
        f'<a href="{_esc("/" if item["slug"] == "index" else "/" + item["slug"] + "/")}">{_esc(item.get("nav_label") or item["slug"].title())}</a>'
        for item in site_pages
    )
    theme_toggle = """<form class="tw-toggle" aria-label="Theme"><input type="radio" name="tw-theme" id="tw-system" checked><label for="tw-system">System</label><input type="radio" name="tw-theme" id="tw-light"><label for="tw-light">Light</label><input type="radio" name="tw-theme" id="tw-dark"><label for="tw-dark">Dark</label></form>"""
    noun = (
        "The system of record for AI agents."
        if product == "neotoma"
        else "The distributed-authority operating layer for governed initiative."
    )
    identity = "the record" if product == "neotoma" else "the seal"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(page.get("title", product.title()))}</title><meta name="description" content="{_esc(page.get("meta_description", ""))}">
{_font_link(product)}<style>{css}</style></head><body class="product-{_esc(product)}">
<header class="nav" id="site-nav"><div class="nav-in"><a class="brand" href="/">{_esc(product.title())}<small>{identity}</small></a><nav class="nav-links" aria-label="Primary">{site_nav}</nav>{nav_cta}{theme_toggle}</div></header>
<main>{body_sections}</main><footer><div class="wrap footer-in"><p class="footer-noun">{noun}</p><div class="flinks">{site_nav}</div></div></footer>
<script>const nav=document.getElementById('site-nav');const syncNav=()=>nav.classList.toggle('scrolled',scrollY>12);syncNav();addEventListener('scroll',syncNav,{{passive:true}});</script></body></html>
"""
