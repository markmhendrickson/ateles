"""Render source-backed product sites with distinct record/swarm identities."""

from __future__ import annotations

import html as html_mod
import re
from urllib.parse import quote_plus

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
.page-title {{ font-size: clamp(2.7rem, 6vw, 4.8rem); max-width: 18ch; }}
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
.brand {{ display: inline-flex; align-items: center; gap: 9px; }}
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
.card .card-label {{ margin: 0 0 14px; color: var(--accent); font-family: var(--mono); font-size: .68rem; letter-spacing: .08em; text-transform: uppercase; }}
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
.public-flow {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr)); gap: 0; margin-top: 36px; border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }}
.flow-step {{ position: relative; min-height: 210px; padding: 26px; background: var(--paper-2); border-right: 1px solid var(--line); }} .flow-step:last-child {{ border-right: 0; }}
.flow-label {{ color: var(--accent); font-family: var(--mono); font-size: .68rem; letter-spacing: .08em; }}
.flow-step:not(:last-child)::after {{ content: "→"; position: absolute; right: -12px; top: 50%; z-index: 2; display: grid; place-items: center; width: 24px; height: 24px; border: 1px solid var(--line); border-radius: 50%; background: var(--paper); color: var(--accent); }}
.comparison-wrap {{ margin-top: 36px; overflow-x: auto; border: 1px solid var(--line); border-radius: var(--radius); }}
.comparison-grid {{ min-width: 680px; }} .comparison-row {{ display: grid; grid-template-columns: .8fr 1fr 1.2fr; }}
.comparison-row > * {{ padding: 16px 18px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }} .comparison-row > *:last-child {{ border-right: 0; }} .comparison-row:last-child > * {{ border-bottom: 0; }}
.comparison-head {{ background: var(--ink); color: var(--paper); font-family: var(--mono); font-size: .68rem; letter-spacing: .06em; text-transform: uppercase; }}
.comparison-body {{ color: var(--ink-2); background: var(--paper-2); }} .comparison-body > :first-child {{ color: var(--ink); font-weight: 700; }}
.concept-film {{ position: relative; min-width: 0; margin: 0; overflow: hidden; border-radius: var(--radius); }}
.concept-film-poster {{ position: relative; z-index: 1; }}
.concept-film-poster-media {{ display: block; width: 100%; height: 100%; object-fit: cover; }}
.concept-film-media {{ position: absolute; inset: 0; z-index: 2; width: 100%; height: 100%; border: 0; object-fit: cover; background: var(--paper-2); }}
.concept-film-overlay {{ position: absolute; inset: 0; z-index: 3; pointer-events: none; }}
.concept-film-overlay svg {{ display: block; width: 100%; height: 100%; }}
.visual-section {{ padding-block: clamp(76px, 11vw, 144px); }}
.section-visual {{ position: relative; min-height: clamp(330px, 48vw, 620px); margin: 0 0 clamp(34px, 5vw, 64px); overflow: hidden; border: 1px solid var(--line); border-radius: calc(var(--radius) * 1.35); background: color-mix(in srgb, var(--paper-2) 88%, var(--accent-wash)); box-shadow: var(--shadow-soft); }}
.section-visual svg {{ display: block; width: 100%; height: 100%; min-height: inherit; }}
.section-visual text {{ fill: var(--ink-2); font-family: var(--mono); font-size: 22px; letter-spacing: .04em; }}
.section-visual .visual-caption {{ font-size: 16px; fill: var(--ink-3); }}
.section-copy {{ max-width: 760px; }}
.section-copy .lede {{ margin-top: 18px; }}
.section-link {{ display: inline-flex; align-items: center; gap: 10px; margin-top: 12px; font-weight: 700; text-decoration: none; }}
.section-link span {{ transition: transform .2s ease; }} .section-link:hover span {{ transform: translateX(4px); }}
.section-evidence {{ margin-top: 34px; }}
.visual-edge {{ stroke: var(--line-2, var(--line)); stroke-width: 1.5; fill: none; }}
.visual-pulse {{ stroke: var(--accent); stroke-width: 2.5; fill: none; stroke-linecap: round; stroke-dasharray: 7 19; animation: signal-flow 7s linear infinite; }}
.visual-record {{ fill: var(--paper); stroke: var(--accent); stroke-width: 2; }}
.visual-record-muted {{ fill: var(--paper-2); stroke: var(--line-2, var(--line)); stroke-width: 1.5; }}
.visual-agent {{ fill: var(--accent); }}
.visual-agent-label {{ fill: var(--on-accent) !important; font-weight: 700; text-anchor: middle; }}
.visual-version {{ fill: var(--paper); stroke: var(--correction, var(--accent)); stroke-width: 1.6; }}
.visual-swarm-member {{ fill: var(--paper); stroke: var(--accent); stroke-width: 2; transform-box: fill-box; transform-origin: center; animation: swarm-breathe 5s ease-in-out infinite alternate; }}
.visual-swarm-member:nth-of-type(2n) {{ animation-delay: -1.7s; }}
.visual-signal {{ stroke: var(--accent); stroke-width: 2.5; fill: none; stroke-linecap: round; stroke-dasharray: 18 18; animation: temporary-handoff 4.8s ease-in-out infinite; }}
.visual-signal:nth-of-type(2n) {{ animation-delay: -2.2s; }}
.visual-checkpoint {{ fill: color-mix(in srgb, var(--grant) 18%, var(--paper)); stroke: var(--grant); stroke-width: 2; }}
@keyframes signal-flow {{ to {{ stroke-dashoffset: -104; }} }}
@keyframes swarm-breathe {{ to {{ transform: translate3d(0, -7px, 0) rotate(2deg); }} }}
@keyframes temporary-handoff {{ 0%, 18%, 82%, 100% {{ opacity: 0; stroke-dashoffset: 40; }} 35%, 62% {{ opacity: 1; }} 75% {{ stroke-dashoffset: -36; }} }}
.status-note, .blocker {{ margin-top: 26px; padding: 16px 18px; border-left: 3px solid var(--accent); background: var(--paper-2); color: var(--ink-2); }}
.blocker {{ color: var(--revoked, #9c4a3a); border-color: currentColor; font-family: var(--mono); }}
.tw-toggle {{ display: flex; gap: 2px; flex: 0 0 auto; padding: 3px; border: 1px solid var(--line); border-radius: 999px; background: var(--paper-2); }}
.tw-toggle input {{ position: absolute; opacity: 0; pointer-events: none; }}
.tw-toggle label {{ display: grid; place-items: center; min-width: 28px; height: 26px; padding-inline: 7px; border-radius: 999px; color: var(--ink-3); cursor: pointer; font-size: .68rem; }}
body:has(#tw-light:checked) .tw-toggle label[for="tw-light"], body:has(#tw-dark:checked) .tw-toggle label[for="tw-dark"], body:has(#tw-system:checked) .tw-toggle label[for="tw-system"] {{ color: var(--on-accent, #fff); background: var(--accent); }}
body:has(#tw-light:checked) {{ {_css_vars(light)} color-scheme: light; }} body:has(#tw-dark:checked) {{ {_css_vars(dark)} color-scheme: dark; }}
footer {{ border-top: 1px solid var(--line); padding-block: 42px; }} .footer-in {{ display: flex; justify-content: space-between; gap: 30px; flex-wrap: wrap; }}
.footer-noun {{ max-width: 44ch; color: var(--ink-2); }} .flinks {{ display: flex; gap: 18px; flex-wrap: wrap; font-size: .86rem; }}
.brand-system-page {{ padding-block: clamp(70px, 9vw, 118px); }}
.brand-system-intro {{ display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(280px, .7fr); gap: clamp(30px, 6vw, 80px); align-items: end; }}
.brand-system-intro h1 {{ max-width: 15ch; }}
.brand-foundation-map {{ min-height: 300px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; margin-top: clamp(48px, 7vw, 82px); background: var(--line); }}
.brand-foundation-step {{ display: grid; align-content: end; min-height: 280px; padding: clamp(22px, 4vw, 42px); background: var(--paper-2); }} .brand-foundation-step strong {{ display: block; margin-top: 10px; font-family: var(--heading); font-size: clamp(1.4rem, 3vw, 2.5rem); line-height: 1; }}
.brand-summary {{ padding: 24px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--paper-2); }}
.brand-status {{ display: inline-flex; align-items: center; gap: 8px; padding: 5px 10px; border: 1px solid currentColor; border-radius: 999px; color: var(--ink-3); font-family: var(--mono); font-size: .65rem; letter-spacing: .07em; text-transform: uppercase; }}
.brand-status::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }}
.brand-status-approved {{ color: var(--grant, var(--accent)); }} .brand-status-provisional {{ color: var(--accent); }} .brand-status-missing {{ color: var(--revoked, var(--correction, var(--accent))); }}
.brand-group {{ margin-top: clamp(58px, 8vw, 100px); }} .brand-group > header {{ display: grid; grid-template-columns: .55fr 1.45fr; gap: 28px; margin-bottom: 30px; align-items: baseline; }}
.brand-group > header p {{ color: var(--ink-2); max-width: 64ch; }}
.brand-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr)); gap: 16px; }}
.brand-item {{ min-width: 0; padding: 22px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--paper-2); }}
.brand-item h3 {{ margin: 12px 0 10px; }} .brand-item p {{ color: var(--ink-2); }}
.brand-asset-preview {{ aspect-ratio: 16/10; margin: -22px -22px 20px; overflow: hidden; border-bottom: 1px solid var(--line); border-radius: var(--radius) var(--radius) 0 0; background: var(--paper); }}
.brand-asset-preview :is(img,video) {{ display: block; width: 100%; height: 100%; object-fit: cover; }}
.brand-palette {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }}
.brand-swatch {{ min-height: 112px; display: flex; align-items: end; padding: 12px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--swatch); color: var(--swatch-label); font-family: var(--mono); font-size: .66rem; }}
.brand-rule-list {{ columns: 2 260px; column-gap: 34px; padding-left: 1.2rem; }} .brand-rule-list li {{ break-inside: avoid; margin-bottom: 12px; color: var(--ink-2); }}
.brand-source-list {{ display: flex; flex-wrap: wrap; gap: 10px; }} .brand-source-list span {{ padding: 8px 11px; border: 1px solid var(--line); border-radius: 999px; color: var(--ink-2); font-size: .82rem; }}
.brand-table {{ min-width: 980px; }} .brand-table th {{ text-align: left; color: var(--ink-3); font-family: var(--mono); font-size: .68rem; letter-spacing: .06em; text-transform: uppercase; }}
.brand-table td {{ vertical-align: top; color: var(--ink-2); }} .brand-table td:first-child {{ color: var(--ink); font-weight: 700; }}
.brand-table a {{ overflow-wrap: anywhere; }} .brand-intent-proof {{ columns: 2 280px; }}
.brand-gate-blocked {{ border-color: var(--revoked, var(--correction, var(--accent))); }}

/* Neotoma: a persistent graph with visible provenance and history. */
.takeover-hero {{ position: relative; min-height: min(850px, calc(100svh - 68px)); display: grid; align-items: center; overflow: hidden; isolation: isolate; padding: clamp(60px, 8vw, 110px) max(20px, calc((100vw - var(--maxw)) / 2 + clamp(20px, 4vw, 56px))); }}
.takeover-hero .concept-film {{ position: absolute; inset: 0; z-index: -2; border: 0; border-radius: 0; background: var(--paper-2); }}
.takeover-hero .concept-film-poster, .takeover-hero .hero-takeover-visual {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
.takeover-hero .hero-takeover-visual svg {{ width: 100%; height: 100%; }}
.takeover-hero::after {{ content: ""; position: absolute; inset: 0; z-index: -1; pointer-events: none; background: linear-gradient(90deg, var(--paper) 0%, color-mix(in srgb, var(--paper) 94%, transparent) 34%, color-mix(in srgb, var(--paper) 54%, transparent) 56%, transparent 78%); }}
.takeover-hero .hero-copy {{ width: min(100%, 650px); position: relative; z-index: 3; padding: clamp(24px, 4vw, 46px) 0; }}
.product-neotoma .record-demo {{ position: relative; border-top: 3px double var(--ink); border-bottom: 1px solid var(--line); padding: 28px 0 12px 68px; }}
.product-neotoma .record-demo::before {{ content: "RECORD"; position: absolute; left: 0; top: 30px; writing-mode: vertical-rl; transform: rotate(180deg); color: var(--ink-3); font-family: var(--mono); font-size: .62rem; letter-spacing: .14em; }}
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

/* Ateles: a swarm of distinct roles coordinating without permanent edges. */
.product-ateles .nav {{ border-top: 4px solid var(--accent); }}
.swarm-mark {{ width: 25px; height: 25px; color: var(--accent); flex: 0 0 auto; }} .swarm-mark :is(circle,rect,path) {{ fill: var(--paper); stroke: currentColor; stroke-width: 1.5; }} .swarm-mark .swarm-core {{ fill: currentColor; }}
.organization-board {{ position: absolute; inset: 0; background: radial-gradient(circle at 78% 46%, var(--accent-wash), transparent 34%), var(--paper-2); }}
.organization-head {{ display: flex; justify-content: space-between; gap: 18px; align-items: baseline; }}
.organization-count {{ color: var(--ink-3); font-family: var(--mono); font-size: .66rem; letter-spacing: .06em; white-space: nowrap; }}
.swarm-field {{ display: block; width: 100%; height: 100%; overflow: visible; }}
.swarm-member {{ transform-box: fill-box; transform-origin: center; animation: swarm-breathe 5s ease-in-out infinite alternate; }}
.swarm-member:nth-of-type(2n) {{ animation-delay: -2s; }}
.swarm-member :is(circle,rect,path) {{ fill: color-mix(in srgb, var(--paper) 90%, transparent); stroke: var(--accent); stroke-width: 2; }}
.swarm-member text {{ fill: var(--ink); font-family: var(--mono); font-size: 19px; text-anchor: middle; }}
.handoff-signal {{ fill: none; stroke: var(--accent); stroke-width: 3; stroke-linecap: round; stroke-dasharray: 24 30; animation: temporary-handoff 4.8s ease-in-out infinite; }}
.handoff-signal:nth-of-type(2n) {{ animation-delay: -2.2s; }}
.purpose-field {{ fill: none; stroke: var(--line-2, var(--line)); stroke-width: 1.5; stroke-dasharray: 3 10; }}
.purpose-label {{ fill: var(--ink-3); font-family: var(--mono); font-size: 17px; letter-spacing: .08em; }}
.coordination-states {{ position: absolute; right: clamp(20px, 5vw, 76px); bottom: clamp(24px, 5vw, 68px); z-index: 2; width: min(330px, calc(100% - 40px)); padding: 14px 18px; border: 1px solid var(--line); border-radius: var(--radius); background: color-mix(in srgb, var(--paper) 88%, transparent); backdrop-filter: blur(12px); box-shadow: var(--shadow-soft); }}
.grant-row {{ display: grid; grid-template-columns: 1fr auto; gap: 18px; align-items: center; padding: 12px 0; border-bottom: 1px solid var(--line); }} .grant-row:last-child {{ border-bottom: 0; }}
.state-mark {{ display: inline-flex; align-items: center; gap: 8px; }}
.authorization-seal {{ display: inline-grid; place-items: center; width: 21px; height: 21px; color: var(--accent); }}
.authorization-seal svg {{ display: block; width: 100%; height: 100%; }}
.grant-row[data-state="GRANTED"] .authorization-seal, .grant-row[data-state="GRANTED"] .grant-state {{ color: var(--grant, var(--accent)); }}
.grant-row[data-state="WITHHELD"] .authorization-seal, .grant-row[data-state="WITHHELD"] .grant-state {{ color: var(--revoked, var(--accent)); }}
.grant-state {{ color: var(--accent); font-family: var(--mono); font-size: .66rem; letter-spacing: .05em; }}
@media (prefers-reduced-motion: reduce) {{ .swarm-member, .handoff-signal, .visual-pulse, .visual-swarm-member, .visual-signal {{ animation: none; }} .concept-film-media, .concept-film-overlay {{ display: none; }} }}
.product-ateles .source-section > .source-inner {{ padding-left: clamp(20px, 5vw, 72px); border-left: 4px solid var(--accent); }}
.product-ateles .capability-list {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; margin-top: 38px; }}
.product-ateles .capability {{ position: relative; min-height: 220px; padding: 28px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--paper-2); }}
.product-ateles .capability::before {{ content: ""; position: absolute; right: 22px; top: 22px; width: 8px; height: 8px; border: 1px solid var(--accent); border-radius: 50%; background: var(--paper-2); }}
.product-ateles .capability::after {{ content: "ROLE NODE"; position: absolute; right: 38px; top: 18px; color: var(--ink-3); font-family: var(--mono); font-size: .56rem; letter-spacing: .08em; }}
.scope-line {{ margin-top: 16px; color: var(--ink-3); font-family: var(--mono); font-size: .72rem; }}
.hierarchy {{ display: grid; grid-template-columns: repeat(5, 1fr); margin-top: 38px; border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }}
.hierarchy-item {{ min-width: 0; padding: 22px 14px; background: var(--paper-2); border-right: 1px solid var(--line); text-align: center; font-family: var(--heading); }} .hierarchy-item:last-child {{ border-right: 0; }}
.hierarchy-arrow {{ display: block; margin-top: 8px; color: var(--accent); font-family: var(--mono); font-size: .68rem; }}
.quorum {{ display: inline-flex; align-items: center; gap: 8px; margin-top: 22px; color: var(--grant, var(--accent)); font-family: var(--mono); font-size: .72rem; }}
@media (max-width: 820px) {{
  .nav-in {{ gap: 12px; }} .nav-in > .btn {{ display: none; }} .brand small {{ display: none; }}
  .takeover-hero {{ min-height: 860px; align-items: start; padding-top: 54px; }} .takeover-hero::after {{ background: linear-gradient(180deg, var(--paper) 0%, color-mix(in srgb, var(--paper) 94%, transparent) 36%, color-mix(in srgb, var(--paper) 48%, transparent) 60%, transparent 82%); }}
  .takeover-hero .hero-takeover-visual svg {{ transform: translate(13%, 18%) scale(1.18); transform-origin: center; }}
  .product-ateles .takeover-hero .hero-takeover-visual svg {{ transform: translate(15%, 22%) scale(1.22); }}
  .coordination-states {{ bottom: 22px; }}
  .product-neotoma .source-section {{ display: block; }} .product-neotoma .source-section::before {{ display: block; margin-bottom: 22px; }}
  .product-neotoma .source-section > .source-inner {{ display: block; }}
  .pain-grid, .product-ateles .capability-list {{ grid-template-columns: 1fr; }}
  .hierarchy {{ grid-template-columns: 1fr; }} .hierarchy-item {{ border-right: 0; border-bottom: 1px solid var(--line); text-align: left; }} .hierarchy-item:last-child {{ border-bottom: 0; }}
  .flow-step {{ border-right: 0; border-bottom: 1px solid var(--line); }} .flow-step:last-child {{ border-bottom: 0; }} .flow-step:not(:last-child)::after {{ content: "↓"; right: 50%; top: auto; bottom: -12px; transform: translateX(50%); }}
  .brand-system-intro, .brand-group > header {{ grid-template-columns: 1fr; }}
  .brand-foundation-map {{ grid-template-columns: 1fr; }} .brand-foundation-step {{ min-height: 180px; }}
}}
@media (max-width: 620px) {{
  .nav-in {{ padding-inline: 16px; }} .nav-links {{ gap: 14px; }} .tw-toggle {{ display: none; }}
  .product-neotoma .record-demo {{ padding-left: 44px; }} .product-neotoma .capability {{ grid-template-columns: 1fr; gap: 8px; }} .step {{ grid-template-columns: 1fr; gap: 6px; }}
  .section-visual {{ min-height: 300px; margin-inline: -8px; }}
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


def _render_source_section(
    product: str, page: dict, section_id: str, resolved: dict
) -> str:
    projection = resolved.get("public_projection")
    if projection:
        # Evidence locators stay in the projection file's _source metadata.
        # The public page receives only the reader-facing interpretation.
        return _render_page_specific(product, page, section_id, projection)
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
    asset = (data.get("concept_film") or {}).get("asset") or {}
    poster_src = asset.get("poster_src") or ""
    poster = (
        f'<img class="concept-film-poster-media" src="{_esc(poster_src)}" alt="" aria-hidden="true">'
        if poster_src
        else ""
    )
    media = _concept_film(
        poster,
        data,
        "A persistent graph of records, relationships, provenance, and version history used by several agents",
    )
    category = str(data.get("category") or "").rstrip(".")
    headline = str(data["headline"]).rstrip(".")
    category_line = (
        f'<p class="eyebrow category">{_esc(data.get("category"))}</p>'
        if category.casefold() != headline.casefold()
        else ""
    )
    return f"""<section class="takeover-hero record-hero" id="hero"><div class="hero-copy">
{category_line}<h1>{_esc(data["headline"])}</h1>
<p class="lede">{_esc(data["body"])}</p>{_hero_ctas(page, data)}<p class="proof-line">{_esc(data.get("footnote"))}</p></div>{media}</section>"""


def _concept_film(poster: str, data: dict, label: str, overlay: str = "") -> str:
    """Wrap a local poster in an optional local film slot."""
    brief = data.get("concept_film") or {}
    asset = brief.get("asset") or {}
    enabled = asset.get("enabled_in_hero") is not False
    video_src = (asset.get("video_src") or "") if enabled else ""
    poster_src = (asset.get("poster_src") or "") if enabled else ""
    fallback_src = (asset.get("fallback_src") or "") if enabled else ""
    poster_attr = f' poster="{_esc(poster_src)}"' if poster_src else ""
    sources = ""
    if video_src:
        sources += f'<source src="{_esc(video_src)}" type="video/webm">'
    if fallback_src:
        sources += f'<source src="{_esc(fallback_src)}" type="video/mp4">'
    video = (
        f'<video class="concept-film-media" muted autoplay playsinline loop preload="metadata"{poster_attr} aria-hidden="true">{sources}</video>'
        if sources
        else ""
    )
    duration = brief.get("duration_seconds", "")
    active = "true" if video else "false"
    overlay_html = (
        f'<div class="concept-film-overlay">{overlay}</div>' if overlay else ""
    )
    return f'<figure class="concept-film" aria-label="{_esc(label)}" data-concept-film-ready="true" data-concept-film-active="{active}" data-duration-seconds="{_esc(duration)}"><div class="concept-film-poster">{poster}</div>{video}{overlay_html}</figure>'


def _authorization_seal_svg(state: str) -> str:
    mark = {
        "GRANTED": "M7 12.5l3 3 7-8",
        "CHECKPOINT": "M12 7v5l3 2",
        "WITHHELD": "M7 12h10",
    }.get(state, "M8 12h8")
    return f'''<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="{mark}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'''


def _swarm_svg(roles: list[str]) -> str:
    defaults = ["Operator", "Research", "Build", "Review", "Operate"]
    labels = (roles + defaults)[:5]
    return f"""<svg class="swarm-field" viewBox="0 0 1440 820" role="img" aria-labelledby="swarm-title swarm-desc" preserveAspectRatio="xMidYMid slice">
<title id="swarm-title">Distinct roles coordinating as a swarm</title><desc id="swarm-desc">Relational, autonomous members move around shared purpose, exchange work through brief signals, pause at a bounded checkpoint or quorum, and continue without permanent connecting edges or a central hub.</desc>
<ellipse class="purpose-field" cx="1010" cy="420" rx="350" ry="260"/><text class="purpose-label" x="910" y="110">SHARED PURPOSE</text>
<g aria-hidden="true"><path class="handoff-signal" d="M820 240C900 280 930 320 962 360"/><path class="handoff-signal" d="M1075 272C1140 320 1160 362 1134 420"/><path class="handoff-signal" d="M1170 530C1090 565 1030 594 948 610"/><path class="handoff-signal" d="M870 590C790 550 760 505 790 458"/></g>
<g class="swarm-member" transform="translate(770 190)"><circle r="56"/><text y="4">{_esc(labels[0]).upper()}</text></g>
<g class="swarm-member" transform="translate(1010 225)"><rect x="-62" y="-44" width="124" height="88" rx="18"/><text y="4">{_esc(labels[1]).upper()}</text></g>
<g class="swarm-member" transform="translate(1210 390)"><path d="M0-58 55 38-55 38Z"/><text y="12">{_esc(labels[2]).upper()}</text></g>
<g class="swarm-member" transform="translate(1110 620)"><rect x="-50" y="-50" width="100" height="100" rx="50"/><text y="4">{_esc(labels[3]).upper()}</text></g>
<g class="swarm-member" transform="translate(820 615)"><path d="M-55 0-27-48h54L55 0 27 48h-54Z"/><text y="4">{_esc(labels[4]).upper()}</text></g>
<g transform="translate(940 420)"><rect class="visual-checkpoint" x="-84" y="-34" width="168" height="68" rx="34"/><text class="purpose-label" x="-55" y="5">CHECKPOINT</text></g>
</svg>"""


def _network_hero(page: dict, data: dict) -> str:
    state_labels = {
        "GRANTED": "Approved",
        "CHECKPOINT": "Needs review",
        "WITHHELD": "Not authorized",
    }
    grants = "".join(
        f'<div class="grant-row" data-state="{_esc(item["state"])}"><span>{_esc(item["label"])}</span><span class="state-mark"><span class="authorization-seal">{_authorization_seal_svg(item["state"])}</span><span class="grant-state">{_esc(state_labels.get(item["state"], item["state"]))}</span></span></div>'
        for item in data.get("grants", [])
    )
    poster = f"""<aside class="hero-takeover-visual organization-board" aria-label="A swarm coordinating through temporary handoffs and bounded authority">{_swarm_svg(data.get("roles", []))}<div class="coordination-states">{grants}</div></aside>"""
    media = _concept_film(
        poster, data, "A swarm becoming an organization through temporary handoffs"
    )
    category = str(data.get("category") or "").rstrip(".")
    headline = str(data["headline"]).rstrip(".")
    category_line = (
        f'<p class="eyebrow category">{_esc(data.get("category"))}</p>'
        if category.casefold() != headline.casefold()
        else ""
    )
    return f"""<section class="takeover-hero network-hero" id="hero"><div class="hero-copy">
{category_line}<h1>{_esc(data["headline"])}</h1><p class="lede">{_esc(data["body"])}</p>
{_hero_ctas(page, data)}<p class="proof-line">{_esc(data.get("footnote"))}</p></div>{media}</section>"""


def _section_visual(product: str, section_id: str, layout: str) -> str:
    """A claim-specific visual that reads before its adjacent copy."""
    if product == "ateles":
        labels = {
            "failures": ("SCATTERED", "WAITING", "MANUAL ROUTING"),
            "mechanism": ("ROLE", "HANDOFF", "ROLE"),
            "priorities": ("MISSION", "PRIORITY", "TASK"),
            "planning": ("MISSION", "PLAN", "TASK"),
            "charter": ("INITIATE", "REVIEW", "CONTINUE"),
            "comparison": ("ONE RUN", "STANDING ROLES", "ACCOUNTABLE"),
            "vision": ("NOW", "NEXT", "ORGANIZATION"),
            "roadmap": ("RUNNING", "BOUNDED", "EXTENDING"),
            "fit": ("RESEARCH", "BUILD", "REVIEW"),
        }.get(section_id, ("ROLE", "SIGNAL", "CONTINUE"))
        checkpoint = (
            ""
            if section_id in {"failures", "fit"}
            else '<rect class="visual-checkpoint" x="644" y="173" width="150" height="64" rx="32"/><text x="676" y="211">CHECKPOINT</text>'
        )
        return f"""<figure class="section-visual ateles-visual" aria-label="Autonomous roles coordinate through proximity and temporary handoffs for {_esc(section_id.replace("-", " "))}">
<svg viewBox="0 0 1000 520" role="img"><title>{_esc(labels[0])}, {_esc(labels[1])}, then {_esc(labels[2])}</title><desc>Distinct roles coordinate through brief signals. No permanent network or central hub is shown.</desc>
<ellipse class="purpose-field" cx="510" cy="265" rx="390" ry="185"/><text class="visual-caption" x="430" y="88">SHARED PURPOSE</text>
<g aria-hidden="true"><path class="visual-signal" d="M268 200C340 154 410 158 458 220"/><path class="visual-signal" d="M556 224C625 168 694 174 746 230"/><path class="visual-signal" d="M740 328C658 374 580 380 518 332"/></g>
<circle class="visual-swarm-member" cx="230" cy="225" r="54"/><rect class="visual-swarm-member" x="438" y="198" width="112" height="82" rx="20"/><path class="visual-swarm-member" d="M792 194 852 292H732Z"/><path class="visual-swarm-member" d="M430 366 474 324h62l44 42-44 42h-62Z"/>
<text x="185" y="230">{_esc(labels[0])}</text><text x="457" y="246">{_esc(labels[1])}</text><text x="753" y="255">{_esc(labels[2])}</text><text x="460" y="373">CONTINUE</text>{checkpoint}
</svg></figure>"""
    labels = {
        "failures": ("WRITE", "CORRECT", "RECONCILE"),
        "mechanism": ("CREATE", "UPDATE", "RETRIEVE"),
        "entry-point": ("SOURCE", "CURRENT", "HISTORY"),
        "start": ("CONNECT", "CORRECT", "REREAD"),
        "profile": ("SOURCE", "DECISION", "CURRENT"),
        "comparison": ("RECALL", "PROVENANCE", "CURRENT"),
        "fit": ("AGENT A", "RECORD", "AGENT B"),
    }.get(section_id, ("CREATE", "VERSION", "RETRIEVE"))
    return f"""<figure class="section-visual neotoma-visual" aria-label="A persistent record graph explaining {_esc(section_id.replace("-", " "))}">
<svg viewBox="0 0 1000 520" role="img"><title>{_esc(labels[0])}, {_esc(labels[1])}, and {_esc(labels[2])} remain connected</title><desc>Records, relationships, source, and version lineage stay durable while agents write and read the current state.</desc>
<g aria-hidden="true"><path class="visual-edge" d="M226 260C340 148 420 154 500 250M500 250C614 146 716 164 794 260M500 250C530 336 586 376 670 390M226 260C320 350 440 380 670 390"/><path class="visual-pulse" d="M226 260C340 148 420 154 500 250M500 250C614 146 716 164 794 260"/></g>
<rect class="visual-record-muted" x="150" y="207" width="152" height="106" rx="16"/><rect class="visual-record" x="414" y="191" width="172" height="118" rx="16"/><rect class="visual-record-muted" x="718" y="207" width="152" height="106" rx="16"/><rect class="visual-version" x="590" y="348" width="160" height="84" rx="14"/>
<text x="191" y="265">{_esc(labels[0])}</text><text x="452" y="255">{_esc(labels[1])}</text><text x="748" y="265">{_esc(labels[2])}</text><text class="visual-caption" x="625" y="398">PRIOR VERSION</text><circle class="visual-agent" cx="500" cy="112" r="32"/><text class="visual-agent-label" x="500" y="116">AGENT</text>
</svg></figure>"""


def _deep_link(product: str, section_id: str, layout: str) -> str:
    if product not in {"neotoma", "ateles"}:
        return ""
    if product == "neotoma":
        href, label = {
            "failures": ("/evaluate/", "See where continuity breaks"),
            "mechanism": ("/install/", "Explore record lineage"),
            "fit": ("/evaluate/", "Evaluate the fit"),
            "comparison": ("/compare/", "Compare record approaches"),
            "start": ("/install/", "Follow the connection path"),
        }.get(section_id, ("/compare/", "See how the record differs"))
    else:
        href, label = {
            "failures": ("/design/", "Explore organizational design"),
            "mechanism": ("/design/", "See temporary handoffs"),
            "priorities": ("/design/", "Trace mission to task"),
            "planning": ("/design/", "Trace mission to task"),
            "charter": ("/design/", "Explore bounded authority"),
            "comparison": ("/compare/", "Compare ongoing coordination"),
            "roadmap": ("/status/", "See what runs today"),
            "vision": ("/status/", "See what runs today"),
            "fit": ("/status/", "Inspect the current reference"),
        }.get(section_id, ("/compare/", "See bounded escalation"))
    return f'<a class="section-link" href="{href}">{label} <span aria-hidden="true">→</span></a>'


def _section_intro(product: str, section_id: str, data: dict) -> str:
    return f"""{_section_visual(product, section_id, data.get("layout", section_id))}<div class="section-copy"><p class="eyebrow">{_esc(data.get("eyebrow"))}</p><h2>{_esc(data["headline"])}</h2><p class="lede">{_esc(data.get("lede") or data.get("body"))}</p>{_deep_link(product, section_id, data.get("layout", section_id))}</div>"""


def _focused_evidence(page: dict, content: str) -> str:
    """Keep landing pages concise; expose supporting detail on focused routes."""
    if page.get("slug") == "index" or not content:
        return ""
    return f'<div class="section-evidence">{content}</div>'


def _render_capabilities(page: dict, product: str, section_id: str, data: dict) -> str:
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
    evidence = _focused_evidence(
        page, f'<div class="capability-list">{items}</div>{correction}{status}'
    )
    return f'<section class="wrap visual-section" id="{_esc(section_id)}">{_section_intro(product, section_id, data)}{evidence}</section>'


def _render_hierarchy(page: dict, product: str, section_id: str, data: dict) -> str:
    chain = "".join(
        f'<div class="hierarchy-item">{_esc(item)}<span class="hierarchy-arrow">{"↓" if index < len(data["levels"]) - 1 else "act"}</span></div>'
        for index, item in enumerate(data["levels"])
    )
    proof_note = data.get("proof_note") or data.get("source_note")
    note = f'<p class="status-note">{_esc(proof_note)}</p>' if proof_note else ""
    evidence = _focused_evidence(
        page,
        f'<div class="hierarchy" aria-label="Planning hierarchy">{chain}</div>{note}',
    )
    return f'<section class="wrap visual-section" id="{_esc(section_id)}">{_section_intro(product, section_id, data)}{evidence}</section>'


def _render_steps(page: dict, product: str, section_id: str, data: dict) -> str:
    steps = "".join(
        f'<article class="step"><span class="step-num">{index:02d}</span><div><h3>{_esc(item["title"])}</h3><p>{_esc(item["body"])}</p></div></article>'
        for index, item in enumerate(data.get("items", []), start=1)
    )
    evidence = _focused_evidence(page, f'<div class="steps">{steps}</div>')
    return f'<section class="wrap visual-section" id="{_esc(section_id)}">{_section_intro(product, section_id, data)}{evidence}</section>'


def _render_cards(page: dict, product: str, section_id: str, data: dict) -> str:
    cards = "".join(
        '<article class="card">'
        + (
            f'<p class="card-label">{_esc(item["label"])}</p>'
            if item.get("label")
            else ""
        )
        + f"<h3>{_esc(item['title'])}</h3><p>{_esc(item['body'])}</p>"
        + (f'<p class="meta">{_esc(item["meta"])}</p>' if item.get("meta") else "")
        + (
            f'<a href="{_esc(item["href"])}">{_esc(item.get("link_label", "Read more"))}</a>'
            if item.get("href")
            else ""
        )
        + "</article>"
        for item in data.get("items", [])
    )
    evidence = _focused_evidence(page, f'<div class="cards">{cards}</div>')
    return f'<section class="wrap visual-section" id="{_esc(section_id)}">{_section_intro(product, section_id, data)}{evidence}</section>'


def _render_flow(page: dict, product: str, section_id: str, data: dict) -> str:
    items = "".join(
        f'<article class="flow-step"><p class="flow-label">{_esc(item.get("label"))}</p><h3>{_esc(item["title"])}</h3><p>{_esc(item["body"])}</p></article>'
        for item in data.get("items", [])
    )
    evidence = _focused_evidence(page, f'<div class="public-flow">{items}</div>')
    return f'<section class="wrap visual-section" id="{_esc(section_id)}">{_section_intro(product, section_id, data)}{evidence}</section>'


def _render_comparison(page: dict, product: str, section_id: str, data: dict) -> str:
    head = "".join(f"<div>{_esc(value)}</div>" for value in data.get("columns", []))
    rows = "".join(
        '<div class="comparison-row comparison-body">'
        + "".join(f"<div>{_esc(value)}</div>" for value in row)
        + "</div>"
        for row in data.get("rows", [])
    )
    evidence = _focused_evidence(
        page,
        f'<div class="comparison-wrap"><div class="comparison-grid" role="table" aria-label="{_esc(data["headline"])}"><div class="comparison-row comparison-head" role="row">{head}</div>{rows}</div></div>',
    )
    return f'<section class="wrap visual-section" id="{_esc(section_id)}">{_section_intro(product, section_id, data)}{evidence}</section>'


def _status_badge(status: str) -> str:
    normalized = str(status or "missing").casefold()
    return f'<span class="brand-status brand-status-{_esc(normalized)}">{_esc(normalized)}</span>'


def _public_anti_pattern(value: str) -> str:
    """Translate internal enforcement language into reader-facing guidance."""
    lower = value.casefold()
    if any(
        term in lower
        for term in (
            "record ids",
            "entity types",
            "field or decision keys",
            "repository filenames",
            "phase bookkeeping",
            "agent instructions",
        )
    ):
        return "Keep internal implementation vocabulary and operational bookkeeping off public surfaces."
    if any(
        term in lower
        for term in (
            "research analysis",
            "source citations",
            "implementation-review notes",
        )
    ):
        return "Use concise reader-facing claims; keep working notes and evidence in their source records."
    return value


def _public_downstream_contract(item: dict) -> dict:
    """Project implementation paths into a stable reader-facing contract."""
    consumer = item.get("consumer") or "Downstream consumer"
    replacements = {
        "Human repository guide": "Generated from the canonical record; never hand-edit.",
        "Machine site contract": "Generated and validated against the versioned schema.",
        "Design tokens": "Palette and typography derive from the canonical visual system.",
        "Product site": "The brand route is a read-only, public-safe viewer.",
        "Cinematic production": "Production briefs inherit these shared and product-specific rules.",
        "Application marks": "Application identity must pass the product symbol and anti-pattern contract.",
    }
    return {
        **item,
        "contract": replacements.get(
            consumer, "Derived from the canonical brand system."
        ),
    }


def _public_gate_text(value: object) -> str:
    """Translate canonical bookkeeping language for a public review surface."""
    public = str(value or "").replace(
        "category_definition", "approved category definition"
    )
    return re.sub(
        r"\s+(?:in|from)\s+ent_[0-9a-f]{12,}", " in the canonical record", public
    )


def _brand_items(items: list[dict], *, include_source: bool = False) -> str:
    visible = [item for item in items if item.get("status") != "retired"]
    return "".join(
        '<article class="brand-item">'
        f"{_status_badge(item.get('status'))}"
        f"<h3>{_esc(str(item.get('name') or item.get('consumer') or '').replace('_', ' '))}</h3>"
        f"<p>{_esc(item.get('guidance') or item.get('contract') or item.get('use'))}</p>"
        + (
            f'<p class="small muted">Source: {_esc(item.get("source"))}</p>'
            if include_source
            and item.get("source")
            and not str(item.get("source")).startswith("ent_")
            else ""
        )
        + "</article>"
        for item in visible
    )


def _brand_asset_preview(item: dict) -> str:
    public_path = item.get("public_path")
    preview = item.get("preview")
    if not public_path or item.get("status") == "missing":
        return ""
    if preview == "video":
        return f'<div class="brand-asset-preview"><video controls muted playsinline preload="metadata" aria-label="{_esc(item.get("name"))}"><source src="{_esc(public_path)}"></video></div>'
    if preview == "image":
        return f'<div class="brand-asset-preview"><img src="{_esc(public_path)}" loading="lazy" alt="{_esc(item.get("name"))}"></div>'
    return ""


def _render_brand_system(product: str, data: dict) -> str:
    positioning = data["positioning"]
    intent = positioning["intent"]
    voice = data["voice"]
    styles = data["visual_styles"]
    logo = styles["logo_system"]
    typography = styles["typography_system"]
    production = data["production_specs"]
    generation_gate = production["generation_gate"]
    provenance = data["provenance"]
    completeness = data["completeness"]
    phrases = _brand_items(data.get("phrases") or [])
    terms = _brand_items(data.get("terminology") or [])
    concepts = _brand_items(data.get("visual_concepts") or [])
    assets = "".join(
        '<article class="brand-item">'
        + _brand_asset_preview(item)
        + _status_badge(item.get("status"))
        + f"<h3>{_esc(item.get('name'))}</h3><p>{_esc(item.get('use'))}</p>"
        + (
            f'<a href="{_esc(item["public_path"])}">Open asset</a>'
            if item.get("public_path") and item.get("status") != "missing"
            else ""
        )
        + "</article>"
        for item in data.get("asset_inventory") or []
    )
    anti_patterns = "".join(
        f"<li>{_esc(_public_anti_pattern(value))}</li>"
        for value in dict.fromkeys(styles.get("anti_patterns") or [])
    )
    rules = "".join(f"<li>{_esc(value)}</li>" for value in voice.get("rules") or [])
    palette = styles.get("palette", {}).get("light", {})
    swatches = "".join(
        f'<div class="brand-swatch" style="--swatch:{_esc(value)};--swatch-label:{"#fff" if key in {"ink", "ink_2", "accent"} else "#111"}">{_esc(key.replace("_", " "))}<br>{_esc(value)}</div>'
        for key, value in palette.items()
        if isinstance(value, str) and value.startswith("#")
    )
    source_labels = "".join(
        f"<span>{_esc(source.get('label'))} · {_esc(source.get('status'))}</span>"
        for source in provenance.get("sources") or []
    )
    intent_cards = "".join(
        '<article class="brand-item">'
        f"<h3>{_esc(label)}</h3><p>{_esc(value)}</p></article>"
        for label, value in (
            ("Core idea", intent["core_idea"]),
            ("Functional truth", intent["functional_truth"]),
            ("Emotional outcome", intent["emotional_outcome"]),
            ("Sibling distinction", intent["sibling_distinction"]),
        )
    )
    intent_proof = "".join(
        f"<li>{_esc(value)}</li>" for value in intent.get("proof_cues") or []
    )
    intended = "".join(
        f"<li>{_esc(value)}</li>" for value in intent.get("intended_perceptions") or []
    )
    forbidden = "".join(
        f"<li>{_esc(value)}</li>" for value in intent.get("forbidden_perceptions") or []
    )
    logo_variants = "".join(
        '<article class="brand-item">'
        f"{_status_badge(item.get('status'))}"
        f"<h3>{_esc(key.replace('_', ' '))}</h3><p>{_esc(item.get('use'))}</p>"
        "</article>"
        for key, item in logo["variants"].items()
    )
    logo_rules = "".join(
        '<article class="brand-item">'
        f"{_status_badge(item.get('status'))}"
        f"<h3>{_esc(key.replace('_', ' '))}</h3><p>{_esc(item.get('guidance'))}</p>"
        "</article>"
        for key, item in logo.items()
        if key
        in {
            "clear_space",
            "minimum_size",
            "background_rules",
            "colorway_rules",
            "co_branding",
        }
    )
    type_roles = "".join(
        '<article class="brand-item">'
        f"{_status_badge(item.get('status'))}"
        f"<h3>{_esc(key)} · {_esc(item.get('name'))}</h3><p>{_esc(item.get('use'))}</p>"
        "</article>"
        for key, item in typography["roles"].items()
    )
    type_tokens = "".join(
        "<tr>"
        f"<td>{_esc(item.get('token'))}<br>{_status_badge(item.get('status'))}</td>"
        f"<td>{_esc(item.get('family'))}</td><td>{_esc(item.get('size'))}</td>"
        f"<td>{_esc(item.get('line_height'))}</td><td>{_esc(item.get('measure'))}</td>"
        f"<td>{_esc(item.get('casing'))}</td></tr>"
        for item in typography["hierarchy"]
    )
    access_cards = "".join(
        '<article class="brand-item">'
        f"{_status_badge(item.get('status'))}"
        f"<h3>{_esc(key.replace('_', ' '))}</h3><p>{_esc(item.get('requirement'))}</p>"
        "</article>"
        for key, item in production["accessibility"].items()
    )
    gate_cards = "".join(
        '<article class="brand-item">'
        f"{_status_badge(item.get('status'))}"
        f"<h3>{_esc(item.get('name', '').replace('_', ' '))}</h3><p>{_esc(_public_gate_text(item.get('evidence')))}</p>"
        "</article>"
        for item in generation_gate["predicates"]
    )
    research_links = "".join(
        '<article class="brand-item">'
        f"<h3>{_esc(item.get('label'))}</h3><p>{_esc(item.get('informs'))}</p>"
        f'<a href="{_esc(item.get("url"))}">Review official guidance</a>'
        f'<p class="small muted">Checked {_esc(item.get("checked_at"))}</p></article>'
        for item in provenance["research"]["sources"]
    )
    public_references = [
        item
        for item in provenance["market_reference_ledger"]
        if item.get("visibility") == "public_safe"
        and item.get("support") != "gap"
        and item.get("evidence", {}).get("observed_at")
    ]
    market_rows = "".join(
        "<tr>"
        f'<td>{_esc(item["referenced_product"])}<br><span class="small muted">{_esc(item["relationship"].replace("_", " "))} · {_esc(item["brand_rule_class"].replace("_", " "))}</span></td>'
        + (
            f'<td><a href="{_esc(item["evidence"]["source"])}">Dated evidence</a><br>{_esc(item["evidence"]["observed_at"])}</td>'
            if str(item["evidence"]["source"]).startswith("https://")
            else f"<td>Dated synthesis<br>{_esc(item['evidence']['observed_at'])}</td>"
        )
        + f"<td><strong>Observed:</strong> {_esc(item['observed_fact'])}<br><strong>Inference:</strong> {_esc(item['derived_learning'].removeprefix('Inference: '))}</td>"
        f"<td>{_esc('; '.join(item['best_practices_to_adopt']))}</td>"
        f"<td>{_esc('; '.join(item['bad_practices_to_avoid']))}</td>"
        f"<td>{_esc(item['differentiation_implication'])}</td></tr>"
        for item in public_references
    )
    matrix_rows = "".join(
        "<tr>"
        f"<td>{_esc(item['axis'].replace('_', ' '))}</td>"
        f'<td>{_esc(item["ateles"])}<br><span class="small muted">{_esc(item["ateles_brand_rule_class"].replace("_", " "))}</span></td>'
        f'<td>{_esc(item["neotoma"])}<br><span class="small muted">{_esc(item["neotoma_brand_rule_class"].replace("_", " "))}</span></td>'
        f"<td>{_esc(item['convergence_test'])}</td></tr>"
        for item in provenance["differentiation_matrix"]["axes"]
    )
    territory = styles["aesthetic_territory"]
    rejected_territories = "".join(
        f"<li><strong>{_esc(item['name'])}</strong> — {_esc(item['reason'])}</li>"
        for item in territory["rejected_alternatives"]
    )
    convergence_tests = "".join(
        f"<li><strong>{_esc(item['reference'])}</strong> — {_esc(item['test'])}</li>"
        for item in territory["convergence_tests"]
    )
    dimensions = _brand_items(completeness.get("dimensions") or [])
    missing = "".join(
        f"<li>{_esc(item)}</li>" for item in completeness.get("missing_items") or []
    )
    contracts = _brand_items(
        [
            _public_downstream_contract(item)
            for item in data.get("downstream_contracts") or []
        ]
    )
    return f"""<section class="wrap brand-system-page" id="brand-system">
<div class="brand-system-intro"><div><p class="eyebrow">Brand system · {_esc(data.get("schema_version"))}</p><h1>{_esc(positioning["category"])}</h1><p class="lede">{_esc(positioning["hero_support"])}</p></div><aside class="brand-summary">{_status_badge(completeness.get("overall_status"))}<h2>One source, visible state.</h2><p>This page is a public-safe viewer of the canonical brand system. Approved, provisional, and missing elements remain distinct.</p></aside></div>
<aside class="brand-item brand-gate-blocked brand-regeneration-notice"><span class="brand-status brand-status-provisional">Provisional review</span><h2>Not an approved brand baseline.</h2><p>The category and product argument are settled; current competitive and aesthetic evidence has produced one original aesthetic territory for review. No page or film should treat this guidance as final until the complete brand system is approved.</p><p>The next gate is operator approval or revision of this provisional brand system.</p></aside>
<figure class="section-visual brand-foundation-map" aria-label="The canonical brand system flows from expression rules to approved assets and downstream use"><div class="brand-foundation-step"><span class="eyebrow">01 · Define</span><strong>Words and visual meaning</strong></div><div class="brand-foundation-step"><span class="eyebrow">02 · Produce</span><strong>Assets with explicit state</strong></div><div class="brand-foundation-step"><span class="eyebrow">03 · Derive</span><strong>Consistent public surfaces</strong></div></figure>
<div class="brand-group"><header><h2>Brand intent</h2><p>The idea, truth, feeling, and evidence every expression must preserve.</p></header><div class="brand-grid">{intent_cards}</div><div class="brand-grid"><article class="brand-item"><h3>Intended perceptions</h3><ul>{intended}</ul></article><article class="brand-item"><h3>Forbidden perceptions</h3><ul>{forbidden}</ul></article></div><h3>Proof cues</h3><ul class="brand-rule-list brand-intent-proof">{intent_proof}</ul></div>
<div class="brand-group"><header><h2>Voice and language</h2><p>{_esc(positioning["product_promise"])}</p></header><div class="brand-grid">{phrases}{terms}</div><h3>Writing rules</h3><ul class="brand-rule-list">{rules}</ul></div>
<div class="brand-group"><header><h2>Recommended aesthetic territory</h2><p>This is an internal creative platform subordinate to the settled category—not an alternate category noun, public headline, or tagline. Alternatives and convergence risks remain explicit.</p></header><article class="brand-item">{_status_badge(territory["status"])}<h3>{_esc(territory["name"])}</h3><p class="lede">{_esc(territory["summary"])}</p><p><strong>Scope:</strong> {_esc(territory["scope_statement"])}</p><p><strong>Product truth:</strong> {_esc(territory["product_truth"])}</p><p><strong>Identity:</strong> {_esc(territory["identity"])}</p><p><strong>Palette, material, and light:</strong> {_esc(territory["palette_material_light"])}</p><p><strong>Typography and layout:</strong> {_esc(territory["typography_layout"])}</p><p><strong>Camera and motion:</strong> {_esc(territory["camera_motion"])}</p><p><strong>Originality basis:</strong> {_esc(territory["originality_basis"])}</p></article><div class="brand-grid"><article class="brand-item"><h3>Rejected aesthetic alternatives</h3><ul>{rejected_territories}</ul></article><article class="brand-item"><h3>Aesthetic convergence tests</h3><ul>{convergence_tests}</ul></article></div></div>
<div class="brand-group"><header><h2>Visual system</h2><p>{_esc(styles["symbol"]["guidance"])}</p></header><div class="brand-palette">{swatches}</div><div class="brand-grid">{concepts}</div><p class="lede"><strong>Material:</strong> {_esc(styles["material"])} <strong>Light:</strong> {_esc(styles["light"])} <strong>Camera:</strong> {_esc(styles["camera"])} <strong>Motion:</strong> {_esc(styles["motion"])}</p></div>
<div class="brand-group"><header><h2>Logo system</h2><p>Every required variant and application rule remains visibly approved, provisional, or missing. Unknown measurements are not invented.</p></header><div class="brand-grid">{logo_variants}</div><h3>Application rules</h3><div class="brand-grid">{logo_rules}</div></div>
<div class="brand-group"><header><h2>Typography system</h2><p>Expressive, productive, and technical type have different jobs inside one responsive hierarchy.</p></header><div class="brand-grid">{type_roles}</div><div class="table-scroll"><table class="brand-table"><thead><tr><th>Token</th><th>Role</th><th>Size</th><th>Line height</th><th>Measure</th><th>Casing</th></tr></thead><tbody>{type_tokens}</tbody></table></div></div>
<div class="brand-group"><header><h2>Assets</h2><p>Every asset carries an explicit acceptance state and intended use.</p></header><div class="brand-grid">{assets}</div></div>
<div class="brand-group"><header><h2>Cinematic grammar</h2><p>{_esc(production["cinematic"]["semantics"])}</p></header><div class="brand-grid"><article class="brand-item"><h3>Delivery</h3><p>{_esc(production["delivery"]["hero"])}</p><p>{_esc(production["delivery"]["responsive"])}</p></article><article class="brand-item"><h3>Still and reduced motion</h3><p>{_esc(production["still_and_reduced_motion"]["requirement"])}</p></article></div></div>
<div class="brand-group"><header><h2>Accessibility</h2><p>Brand expression must remain perceivable, semantic, scalable, and complete without motion.</p></header><div class="brand-grid">{access_cards}</div></div>
<div class="brand-group"><header><h2>Cinematic generation gate</h2><p>{_esc(generation_gate["rule"])}</p></header><article class="brand-item brand-gate-blocked">{_status_badge(generation_gate["status"])}<h3>Generation allowed: {_esc(str(generation_gate["generation_allowed"]).lower())}</h3><p>Every predicate must be approved and read back before production resumes.</p></article><div class="brand-grid">{gate_cards}</div></div>
<div class="brand-group"><header><h2>Do not drift here</h2><p>These constraints preserve the product's meaning as the system evolves.</p></header><ul class="brand-rule-list">{anti_patterns}</ul></div>
<div class="brand-group"><header><h2>Research and review</h2><p>{_esc(provenance["research"]["review_cadence"]["cadence"])} Principles are translated into this product's own expression, never copied as a recognizable aesthetic.</p><p><strong>Sampling rationale:</strong> {_esc(provenance["regeneration_chain"]["sampling_rationale"])}</p></header><div class="brand-grid">{research_links}</div></div>
<div class="brand-group"><header><h2>Market-reference learning ledger</h2><p>Evidence, observed fact, inference, adopted principle, rejected pattern, and distinctiveness remain separate.</p></header><div class="table-scroll"><table class="brand-table"><thead><tr><th>Reference</th><th>Evidence</th><th>Observation → inference</th><th>Adopt</th><th>Avoid</th><th>Differentiate</th></tr></thead><tbody>{market_rows}</tbody></table></div></div>
<div class="brand-group"><header><h2>Cross-product differentiation</h2><p>{_esc(provenance["differentiation_matrix"]["principle"])}</p></header><div class="table-scroll"><table class="brand-table"><thead><tr><th>Axis</th><th>Ateles</th><th>Neotoma</th><th>Convergence test</th></tr></thead><tbody>{matrix_rows}</tbody></table></div></div>
<div class="brand-group"><header><h2>Completeness and provenance</h2><p>Expression is canonical in the product brand record; capabilities and foundation truth remain in their own sources.</p></header><div class="brand-grid">{dimensions}</div><h3>Still missing</h3><ul>{missing}</ul><h3>Source families</h3><div class="brand-source-list">{source_labels}</div></div>
<div class="brand-group"><header><h2>Downstream contracts</h2><p>Each consumer derives from this system instead of becoming a parallel source of truth.</p></header><div class="brand-grid">{contracts}</div></div>
</section>"""


def _render_page_specific(product: str, page: dict, section_id: str, data: dict) -> str:
    layout = data.get("layout", section_id)
    if layout == "hero":
        return f'<section class="wrap hero" id="hero"><div class="hero-copy"><h1>{_esc(data["headline"])}</h1><p class="lede">{_esc(data.get("subheadline"))}</p><p>{_esc(data.get("body"))}</p>{_hero_ctas(page, data)}</div></section>'
    if layout == "record_hero":
        return _record_hero(page, data)
    if layout == "network_hero":
        return _network_hero(page, data)
    if layout == "capabilities":
        return _render_capabilities(page, product, section_id, data)
    if layout == "hierarchy":
        return _render_hierarchy(page, product, section_id, data)
    if layout == "steps":
        return _render_steps(page, product, section_id, data)
    if layout in ("cards", "proof_cards", "comparison_teaser", "sibling"):
        return _render_cards(page, product, section_id, data)
    if layout == "flow":
        return _render_flow(page, product, section_id, data)
    if layout == "comparison":
        return _render_comparison(page, product, section_id, data)
    if layout == "cta_banner":
        return f'<section class="wrap visual-section" id="{_esc(section_id)}">{_section_visual(product, section_id, layout)}<div class="panel"><p class="eyebrow">{_esc(data.get("eyebrow"))}</p><h2>{_esc(data["headline"])}</h2><p class="lede" style="margin-top:18px">{_esc(data.get("body"))}</p>{_hero_ctas(page, data)}</div></section>'
    import json as _json

    return f'<section class="wrap" id="{_esc(section_id)}"><div class="blocker">No renderer for page-specific layout {_esc(layout)}</div><pre>{_esc(_json.dumps(data, indent=2))}</pre></section>'


def _render_section(product: str, page: dict, section_id: str, resolved: dict) -> str:
    if not resolved["_resolved"]:
        return f'<section class="wrap" id="{_esc(section_id)}"><div class="blocker">BLOCKED — {_esc(resolved["_blocker"])}</div></section>'
    if "data" in resolved:
        if resolved.get("origin") == "brand_system":
            return _render_brand_system(product, resolved["data"])
        return _render_page_specific(product, page, section_id, resolved["data"])
    if resolved["origin"] in ("authored", "positioning_mirror"):
        return _render_source_section(product, page, section_id, resolved)
    return f'<section class="wrap" id="{_esc(section_id)}"><div class="blocker">Unknown content origin.</div></section>'


def _font_link(tokens: dict) -> str:
    """Load the three token-declared families without product-name branches."""
    type_scale = tokens.get("product", {}).get("type_scale", {})
    families = []
    for field, weights in (
        ("heading_font_family", "500;600;700"),
        ("body_font_family", "400;500;600"),
        ("code_font_family", "400;600"),
    ):
        stack = type_scale.get(field, "")
        primary = stack.split(",", 1)[0].strip().strip("'\"")
        if primary and primary not in {family for family, _ in families}:
            families.append((primary, weights))
    if not families:
        return ""
    query = "&family=".join(
        f"{quote_plus(family)}:wght@{weights}" for family, weights in families
    )
    return f'<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family={query}&display=swap" rel="stylesheet">'


def _brand_mark(product: str) -> str:
    if product != "ateles":
        return ""
    return """<svg class="swarm-mark" viewBox="0 0 28 28" role="img" aria-label="Ateles swarm mark"><circle cx="7" cy="8" r="2.4"/><rect class="swarm-core" x="12" y="4" width="5" height="5" rx="1"/><path d="M22 9l2.8 4.8h-5.6Z"/><circle cx="19" cy="20" r="2.4"/><rect x="8" y="18" width="5" height="5" rx="2.5"/><path d="M4 14h4l2 3.4-2 3.5H4l-2-3.5Z"/></svg>"""


def render_page(
    product: str,
    page: dict,
    site_pages: list[dict],
    sections: list[tuple[str, dict]],
    tokens: dict,
) -> str:
    css = build_css(tokens, product)
    rendered_sections = [
        _render_section(product, page, section_id, resolved)
        for section_id, resolved in sections
    ]
    if rendered_sections and "<h1" not in rendered_sections[0]:
        rendered_sections[0] = re.sub(
            r"<h2>(.*?)</h2>",
            r'<h1 class="page-title">\1</h1>',
            rendered_sections[0],
            count=1,
            flags=re.DOTALL,
        )
    body_sections = '\n<hr class="rule">\n'.join(rendered_sections)
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
        else "The operating system for agentic organizations."
    )
    identity = "the record" if product == "neotoma" else "the swarm"
    brand_mark = _brand_mark(product)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(page.get("title", product.title()))}</title><meta name="description" content="{_esc(page.get("meta_description", ""))}">
{_font_link(tokens)}<style>{css}</style></head><body class="product-{_esc(product)}">
<header class="nav" id="site-nav"><div class="nav-in"><a class="brand" href="/">{brand_mark}{_esc(product.title())}<small>{identity}</small></a><nav class="nav-links" aria-label="Primary">{site_nav}</nav>{nav_cta}{theme_toggle}</div></header>
<main>{body_sections}</main><footer><div class="wrap footer-in"><p class="footer-noun">{noun}</p><div class="flinks">{site_nav}</div></div></footer>
<script>const nav=document.getElementById('site-nav');const syncNav=()=>nav.classList.toggle('scrolled',scrollY>12);syncNav();addEventListener('scroll',syncNav,{{passive:true}});</script></body></html>
"""
