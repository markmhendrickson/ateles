"""render.py — turn resolved page/section data + design tokens into a
complete static HTML document.

No third-party templating engine (see build_site.py's module docstring for
why) — this module builds HTML with plain Python string joining. It is
intentionally boring: each function takes already-resolved data and returns
an HTML string, with no file I/O and no Neotoma awareness, so it can be unit
tested against fixed inputs (see test_build_site.py).

FULL WEB CAPABILITY: this output is a real static site, not a CSP-sandboxed
rendered_page. Nothing here strips external font sources, scripts, or SVG —
a page can use normal web capabilities when its inventory and template call
for them. This first proof stays CSS-and-HTML only.
"""

from __future__ import annotations

import html as html_mod

from . import minimal_markdown as mdlib


def _esc(s: str) -> str:
    return html_mod.escape(s or "", quote=True)


def _css_vars(mode_tokens: dict) -> str:
    """Render one color-mode's token dict (light or dark) as `--name: value;`
    declarations. Keys match the design_system entity's own field names
    (ink, ink_2, paper, accent, ...) so the mapping from Neotoma field to CSS
    custom property is a straight rename, not a re-derivation."""
    order = [
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
        "warn",
    ]
    decls = []
    for key in order:
        if key in mode_tokens:
            var_name = "--" + key.replace("_", "-")
            decls.append(f"{var_name}: {mode_tokens[key]};")
    return "\n    ".join(decls)


def build_css(tokens: dict) -> str:
    """Assemble the page <style> block from shared spacing/radius tokens plus
    the product's per-mode color palette and type scale. Every value here
    traces to design_tokens/<product>.json — nothing is invented in this
    function."""
    shared = tokens["shared"]
    product = tokens["product"]
    palette = product["color_palette"]
    type_scale = product["type_scale"]
    spacing = shared["spacing_system"]
    radius = shared["border_radius"]

    light = palette.get("light", {})
    dark = palette.get("dark", {})

    heading_font = type_scale.get("heading_font_family", "system-ui, sans-serif")
    body_font = type_scale.get("body_font_family", "system-ui, sans-serif")
    code_font = type_scale.get("code_font_family", "ui-monospace, monospace")
    h1_size = type_scale.get("h1_size", "clamp(2rem, 6vw, 3.4rem)")
    h2_size = type_scale.get("h2_size", "clamp(1.4rem, 4vw, 2rem)")
    body_size = type_scale.get("body_base_size", "16px")
    heading_weight = type_scale.get("heading_weight", 700)

    root_radius = (
        radius.get("root_radius_variable", "--radius: 10px")
        .split(":")[-1]
        .strip()
        .rstrip(";")
    )
    max_w = "1080px"
    if "container" in spacing and "--maxw:" in spacing["container"]:
        max_w = spacing["container"].split("--maxw:")[1].split(";")[0].strip()

    return f"""
:root {{
    {_css_vars(light)}
    --radius: {root_radius};
    --maxw: {max_w};
    --shadow: 0 1px 2px rgba(0,0,0,.06), 0 10px 26px rgba(0,0,0,.06);
  }}
  :root:not([data-theme="light"]) {{ color-scheme: light; }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      {_css_vars(dark)}
      --shadow: 0 1px 2px rgba(0,0,0,.35), 0 10px 26px rgba(0,0,0,.28);
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    {_css_vars(dark)}
    --shadow: 0 1px 2px rgba(0,0,0,.35), 0 10px 26px rgba(0,0,0,.28);
  }}
  html {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--paper); color: var(--ink);
    font-family: {body_font};
    font-size: {body_size}; line-height: 1.6; -webkit-font-smoothing: antialiased;
  }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ font-family: {code_font}; font-size: .88em; background: var(--paper-2); padding: .1em .35em; border-radius: 4px; }}
  .wrap {{ max-width: var(--maxw); margin: 0 auto; padding-inline: 20px; }}
  section {{ padding-block: 56px; }}
  .rule {{ border: 0; border-top: 1px solid var(--line); margin: 0; }}
  h1, h2 {{ font-family: {heading_font}; font-weight: {heading_weight}; letter-spacing: -.02em; line-height: 1.12; margin: 0; }}
  h1 {{ font-size: {h1_size}; max-width: 18ch; }}
  h2 {{ font-size: {h2_size}; }}
  h3 {{ font-family: {heading_font}; font-size: 1.05rem; font-weight: 600; margin: 0 0 8px; }}
  p {{ margin: 0 0 1rem; }}
  .lede {{ font-size: clamp(1.02rem, 2.4vw, 1.16rem); color: var(--ink-2); max-width: 63ch; }}
  .eyebrow {{ font-size: .72rem; letter-spacing: .14em; text-transform: uppercase; font-weight: 600; color: var(--accent); margin-bottom: 14px; }}
  .muted {{ color: var(--ink-3); }}
  .small {{ font-size: .89rem; }}
  header.nav {{ position: sticky; top: 0; z-index: 20; background: var(--paper); background: color-mix(in srgb, var(--paper) 88%, transparent); border-bottom: 1px solid var(--line); backdrop-filter: blur(14px); }}
  .nav-in {{ width: 100%; display: flex; align-items: center; gap: 18px; min-height: 58px; flex-wrap: nowrap; padding: 10px clamp(20px, 3vw, 48px); }}
  .brand {{ font-family: {heading_font}; font-weight: {heading_weight}; color: var(--ink); white-space: nowrap; }}
  .brand:hover {{ text-decoration: none; }}
  .nav-links {{ display: flex; gap: 16px; margin-left: auto; overflow-x: auto; white-space: nowrap; font-size: .92rem; }}
  .nav-links a {{ color: var(--ink-2); }}
  .hero {{ padding-block: clamp(48px, 10vw, 96px) 48px; }}
  .cta-row {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 26px; }}
  .btn {{ display: inline-block; padding: 11px 20px; border-radius: 8px; font-weight: 600; font-size: .94rem; border: 1px solid var(--accent); background: var(--accent); color: #fffdf8; }}
  .btn.ghost {{ background: transparent; color: var(--ink); border-color: var(--line); }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(258px, 1fr)); gap: 18px; margin-top: 26px; }}
  .card {{ background: var(--paper-2); border: 1px solid var(--line); border-radius: var(--radius); padding: 22px; box-shadow: var(--shadow); }}
  .panel {{ background: var(--paper-2); border: 1px solid var(--line); border-radius: var(--radius); padding: 22px; box-shadow: var(--shadow); }}
  .two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 24px; }}
  @media (max-width: 720px) {{ .two {{ grid-template-columns: 1fr; }} }}
  .caps {{ margin-top: 30px; border-top: 1px solid var(--line); }}
  .cap {{ display: grid; grid-template-columns: 10px 1fr; gap: 20px; padding: 24px 0; border-bottom: 1px solid var(--line); }}
  .cap-tick {{ width: 3px; border-radius: 2px; background: var(--accent); margin-top: 8px; align-self: stretch; }}
  .cap .terms {{ margin-top: 8px; font-size: .85rem; color: var(--ink-3); }}
  .sibling {{ display: grid; grid-template-columns: 1fr 1fr; border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; margin-top: 24px; background: var(--paper-2); }}
  .sibling > div {{ padding: 20px; }}
  .sibling > div + div {{ border-left: 1px solid var(--line); }}
  @media (max-width: 640px) {{ .sibling {{ grid-template-columns: 1fr; }} .sibling > div + div {{ border-left: 0; border-top: 1px solid var(--line); }} }}
  .status-note, .blocker {{ margin-top: 24px; padding: 14px 16px; border-left: 3px solid var(--accent); background: var(--paper-2); color: var(--ink-2); font-size: .92rem; border-radius: 0 8px 8px 0; }}
  .blocker {{ border-left-color: #c0392b; color: #c0392b; font-family: {code_font}; font-size: .85rem; }}
  footer {{ border-top: 1px solid var(--line); padding-block: 36px 44px; background: var(--paper-2); }}
  footer .cols {{ display: flex; gap: 24px; flex-wrap: wrap; justify-content: space-between; }}
  .flinks {{ display: flex; gap: 16px; flex-wrap: wrap; font-size: .9rem; }}
  .tw-toggle {{ flex: 0 0 auto; display: flex; gap: 2px; background: var(--paper-2); border: 1px solid var(--line); border-radius: 999px; padding: 3px; box-shadow: var(--shadow); }}
  .tw-toggle input {{ position: absolute; opacity: 0; width: 1px; height: 1px; }}
  .tw-toggle label {{ display: inline-flex; align-items: center; justify-content: center; min-width: 30px; height: 24px; padding: 0 8px; border-radius: 999px; font-size: .72rem; font-weight: 600; color: var(--ink-3); cursor: pointer; user-select: none; }}
  body:has(#tw-light:checked) .tw-toggle label[for="tw-light"],
  body:has(#tw-dark:checked) .tw-toggle label[for="tw-dark"],
  body:has(#tw-system:checked) .tw-toggle label[for="tw-system"] {{ background: var(--accent); color: #fffdf8; }}
  body:has(#tw-light:checked) {{ {_css_vars(light)} color-scheme: light; }}
  body:has(#tw-dark:checked) {{ {_css_vars(dark)} color-scheme: dark; }}
  .markdown-body :is(h1,h2,h3) {{ margin-top: 1.4em; }}
  .markdown-body ul {{ padding-left: 20px; color: var(--ink-2); }}
  .markdown-body ol {{ padding-left: 24px; color: var(--ink-2); }}
  .markdown-body blockquote {{ margin: 24px 0; padding: 4px 18px; border-left: 3px solid var(--accent); color: var(--ink-2); background: var(--paper-2); }}
  .markdown-body pre {{ overflow-x: auto; padding: 18px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--paper-2); }}
  .markdown-body pre code {{ padding: 0; background: transparent; }}
  .table-scroll {{ overflow-x: auto; margin: 22px 0; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .92rem; }}
  th, td {{ padding: 10px 12px; border: 1px solid var(--line); text-align: left; vertical-align: top; }}
  th {{ background: var(--paper-2); font-family: {heading_font}; }}
  @media (max-width: 760px) {{
    .nav-in {{ gap: 12px; }}
    .nav-in > .btn {{ display: none; }}
    .tw-toggle label {{ min-width: 26px; padding-inline: 6px; font-size: 0; }}
    .tw-toggle label::first-letter {{ font-size: .72rem; }}
  }}
""".strip()


def _render_section(product: str, page: dict, section_id: str, resolved: dict) -> str:
    if not resolved["_resolved"]:
        return (
            f'<section class="wrap" id="{_esc(section_id)}">'
            f'<div class="blocker">BLOCKED — {_esc(resolved["_blocker"])}</div>'
            f"</section>"
        )

    origin = resolved["origin"]

    if origin == "page_specific":
        return _render_page_specific(section_id, resolved["data"])

    if origin in ("authored", "positioning_mirror"):
        fm, body = mdlib.strip_frontmatter(resolved["markdown"])
        body_html = mdlib.to_html(body, link_base=resolved.get("link_base"))
        source_note = (
            f'<p class="small muted">Rendered from '
            f"<code>{_esc(resolved['source_path'])}</code>"
            + (
                f" (source: {_esc(resolved['source_entity_id'])})"
                if resolved.get("source_entity_id")
                else ""
            )
            + "</p>"
        )
        title = fm.get("title", "")
        heading = f"<h2>{_esc(title)}</h2>" if title else ""
        return (
            f'<section class="wrap markdown-body" id="{_esc(section_id)}">'
            f"{heading}{body_html}{source_note}"
            f"</section>"
        )

    return f'<section class="wrap" id="{_esc(section_id)}"><div class="blocker">unknown origin</div></section>'


def _render_page_specific(section_id: str, data: dict) -> str:
    if section_id == "hero":
        sub = (
            f'<p class="sub" style="margin-top:16px;font-weight:600">{_esc(data["subheadline"])}</p>'
            if data.get("subheadline")
            else ""
        )
        body = (
            f'<p class="lede" style="margin-top:16px">{_esc(data["body"])}</p>'
            if data.get("body")
            else ""
        )
        sec_cta = ""
        if data.get("secondary_cta"):
            sec_cta = f'<a class="btn ghost" href="{_esc(data["secondary_cta"]["href"])}">{_esc(data["secondary_cta"]["label"])}</a>'
        footnote = (
            f'<p class="small muted" style="margin-top:20px">{_esc(data["footnote"])}</p>'
            if data.get("footnote")
            else ""
        )
        return f"""
<section class="wrap hero" id="hero">
  <h1>{_esc(data["headline"])}</h1>
  {sub}
  {body}
  <div class="cta-row">
    <a class="btn" href="#what-it-is">Read more</a>
    {sec_cta}
  </div>
  {footnote}
</section>""".strip()

    if section_id == "what-it-is":
        eyebrow = (
            f'<p class="eyebrow">{_esc(data["eyebrow"])}</p>'
            if data.get("eyebrow")
            else ""
        )
        caps = "".join(
            f"""<div class="cap"><div class="cap-tick"></div><div>
  <h3>{_esc(c["title"])}</h3><p>{_esc(c["body"])}</p>
  <p class="terms">{_esc(c.get("terms", ""))}</p>
</div></div>"""
            for c in data.get("capabilities", [])
        )
        status = (
            f'<div class="status-note"><strong>What this page is.</strong> {_esc(data["status_note"])}</div>'
            if data.get("status_note")
            else ""
        )
        return f"""
<section class="wrap" id="what-it-is">
  {eyebrow}
  <h2>{_esc(data["headline"])}</h2>
  <p class="lede" style="margin-top:14px">{_esc(data["lede"])}</p>
  <div class="caps">{caps}</div>
  {status}
</section>""".strip()

    if section_id == "the-other-half":
        eyebrow = (
            f'<p class="eyebrow">{_esc(data["eyebrow"])}</p>'
            if data.get("eyebrow")
            else ""
        )
        cols = "".join(
            f"""<div><h3>{f'<a href="{_esc(s["href"])}">{_esc(s["title"])}</a>' if s.get("href") else _esc(s["title"])}</h3>
<p class="small" style="color:var(--ink-2)">{_esc(s["body"])}</p></div>"""
            for s in data.get("sibling", [])
        )
        closing = (
            f'<p class="lede" style="margin-top:20px">{_esc(data["closing"])}</p>'
            if data.get("closing")
            else ""
        )
        return f"""
<section class="wrap" id="the-other-half">
  {eyebrow}
  <h2>{_esc(data["headline"])}</h2>
  <p class="lede" style="margin-top:14px">{_esc(data["lede"])}</p>
  <div class="sibling">{cols}</div>
  {closing}
</section>""".strip()

    # Generic fallback: any other page_specific section renders as a labeled
    # JSON dump rather than silently disappearing — a missing renderer for a
    # NEW section id is a finding, not something this function should hide.
    import json as _json

    return f'<section class="wrap" id="{_esc(section_id)}"><pre>{_esc(_json.dumps(data, indent=2))}</pre></section>'


def render_page(
    product: str,
    page: dict,
    site_pages: list[dict],
    sections: list[tuple[str, dict]],
    tokens: dict,
) -> str:
    css = build_css(tokens)
    title = page.get("title", product)
    meta_description = page.get("meta_description", "")
    body_sections = '\n<hr class="rule">\n'.join(
        _render_section(product, page, sid, resolved) for sid, resolved in sections
    )

    primary_cta = page.get("primary_cta") or {}
    nav_cta = (
        f'<a class="btn" href="{_esc(primary_cta.get("href", "#"))}">{_esc(primary_cta.get("label", ""))}</a>'
        if primary_cta
        else ""
    )

    site_nav = "".join(
        f'<a href="{_esc("/" if p["slug"] == "index" else "/" + p["slug"] + "/")}">{_esc(p.get("nav_label") or ("Home" if p["slug"] == "index" else p["slug"].replace("-", " ").title()))}</a>'
        for p in site_pages
    )

    theme_toggle = """
<form class="tw-toggle" aria-label="Theme">
  <input type="radio" name="tw-theme" id="tw-system" checked>
  <label for="tw-system">System</label>
  <input type="radio" name="tw-theme" id="tw-light">
  <label for="tw-light">Light</label>
  <input type="radio" name="tw-theme" id="tw-dark">
  <label for="tw-dark">Dark</label>
</form>""".strip()

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(meta_description)}">
<style>{css}</style>
</head>
<body>
<header class="nav">
  <div class="nav-in">
    <a class="brand" href="/">{_esc(product.capitalize())}</a>
    <nav class="nav-links">
      {site_nav}
    </nav>
    {nav_cta}
    {theme_toggle}
  </div>
</header>
{body_sections}
<footer>
  <div class="wrap cols">
    <p class="small muted">Built by execution/scripts/site_generator/build_site.py from the page inventory at execution/scripts/site_generator/inventory/{_esc(product)}.json — repo files only, no content authored at build time.</p>
    <div class="flinks">
      {"".join(f'<a href="#{_esc(sid)}">{_esc(sid)}</a>' for sid, _ in sections)}
    </div>
  </div>
</footer>
</body>
</html>
"""
