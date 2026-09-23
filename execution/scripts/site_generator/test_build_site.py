"""
Tests for build_site.py — the static site generator that turns a page
inventory into HTML by reading only repo files (no Neotoma calls).

Covers the three content-origin paths (page_specific, authored,
positioning_mirror) and the honest-failure behavior when a source is
missing: this generator must FAIL LOUDLY (non-zero exit, a reported
blocker) rather than silently omit a section or fabricate content — that is
the property the whole task exists to guarantee, so it is the thing these
tests check most directly.

Run with: pytest execution/scripts/site_generator/test_build_site.py -v
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import json
import re
import shutil
import sys
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

_GEN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_GEN_DIR))

import build_site  # noqa: E402
import preview_server  # noqa: E402
from templates import minimal_markdown as mdlib  # noqa: E402
from templates import render as tpl  # noqa: E402


FIXTURE_TOKENS = {
    "shared": {
        "scope": "test scope",
        "spacing_system": {"container": ".wrap { --maxw: 1080px; }"},
        "border_radius": {"root_radius_variable": "--radius: 10px"},
        "positioning_principles": ["one system, two themes"],
        "anti_patterns": ["no sharp corners"],
    },
    "product": {
        "color_palette": {
            "light": {"ink": "#111", "paper": "#fff", "accent": "#900"},
            "dark": {"ink": "#eee", "paper": "#111", "accent": "#f90"},
        },
        "type_scale": {
            "heading_font_family": "Test Sans, sans-serif",
            "body_font_family": "Test Serif, serif",
            "code_font_family": "Test Mono, monospace",
            "h1_size": "3rem",
            "h2_size": "2rem",
            "body_base_size": "16px",
            "heading_weight": 700,
        },
    },
}


@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    """Build a minimal fake repo tree so build_site.py's REPO_ROOT-relative
    reads resolve inside a throwaway directory instead of touching the real
    repo. Monkeypatches the module-level path constants directly since
    REPO_ROOT is computed once at import time from __file__."""
    repo_root = tmp_path / "repo"
    gen_dir = repo_root / "execution" / "scripts" / "site_generator"
    (gen_dir / "inventory").mkdir(parents=True)
    (gen_dir / "design_tokens").mkdir(parents=True)
    (gen_dir / "content" / "testproduct").mkdir(parents=True)
    (repo_root / "docs" / "positioning" / "testproduct").mkdir(parents=True)

    monkeypatch.setattr(build_site, "REPO_ROOT", repo_root)
    monkeypatch.setattr(build_site, "GEN_DIR", gen_dir)
    monkeypatch.setattr(build_site, "INVENTORY_DIR", gen_dir / "inventory")
    monkeypatch.setattr(build_site, "DESIGN_TOKENS_DIR", gen_dir / "design_tokens")
    monkeypatch.setattr(build_site, "CONTENT_DIR", gen_dir / "content")

    (gen_dir / "design_tokens" / "testproduct.json").write_text(
        json.dumps(FIXTURE_TOKENS)
    )

    return repo_root, gen_dir


def _write_inventory(gen_dir: Path, pages: list[dict]) -> None:
    inv = {
        "product": "testproduct",
        "design_tokens": "design_tokens/testproduct.json",
        "pages": pages,
    }
    (gen_dir / "inventory" / "testproduct.json").write_text(json.dumps(inv))


def _write_projection(
    gen_dir: Path,
    name: str,
    source: str,
    *,
    entity_id: str | None = None,
    headline: str = "Reader-facing headline",
    body: str = "Reader-facing proof.",
) -> str:
    rel = f"content/testproduct/public/{name}.json"
    path = gen_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    source_meta = {"path": source}
    if entity_id:
        source_meta["entity_id"] = entity_id
    path.write_text(
        json.dumps(
            {
                "_source": source_meta,
                "layout": "proof_cards",
                "headline": headline,
                "lede": body,
                "items": [],
            }
        )
    )
    return rel


def test_page_specific_section_resolves_and_renders(tmp_repo):
    repo_root, gen_dir = tmp_repo
    content_path = gen_dir / "content" / "testproduct" / "hero.json"
    content_path.write_text(
        json.dumps(
            {
                "headline": "Hello world",
                "subheadline": "A subtitle",
                "body": "Body copy.",
            }
        )
    )

    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "Test page",
                "sections": [
                    {
                        "id": "hero",
                        "origin": "page_specific",
                        "source": "content/testproduct/hero.json",
                    }
                ],
            }
        ],
    )

    blockers = build_site.build("testproduct", repo_root / "dist" / "site")
    assert blockers == []
    html = (repo_root / "dist" / "site" / "testproduct" / "index.html").read_text()
    assert "Hello world" in html
    assert "A subtitle" in html


def test_authored_json_composition_resolves_and_renders(tmp_repo):
    repo_root, gen_dir = tmp_repo
    authored = repo_root / "docs" / "sites" / "testproduct" / "hero.json"
    authored.parent.mkdir(parents=True)
    authored.write_text(
        json.dumps(
            {
                "layout": "record_hero",
                "headline": "Durable truth",
                "body": "A current record.",
                "proof": {"old": "Earlier", "current": "Current"},
            }
        )
    )
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {
                        "id": "hero",
                        "origin": "authored",
                        "source": "docs/sites/testproduct/hero.json",
                    }
                ],
            }
        ],
    )

    assert build_site.build("testproduct", repo_root / "dist" / "site") == []
    html = (repo_root / "dist" / "site" / "testproduct" / "index.html").read_text()
    assert "Durable truth" in html
    assert "A current record." in html


def test_page_specific_rejects_composition_keys(tmp_repo):
    repo_root, gen_dir = tmp_repo
    (gen_dir / "content" / "testproduct" / "hero.json").write_text(
        json.dumps({"headline": "T", "proof": {"current": "Current"}})
    )
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {
                        "id": "hero",
                        "origin": "page_specific",
                        "source": "content/testproduct/hero.json",
                    }
                ],
            }
        ],
    )

    blockers = build_site.build("testproduct", repo_root / "dist" / "site")
    assert any("holds composition, not a transition" in item for item in blockers)


def test_concept_film_brief_keeps_code_native_poster_until_asset_exists(tmp_repo):
    repo_root, gen_dir = tmp_repo
    brief = gen_dir / "content" / "testproduct" / "concept-film.json"
    brief.write_text(
        json.dumps(
            {
                "duration_seconds": 8,
                "playback": {
                    "muted": True,
                    "autoplay": True,
                    "playsinline": True,
                    "loop": True,
                    "reduced_motion": "static_poster",
                },
                "performance": {"max_bytes": 1_800_000},
                "asset": {"video_src": "", "poster_src": ""},
            }
        )
    )
    content_path = gen_dir / "content" / "testproduct" / "hero.json"
    content_path.write_text(
        json.dumps(
            {
                "layout": "record_hero",
                "headline": "Durable truth",
                "body": "A current record.",
                "concept_film_brief": "content/testproduct/concept-film.json",
            }
        )
    )
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {
                        "id": "hero",
                        "origin": "page_specific",
                        "source": "content/testproduct/hero.json",
                    }
                ],
            }
        ],
    )

    assert build_site.build("testproduct", repo_root / "dist" / "site") == []
    html = (repo_root / "dist" / "site" / "testproduct" / "index.html").read_text()
    assert 'data-concept-film-ready="true"' in html
    assert 'data-duration-seconds="8"' in html
    assert "concept-film-poster" in html
    assert "<video" not in html


def test_concept_film_local_assets_are_validated_rendered_and_copied(tmp_repo):
    repo_root, gen_dir = tmp_repo
    asset_dir = gen_dir / "assets" / "testproduct"
    asset_dir.mkdir(parents=True)
    (asset_dir / "hero.webm").write_bytes(b"webm")
    (asset_dir / "hero.mp4").write_bytes(b"mp4")
    (asset_dir / "hero.avif").write_bytes(b"avif")
    brief = gen_dir / "content" / "testproduct" / "concept-film.json"
    brief.write_text(
        json.dumps(
            {
                "duration_seconds": 8,
                "playback": {
                    "muted": True,
                    "autoplay": True,
                    "playsinline": True,
                    "loop": True,
                    "reduced_motion": "static_poster",
                },
                "performance": {"max_bytes": 1_800_000},
                "asset": {
                    "repository_video_path": "execution/scripts/site_generator/assets/testproduct/hero.webm",
                    "repository_fallback_path": "execution/scripts/site_generator/assets/testproduct/hero.mp4",
                    "repository_poster_path": "execution/scripts/site_generator/assets/testproduct/hero.avif",
                    "public_video_path": "/assets/testproduct/hero.webm",
                    "public_fallback_path": "/assets/testproduct/hero.mp4",
                    "public_poster_path": "/assets/testproduct/hero.avif",
                },
            }
        )
    )
    (gen_dir / "content" / "testproduct" / "hero.json").write_text(
        json.dumps(
            {
                "layout": "record_hero",
                "headline": "Durable truth",
                "body": "A current record.",
                "concept_film_brief": "content/testproduct/concept-film.json",
            }
        )
    )
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {
                        "id": "hero",
                        "origin": "page_specific",
                        "source": "content/testproduct/hero.json",
                    }
                ],
            }
        ],
    )

    out_dir = repo_root / "dist" / "site"
    assert build_site.build("testproduct", out_dir) == []
    html = (out_dir / "testproduct" / "index.html").read_text()
    assert '<video class="concept-film-media" muted autoplay playsinline loop' in html
    assert 'poster="/assets/testproduct/hero.avif"' in html
    assert 'src="/assets/testproduct/hero.webm" type="video/webm"' in html
    assert 'src="/assets/testproduct/hero.mp4" type="video/mp4"' in html
    assert (
        out_dir / "testproduct" / "assets/testproduct/hero.webm"
    ).read_bytes() == b"webm"
    assert build_site.check("testproduct", out_dir) == 0


def test_disabled_concept_film_never_emits_hero_media():
    html = tpl._concept_film(
        '<div class="semantic-poster">Poster</div>',
        {
            "concept_film": {
                "duration_seconds": 8,
                "asset": {
                    "enabled_in_hero": False,
                    "video_src": "/assets/testproduct/hero.webm",
                    "fallback_src": "/assets/testproduct/hero.mp4",
                    "poster_src": "/assets/testproduct/hero.avif",
                },
            }
        },
        "Static semantic poster",
    )

    assert 'data-concept-film-active="false"' in html
    assert "<video" not in html
    assert "hero.webm" not in html
    assert "hero.mp4" not in html
    assert "hero.avif" not in html


def test_disabled_concept_film_rejects_nonlocal_sources(tmp_repo):
    _, gen_dir = tmp_repo
    brief = gen_dir / "content" / "testproduct" / "concept-film.json"
    brief.write_text(
        json.dumps(
            {
                "duration_seconds": 8,
                "playback": {
                    "muted": True,
                    "autoplay": True,
                    "playsinline": True,
                    "loop": True,
                    "reduced_motion": "static_poster",
                },
                "performance": {"max_bytes": 1_800_000},
                "asset": {
                    "enabled_in_hero": False,
                    "fallback_src": "HTTPS://example.invalid/film.mp4",
                },
            }
        )
    )
    data = {"concept_film_brief": "content/testproduct/concept-film.json"}

    blocker = build_site._attach_concept_film(data)

    assert blocker is not None
    assert "invalid fallback_src" in blocker
    assert "concept_film" not in data


def test_concept_film_brief_rejects_out_of_range_duration(tmp_repo):
    repo_root, gen_dir = tmp_repo
    brief = gen_dir / "content" / "testproduct" / "concept-film.json"
    brief.write_text(
        json.dumps(
            {
                "duration_seconds": 14,
                "playback": {
                    "muted": True,
                    "autoplay": True,
                    "playsinline": True,
                    "loop": True,
                    "reduced_motion": "static_poster",
                },
                "performance": {"max_bytes": 1_800_000},
                "asset": {},
            }
        )
    )
    content_path = gen_dir / "content" / "testproduct" / "hero.json"
    content_path.write_text(
        json.dumps(
            {
                "layout": "hero",
                "headline": "T",
                "concept_film_brief": "content/testproduct/concept-film.json",
            }
        )
    )
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {
                        "id": "hero",
                        "origin": "page_specific",
                        "source": "content/testproduct/hero.json",
                    }
                ],
            }
        ],
    )

    blockers = build_site.build("testproduct", repo_root / "dist" / "site")
    assert any("must declare a 6–10 second loop" in blocker for blocker in blockers)


def test_authored_section_renders_public_projection_not_internal_source(tmp_repo):
    repo_root, gen_dir = tmp_repo
    (repo_root / "README.md").write_text(
        "# A Title\n\nCandidate B from research_finding ent_deadbeefcafe.\n"
    )
    projection = _write_projection(
        gen_dir,
        "body",
        "README.md",
        headline="A useful public promise",
        body="A concise proof for a cold reader.",
    )

    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {
                        "id": "body",
                        "origin": "authored",
                        "source": "README.md",
                        "projection": projection,
                    }
                ],
            }
        ],
    )

    blockers = build_site.build("testproduct", repo_root / "dist" / "site")
    assert blockers == []
    html = (repo_root / "dist" / "site" / "testproduct" / "index.html").read_text()
    assert "A useful public promise" in html
    assert "A concise proof for a cold reader." in html
    assert "Candidate B" not in html
    assert "ent_deadbeefcafe" not in html
    assert "README.md" not in html


def test_positioning_mirror_missing_is_a_reported_blocker_not_a_silent_gap(tmp_repo):
    """The core guarantee this generator exists for: a declared
    positioning-mirror section whose file is absent must fail the build
    loudly, never render blank or fall back to invented copy."""
    repo_root, gen_dir = tmp_repo
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {
                        "id": "fit",
                        "origin": "positioning_mirror",
                        "source": "docs/positioning/testproduct/icp.md",
                        "source_entity_id": "ent_fake123",
                        "projection": "content/testproduct/public/fit.json",
                    }
                ],
            }
        ],
    )

    out_dir = repo_root / "dist" / "site"
    existing = out_dir / "testproduct" / "index.html"
    existing.parent.mkdir(parents=True)
    existing.write_text("previous complete build")

    blockers = build_site.build("testproduct", out_dir)
    assert len(blockers) == 1
    assert "positioning mirror missing" in blockers[0]
    assert (
        "ent_fake123" in blockers[0]
    )  # the source entity id is named so a reader knows which Neotoma entity to check/correct
    # A partial build must never replace the last complete preview.
    assert existing.read_text() == "previous complete build"


def test_positioning_mirror_present_renders_public_projection(tmp_repo):
    repo_root, gen_dir = tmp_repo
    mirror_path = repo_root / "docs" / "positioning" / "testproduct" / "icp.md"
    mirror_path.write_text(
        '<!-- generated mirror, do not edit -->\n---\ntitle: "Current ICP"\n---\n\n# Current ICP\n\nThe reconciled anti-profile.\n'
    )
    projection = _write_projection(
        gen_dir,
        "fit",
        "docs/positioning/testproduct/icp.md",
        entity_id="ent_fake123",
        headline="Who this is for",
        body="A team that needs continuity.",
    )
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {
                        "id": "fit",
                        "origin": "positioning_mirror",
                        "source": "docs/positioning/testproduct/icp.md",
                        "source_entity_id": "ent_fake123",
                        "projection": projection,
                    }
                ],
            }
        ],
    )

    blockers = build_site.build("testproduct", repo_root / "dist" / "site")
    assert blockers == []
    html = (repo_root / "dist" / "site" / "testproduct" / "index.html").read_text()
    assert "Who this is for" in html
    assert "A team that needs continuity." in html
    assert "The reconciled anti-profile." not in html
    assert "ent_fake123" not in html


def test_public_projection_must_match_source_path_and_entity(tmp_repo):
    repo_root, gen_dir = tmp_repo
    mirror_path = repo_root / "docs" / "positioning" / "testproduct" / "icp.md"
    mirror_path.write_text("# Current ICP\n")
    projection = _write_projection(
        gen_dir,
        "fit",
        "docs/positioning/testproduct/wrong.md",
        entity_id="ent_wrong",
    )
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {
                        "id": "fit",
                        "origin": "positioning_mirror",
                        "source": "docs/positioning/testproduct/icp.md",
                        "source_entity_id": "ent_expected",
                        "projection": projection,
                    }
                ],
            }
        ],
    )

    blockers = build_site.build("testproduct", repo_root / "dist" / "site")
    assert len(blockers) == 1
    assert "declares source" in blockers[0]


def test_missing_design_tokens_is_a_build_blocker_not_a_default(tmp_repo):
    repo_root, gen_dir = tmp_repo
    (gen_dir / "design_tokens" / "testproduct.json").unlink()
    _write_inventory(gen_dir, [{"slug": "index", "title": "T", "sections": []}])

    with pytest.raises(build_site.BuildBlocker, match="design tokens not found"):
        build_site.build("testproduct", repo_root / "dist" / "site")


def test_check_fails_when_disk_differs_from_fresh_build(tmp_repo):
    repo_root, gen_dir = tmp_repo
    content_path = gen_dir / "content" / "testproduct" / "hero.json"
    content_path.write_text(
        json.dumps({"headline": "V1", "subheadline": "", "body": ""})
    )
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {
                        "id": "hero",
                        "origin": "page_specific",
                        "source": "content/testproduct/hero.json",
                    }
                ],
            }
        ],
    )
    out_dir = repo_root / "dist" / "site"
    build_site.build("testproduct", out_dir)
    assert build_site.check("testproduct", out_dir) == 0

    # Change the source without rebuilding dist/ -> check must fail.
    content_path.write_text(
        json.dumps({"headline": "V2", "subheadline": "", "body": ""})
    )
    assert build_site.check("testproduct", out_dir) == 1


def test_successful_build_replaces_tree_and_removes_stale_pages(tmp_repo):
    repo_root, gen_dir = tmp_repo
    content_path = gen_dir / "content" / "testproduct" / "hero.json"
    content_path.write_text(json.dumps({"headline": "Current", "body": ""}))
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {
                        "id": "hero",
                        "origin": "page_specific",
                        "source": "content/testproduct/hero.json",
                    }
                ],
            }
        ],
    )
    out_dir = repo_root / "dist" / "site"
    stale = out_dir / "testproduct" / "removed-page" / "index.html"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")

    assert build_site.build("testproduct", out_dir) == []
    assert not stale.exists()
    assert "Current" in (out_dir / "testproduct" / "index.html").read_text()


def test_multi_page_inventory_renders_site_wide_navigation(tmp_repo):
    repo_root, gen_dir = tmp_repo
    (repo_root / "README.md").write_text("# Design\n")
    projection = _write_projection(gen_dir, "design", "README.md")
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "nav_label": "Home",
                "title": "Home",
                "sections": [
                    {
                        "id": "body",
                        "origin": "authored",
                        "source": "README.md",
                        "projection": projection,
                    }
                ],
            },
            {
                "slug": "design",
                "nav_label": "Design",
                "title": "Design",
                "sections": [
                    {
                        "id": "body",
                        "origin": "authored",
                        "source": "README.md",
                        "projection": projection,
                    }
                ],
            },
        ],
    )

    out_dir = repo_root / "dist" / "site"
    assert build_site.build("testproduct", out_dir) == []
    for page in (
        out_dir / "testproduct" / "index.html",
        out_dir / "testproduct" / "design" / "index.html",
    ):
        html = page.read_text()
        assert '<a href="/">Home</a>' in html
        assert '<a href="/design/">Design</a>' in html


def test_display_path_keeps_explicit_output_outside_repo_absolute():
    path = Path("/tmp/site-generator-preview/index.html")
    assert build_site._display_path(path) == path


def test_checked_in_ateles_inventory_builds_and_checks(tmp_path):
    """Bind the real product proof to execution/scripts' required CI lane."""
    assert build_site.build("ateles", tmp_path) == []
    assert build_site.check("ateles", tmp_path) == 0
    assert (tmp_path / "ateles" / "index.html").exists()
    assert (tmp_path / "ateles" / "design" / "index.html").exists()


@pytest.mark.parametrize(
    ("product", "routes"),
    [
        (
            "neotoma",
            (
                "index.html",
                "install/index.html",
                "evaluate/index.html",
                "compare/index.html",
                "brand/index.html",
            ),
        ),
        (
            "ateles",
            (
                "index.html",
                "design/index.html",
                "compare/index.html",
                "brand/index.html",
                "status/index.html",
            ),
        ),
    ],
)
def test_checked_in_product_sites_build_all_declared_routes(tmp_path, product, routes):
    assert build_site.build(product, tmp_path) == []
    assert build_site.check(product, tmp_path) == 0
    for route in routes:
        page = tmp_path / product / route
        assert page.exists()
        assert page.read_text().count("<h1") == 1


def test_product_homepages_bind_ambition_first_sequence_and_categories(tmp_path):
    for product in ("neotoma", "ateles"):
        assert build_site.build(product, tmp_path) == []

    neotoma = (tmp_path / "neotoma" / "index.html").read_text()
    ateles = (tmp_path / "ateles" / "index.html").read_text()

    assert (
        neotoma.index('id="hero"')
        < neotoma.index('id="failures"')
        < neotoma.index('id="mechanism"')
    )
    assert (
        ateles.index('id="hero"')
        < ateles.index('id="failures"')
        < ateles.index('id="mechanism"')
    )
    assert "The system of record for AI agents" in neotoma
    assert "The operating system for agentic organizations" in ateles
    assert "agent forgot" not in neotoma.lower()
    assert "agents forget" not in neotoma.lower()


def test_product_identities_render_distinct_signature_devices(tmp_path):
    for product in ("neotoma", "ateles"):
        assert build_site.build(product, tmp_path) == []

    neotoma = (tmp_path / "neotoma" / "index.html").read_text()
    ateles = (tmp_path / "ateles" / "index.html").read_text()
    neotoma_tokens = json.loads(
        (_GEN_DIR / "design_tokens" / "neotoma.json").read_text()
    )
    ateles_tokens = json.loads((_GEN_DIR / "design_tokens" / "ateles.json").read_text())

    neotoma_type = neotoma_tokens["product"]["type_scale"]
    ateles_type = ateles_tokens["product"]["type_scale"]
    neotoma_accent = neotoma_tokens["product"]["color_palette"]["light"]["accent"]
    ateles_accent = ateles_tokens["product"]["color_palette"]["light"]["accent"]
    assert neotoma_type["heading_font_family"] in neotoma
    assert ateles_type["heading_font_family"] in ateles
    assert neotoma_accent in neotoma
    assert ateles_accent in ateles
    assert neotoma_type["heading_font_family"] != ateles_type["heading_font_family"]
    assert neotoma_accent != ateles_accent
    renderer_source = Path(tpl.__file__).read_text()
    for literal in ("Fraunces", "Space Grotesk", "#1f6f5c", "#8a3b1f"):
        assert literal not in renderer_source

    assert neotoma_tokens["_source"]["entity_id"] == "ent_746b1d7c717e7780e7943782"
    assert ateles_tokens["_source"]["entity_id"] == "ent_9158c8b39e0f437fdb2a86de"
    assert "concept-film-poster-media" in neotoma
    assert "swarm-field" in ateles
    assert "swarm-member" in ateles
    assert "handoff-signal" in ateles
    assert "org-link" not in ateles
    assert "the swarm" in ateles
    assert "swarm-mark" in ateles
    assert "seal-board" not in ateles
    # The seal remains only as a small state marker attached to a specific
    # authorization/checkpoint result, not as the product symbol.
    assert "authorization-seal" in ateles
    assert "grant-state" in ateles
    assert 'data-concept-film-active="true"' in neotoma
    assert 'data-concept-film-active="false"' in ateles
    assert "<video" in neotoma
    assert "<video" not in ateles
    assert (tmp_path / "neotoma/assets/neotoma/hero-concept.webm").exists()
    # The exploratory Ateles film is available for review on /brand/ while
    # remaining disabled in the hero; inventory does not imply promotion.
    assert (tmp_path / "ateles/assets/ateles/hero-concept.webm").exists()
    assert "prefers-reduced-motion: reduce" in neotoma
    assert ".concept-film-media, .concept-film-overlay { display: none; }" in neotoma
    assert "The system of record for AI agents." in neotoma
    assert (
        "Persistent, connected context agents can create, retrieve, and update—with provenance intact."
        in neotoma
    )
    assert "Delegate more than a session can hold." not in neotoma
    assert "The operating system for agentic organizations." in ateles
    assert (
        "Give agents distinct roles, bounded authority, and shared direction—so the organization keeps moving without constant supervision."
        in ateles
    )
    assert ateles.count("Delegate outcomes, not every next step.") == 1


def test_neotoma_cinematic_hero_has_no_semantic_svg_or_overlay(tmp_path):
    assert build_site.build("neotoma", tmp_path) == []
    document = (tmp_path / "neotoma" / "index.html").read_text()
    hero_match = re.search(
        r'(?s)<section class="takeover-hero record-hero" id="hero">(.*?)</section>',
        document,
    )
    assert hero_match is not None
    hero = hero_match.group(1)

    assert '<video class="concept-film-media"' in hero
    assert '<img class="concept-film-poster-media"' in hero
    assert "<svg" not in hero
    assert "concept-film-overlay" not in hero
    assert "record-semantic-overlay" not in hero
    assert "record-graph" not in hero
    for diagram_label in (
        "CREATE",
        "UPDATE",
        "RETRIEVE",
        "CURRENT RECORD",
        "PROVENANCE",
        "PRIOR STATE",
    ):
        assert diagram_label not in hero

    # Calibrate the binding validator with a known-positive forbidden overlay.
    rendered, blockers = build_site.render_site("neotoma")
    assert blockers == []
    mutated = dict(rendered)
    mutated[Path("index.html")] = str(mutated[Path("index.html")]).replace(
        '<div class="concept-film-poster">',
        '<div class="concept-film-overlay"><svg></svg></div>'
        '<div class="concept-film-poster">',
        1,
    )
    inventory = json.loads((_GEN_DIR / "inventory" / "neotoma.json").read_text())
    overlay_blockers = build_site._validate_site("neotoma", inventory, mutated)
    assert any(
        "must not contain a semantic overlay" in item for item in overlay_blockers
    )


def test_generated_html_shows_product_motifs(tmp_path):
    for product in ("neotoma", "ateles"):
        assert build_site.build(product, tmp_path) == []

    neotoma = "\n".join(
        path.read_text() for path in sorted((tmp_path / "neotoma").rglob("*.html"))
    )
    ateles = "\n".join(
        path.read_text() for path in sorted((tmp_path / "ateles").rglob("*.html"))
    )

    # Durable epistemological cues: the generated public surface must show
    # where a claim came from and retain its version history and disagreement.
    for motif in (
        "provenance",
        "superseded",
        "disagreement",
        "version",
        "history",
    ):
        assert motif.casefold() in neotoma.casefold(), motif
    # These exact source-backed cues are part of the signed acceptance contract;
    # near-synonyms must not make the effect test pass.
    for motif in ("supersession", "REFRESH", "effective time"):
        assert motif.casefold() in neotoma.casefold(), motif

    # Durable organizational cues: Ateles must read as roles coordinating
    # through bounded, temporary handoffs rather than as a persistent graph.
    for motif in (
        "relational",
        "grant-state",
        "CHECKPOINT",
        "quorum",
        "coordinated",
        "handoff-signal",
    ):
        assert motif.casefold() in ateles.casefold(), motif
    assert "org-link" not in ateles
    hierarchy_markers = [
        f'class="hierarchy-item">{level}'
        for level in (
            "Mission",
            "Strategy",
            "Project",
            "Plan",
            "Task",
        )
    ]
    assert [ateles.index(marker) for marker in hierarchy_markers] == sorted(
        ateles.index(marker) for marker in hierarchy_markers
    )


def test_product_routes_reject_disclosure_components_and_keep_named_links(tmp_path):
    for product in ("neotoma", "ateles"):
        assert build_site.build(product, tmp_path) == []
        pages = sorted((tmp_path / product).rglob("*.html"))
        assert len(pages) == 5
        for page in pages:
            document = page.read_text()
            assert "<details" not in document.casefold(), page
            assert "<summary" not in document.casefold(), page
            assert "section-details" not in document, page
            for section in re.findall(
                r"(?s)(<section\b[^>]*class=\"[^\"]*visual-section[^\"]*\"[^>]*>.*?</section>)",
                document,
            ):
                assert 'class="section-link"' in section or 'class="btn' in section

    # Prove the validator fails on the forbidden interaction, instead of
    # treating a zero count as sufficient evidence.
    rendered, blockers = build_site.render_site("neotoma")
    assert blockers == []
    mutated = dict(rendered)
    mutated[Path("index.html")] = str(mutated[Path("index.html")]).replace(
        "</section>", "<details><summary>More</summary></details></section>", 1
    )
    neotoma_inventory = json.loads(
        (_GEN_DIR / "inventory" / "neotoma.json").read_text()
    )
    assert any(
        "must not hide copy" in blocker
        for blocker in build_site._validate_site("neotoma", neotoma_inventory, mutated)
    )


def test_ateles_status_distinguishes_vision_from_execution(tmp_path):
    assert build_site.build("ateles", tmp_path) == []
    status = (tmp_path / "ateles" / "status" / "index.html").read_text()
    assert "Vision and execution status" in status
    assert "nothing built" in status


@contextlib.contextmanager
def _serve_directory(directory: Path):
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(directory),
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _launch_chromium(playwright):
    """Use installed Chrome in CI; use Playwright Chromium when available."""
    executable = next(
        (
            path
            for path in (
                shutil.which("google-chrome"),
                shutil.which("chromium"),
                shutil.which("chromium-browser"),
            )
            if path
        ),
        None,
    )
    options = {"executable_path": executable} if executable else {}
    return playwright.chromium.launch(headless=True, **options)


def test_all_routes_have_no_document_overflow_at_390px(tmp_path):
    for product in ("neotoma", "ateles"):
        assert build_site.build(product, tmp_path) == []

    with sync_playwright() as playwright:
        browser = _launch_chromium(playwright)
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})

            # Prove the browser measurement can detect a known overflow before
            # trusting a zero-overflow result from the generated pages.
            overflow_fixture = tmp_path / "overflow-fixture.html"
            overflow_fixture.write_text(
                '<!doctype html><div style="width:800px;height:1px"></div>'
            )
            page.goto(overflow_fixture.resolve().as_uri())
            assert page.evaluate(
                "document.documentElement.scrollWidth > "
                "document.documentElement.clientWidth"
            )

            routes = {
                "neotoma": ("/", "/install/", "/evaluate/", "/compare/", "/brand/"),
                "ateles": ("/", "/design/", "/compare/", "/status/", "/brand/"),
            }
            for product, product_routes in routes.items():
                with _serve_directory(tmp_path / product) as base_url:
                    for route in product_routes:
                        page.goto(f"{base_url}{route}", wait_until="load")
                        widths = page.evaluate(
                            """() => ({
                              scroll: document.documentElement.scrollWidth,
                              client: document.documentElement.clientWidth,
                              htmlOverflow: getComputedStyle(document.documentElement).overflowX,
                              bodyOverflow: getComputedStyle(document.body).overflowX,
                            })"""
                        )
                        assert widths["scroll"] <= widths["client"], (
                            product,
                            route,
                            widths,
                        )
                        assert widths["htmlOverflow"] not in {"hidden", "clip"}
                        assert widths["bodyOverflow"] not in {"hidden", "clip"}

                    page.set_viewport_size({"width": 1280, "height": 800})
                    page.goto(f"{base_url}/", wait_until="load")
                    assert (
                        page.locator(".nav-in").evaluate(
                            "element => getComputedStyle(element).flexWrap"
                        )
                        == "nowrap"
                    )
                    page.evaluate("scrollTo(0, 40)")
                    page.wait_for_function(
                        "document.querySelector('#site-nav').classList.contains('scrolled')"
                    )
                    page.set_viewport_size({"width": 390, "height": 844})
        finally:
            browser.close()


@pytest.mark.parametrize("product", ["neotoma", "ateles"])
def test_every_major_section_is_visual_first_with_contextual_depth_link(
    tmp_path, product
):
    rendered, blockers = build_site.render_site(product)
    assert blockers == []
    for rel, document in rendered.items():
        if isinstance(document, bytes):
            continue
        sections = re.findall(r"(?s)(<section\b[^>]*>.*?</section>)", document)
        assert sections, rel
        for section in sections:
            if 'id="hero"' in section:
                continue
            assert "section-visual" in section, rel
        assert ">Learn more<" not in document


def test_selected_markdown_heading_must_exist(tmp_repo):
    repo_root, gen_dir = tmp_repo
    (repo_root / "README.md").write_text("# Present\n\nBody.\n")
    projection = _write_projection(gen_dir, "body", "README.md")
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {
                        "id": "body",
                        "origin": "authored",
                        "source": "README.md",
                        "headings": ["Missing"],
                        "projection": projection,
                    }
                ],
            }
        ],
    )

    blockers = build_site.build("testproduct", repo_root / "dist" / "site")
    assert len(blockers) == 1
    assert "missing headings: Missing" in blockers[0]


def test_internal_leakage_scanner_detects_known_positive_fixture():
    fixture = """
    <main><p>Candidate B · research_finding · ent_deadbeefcafe</p>
    <p>Rendered from README.md during Phase 3. [blast:low]</p></main>
    """
    leaks = build_site._public_copy_leaks(Path("fixture.html"), fixture)
    assert {leak.rsplit(" ", 2)[-2] for leak in leaks}  # prove non-empty
    labels = " ".join(leaks)
    assert "candidate label" in labels
    assert "entity id" in labels
    assert "entity-type label" in labels
    assert "repo filename" in labels
    assert "internal scope notation" in labels
    assert "analysis narration" in labels
    assert "phase bookkeeping" in labels


@pytest.mark.parametrize("product", ["neotoma", "ateles"])
def test_checked_in_public_routes_have_no_internal_leakage(tmp_path, product):
    rendered, blockers = build_site.render_site(product)
    assert blockers == []
    for rel, document in rendered.items():
        assert build_site._public_copy_leaks(rel, document) == []


@pytest.mark.parametrize("product", ("ateles", "neotoma"))
def test_brand_route_projects_complete_public_safe_contract(tmp_path, product):
    assert build_site.build(product, tmp_path) == []
    document = (tmp_path / product / "brand" / "index.html").read_text()
    for marker in (
        "Brand intent",
        "Voice and language",
        "Visual system",
        "Recommended aesthetic territory",
        "Rejected aesthetic alternatives",
        "Aesthetic convergence tests",
        "internal creative platform subordinate to the settled category",
        "not an alternate category noun, public headline, or tagline",
        "Logo system",
        "Typography system",
        "Assets",
        "Cinematic grammar",
        "Accessibility",
        "Cinematic generation gate",
        "Not an approved brand baseline.",
        "The category and product argument are settled",
        "operator approval or revision of this provisional brand system",
        "Market-reference learning ledger",
        "Cross-product differentiation",
        "Observed:",
        "Inference:",
        "Adopt",
        "Avoid",
        "Differentiate",
        "Do not drift here",
        "Completeness and provenance",
        "Downstream contracts",
        "brand-status-approved",
        "brand-status-provisional",
        "brand-status-missing",
    ):
        assert marker in document
    assert "ent_" not in document
    assert "repository_path" not in document
    assert "README.md" not in document
    assert "<details" not in document.casefold()
    assert build_site._public_copy_leaks(Path("brand/index.html"), document) == []
    schema = json.loads(
        (_GEN_DIR / "brand_systems" / "schema.v1.json").read_text()
    )
    assert 'data-brand-review-summary="COMPLETE"' in document
    assert document.index("Review-completeness summary") < document.index(
        'class="candidate-brand-canvas"'
    )
    for requirement in schema["x-review-deliverables"]:
        deliverable_id = requirement["id"]
        anchor = deliverable_id.replace(".", "-").replace("_", "-")
        assert f'id="review-{anchor}"' in document
        assert f'data-brand-deliverable="{deliverable_id}"' in document
        assert f'data-brand-proof="{deliverable_id}"' in document
    assert "Completeness does not mean brand approval" in document
    assert "permission to begin cinematic production" in document


@pytest.mark.parametrize(
    ("product", "families"),
    (
        ("ateles", ("Space Grotesk", "Source Sans 3", "IBM Plex Mono")),
        ("neotoma", ("Fraunces", "Inter", "JetBrains Mono")),
    ),
)
def test_brand_route_renders_judgeable_visual_specimens(tmp_path, product, families):
    assert build_site.build(product, tmp_path) == []
    document = (tmp_path / product / "brand" / "index.html").read_text()
    assert 'data-review-shell="neutral-v1"' in document
    assert f'data-candidate-brand="{product}"' in document
    for token in ("display", "section_heading", "body", "label"):
        specimen = re.search(
            rf'<article[^>]+data-brand-specimen="typography-{token}"[^>]+>',
            document,
        )
        assert specimen, token
        assert "font-family:" in specimen.group(0)
        assert "font-size:" in specimen.group(0)
        assert "line-height:" in specimen.group(0)
    for family in families:
        assert f'data-font-family="{family}"' in document
    assert document.count("data-brand-swatch=") >= 20
    assert 'data-palette-theme="light"' in document
    assert 'data-palette-theme="dark"' in document
    for marker in (
        "data-material-specimen=",
        "data-story-frame=",
        "data-reduced-motion-equivalent",
        'data-voice-sample="forbidden"',
        'data-voice-sample="preferred"',
        'data-accessibility-specimen="contrast"',
        'data-accessibility-specimen="reflow"',
        'data-accessibility-specimen="reduced-motion"',
    ):
        assert marker in document


def test_brand_routes_share_neutral_chrome_and_scope_candidate_styles(tmp_path):
    documents = {}
    for product in ("ateles", "neotoma"):
        assert build_site.build(product, tmp_path) == []
        documents[product] = (tmp_path / product / "brand" / "index.html").read_text()
    styles = {
        product: re.search(r"<style>(.*?)</style>", document, re.DOTALL).group(1)
        for product, document in documents.items()
    }
    assert styles["ateles"] == styles["neotoma"]
    assert "#f4f4f2" in styles["ateles"]
    assert "Space Grotesk" not in styles["ateles"]
    assert "Fraunces" not in styles["neotoma"]
    candidate_colors = {
        "ateles": ("#8a3b1f", "#f7f4ee"),
        "neotoma": ("#1f6f5c", "#f6f3ea"),
    }
    for product, document in documents.items():
        start = document.index('<div class="candidate-brand-canvas"')
        end = document.index(
            '</div>\n<figure class="section-visual brand-foundation-map"', start
        )
        outside = document[:start] + document[end:]
        for color in candidate_colors[product]:
            assert color in document[start:end]
            assert color not in outside


@pytest.mark.parametrize("product", ("ateles", "neotoma"))
def test_provisional_logo_family_renders_source_backed_assets(tmp_path, product):
    assert build_site.build(product, tmp_path) == []
    document = (tmp_path / product / "brand" / "index.html").read_text()
    required = (
        "primary_mark",
        "wordmark",
        "lockup",
        "horizontal",
        "stacked",
        "monochrome",
        "reversed",
        "favicon",
        "small_scale",
        "symbol_only",
    )
    for key in required:
        assert f'data-logo-variant="{key}"' in document
        start = document.index(f'data-logo-variant="{key}"')
        chunk = document[start : start + 500]
        assert "<img " in chunk, f"{product}/{key} must render a source-backed image"
        assert "brand-logo-placeholder" not in chunk
    # Placeholders remain only for genuinely missing non-logo assets if present.
    placeholders = re.findall(
        r'<div class="brand-logo-placeholder" data-logo-state="missing">(.*?)</div>',
        document,
        re.DOTALL,
    )
    assert all("<svg" not in value and "<img" not in value for value in placeholders)
    assert f'src="/assets/{product}/marks/primary.svg"' in document
    assert f'/assets/{product}/marks/' in document
    # Historical evidence preserved
    if product == "ateles":
        assert "hub-satellites.svg" in document or "Existing application lockup" in document
    else:
        assert "neotoma-wordmark.svg" in document or "historical/wordmark.svg" in document


def test_ateles_and_neotoma_marks_anti_converge_and_obey_prohibitions():
    ateles = (
        build_site.REPO_ROOT
        / "execution/scripts/site_generator/assets/ateles/marks/primary.svg"
    ).read_text()
    neotoma = (
        build_site.REPO_ROOT
        / "execution/scripts/site_generator/assets/neotoma/marks/primary.svg"
    ).read_text()
    assert ateles != neotoma
    assert "<line" not in ateles
    # Geometry: no hub-and-satellites ring (historical pattern uses r=3.7 core).
    assert 'r="3.7"' not in ateles
    assert "M24 28" in neotoma  # durable edge
    assert 'opacity="0.35"' in neotoma  # prior-state continuity
    # Strip titles/descriptions before checking for prohibited iconography words.
    geometry = re.sub(r"<(title|desc)\b[^>]*>.*?</\1>", "", ateles + neotoma, flags=re.I | re.S)
    for banned in ("hive", "insect", "brain", "cloud", "cylinder", "database"):
        assert banned not in geometry.casefold()


def test_mark_clear_space_and_minimum_size_are_geometry_derived():
    for product in ("ateles", "neotoma"):
        contract = json.loads(
            (build_site.BRAND_SYSTEMS_DIR / f"{product}.json").read_text()
        )
        logo = contract["visual_styles"]["logo_system"]
        assert logo["clear_space"]["status"] == "provisional"
        assert logo["clear_space"]["measurement"]
        assert "0.5" in logo["clear_space"]["measurement"]
        assert logo["minimum_size"]["status"] == "provisional"
        assert logo["minimum_size"]["digital"]
        assert logo["minimum_size"]["print"]
        assert logo["co_branding"]["status"] == "provisional"
        for key in (
            "primary_mark",
            "wordmark",
            "lockup",
            "horizontal",
            "stacked",
            "monochrome",
            "reversed",
            "favicon",
            "symbol_only",
        ):
            variant = logo["variants"][key]
            assert variant["source_asset"]
            assert (build_site.REPO_ROOT / variant["source_asset"]).is_file()


def test_public_url_policy_rejects_active_and_encoded_traversal_urls():
    from url_policy import local_asset_url, public_href

    assert public_href("javascript:alert(1)") is None
    assert public_href("http://example.com") is None
    assert public_href("https://example.com/path") == "https://example.com/path"
    assert local_asset_url("/assets/%2e%2e/private.txt") is None
    assert local_asset_url("/assets/film.webm") == "/assets/film.webm"


def test_brand_route_internal_leakage_validator_catches_known_positive(tmp_path):
    assert build_site.build("ateles", tmp_path) == []
    document = (tmp_path / "ateles" / "brand" / "index.html").read_text()
    leaked = document.replace(
        "One source, visible state.", "Entity ent_deadbeef1234567890"
    )
    blockers = build_site._public_copy_leaks(Path("brand/index.html"), leaked)
    assert any("entity id" in blocker for blocker in blockers)


def test_brand_gate_projection_strips_internal_entity_ids():
    projected = tpl._public_gate_text(
        "Exact category was read back in ent_deadbeef1234567890."
    )
    assert projected == "Exact category was read back in the canonical record."
    assert "ent_" not in projected


def test_design_tokens_drive_css_output_no_hardcoded_colors():
    css = tpl.build_css(FIXTURE_TOKENS)
    assert "#900" in css  # the fixture's light accent, not a hardcoded default
    assert "#f90" in css  # the fixture's dark accent
    assert "Test Sans, sans-serif" in css


def test_minimal_markdown_strips_leading_html_comment_before_frontmatter():
    raw = '<!-- do not edit -->\n---\ntitle: "Hi"\n---\n\n# Heading\n\nBody.\n'
    fm, body = mdlib.strip_frontmatter(raw)
    assert fm.get("title") == "Hi"
    assert "<!--" not in body
    assert "Heading" in body


def test_minimal_markdown_handles_no_frontmatter():
    fm, body = mdlib.strip_frontmatter("# Just a heading\n\nplain text\n")
    assert fm == {}
    assert "Just a heading" in body


def test_minimal_markdown_to_html_covers_supported_subset():
    html = mdlib.to_html(
        "# H1\n\nA **bold** word and a [link](https://example.com).\n\n"
        "> A quoted constraint.\n\n- one\n- two\n\n1. first\n2. second\n\n"
        "| A | B |\n| --- | --- |\n| one | two |\n\n"
        "```bash\necho ok\n```\n"
    )
    assert '<h1 id="h1">H1</h1>' in html
    assert "<strong>bold</strong>" in html
    assert '<a href="https://example.com">link</a>' in html
    assert "<li>one</li>" in html
    assert "<blockquote>" in html
    assert "<ol><li>first</li>" in html
    assert "<table>" in html
    assert '<code class="language-bash">echo ok</code>' in html


def test_minimal_markdown_rewrites_relative_links_to_declared_source_base():
    html = mdlib.to_html(
        "[design](docs/foundation/)",
        link_base="https://github.com/example/project/blob/main/",
    )
    assert (
        'href="https://github.com/example/project/blob/main/docs/foundation/"' in html
    )


def test_minimal_markdown_fails_closed_on_unsafe_links():
    html = mdlib.to_html("[unsafe](javascript:alert(1))")
    assert 'href="#"' in html
    assert "javascript:" not in html


def test_preview_refuses_to_serve_a_build_with_unresolved_sections(
    tmp_path, monkeypatch
):
    """A visible blocker must stop the preview, not become a reviewable page."""
    product_dir = tmp_path / "dist" / "site" / "testproduct"
    product_dir.mkdir(parents=True)
    monkeypatch.setattr(preview_server, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_site, "DEFAULT_OUT_DIR", tmp_path / "dist" / "site")
    monkeypatch.setattr(
        build_site,
        "build",
        lambda product, out_dir: ["index#fit: positioning mirror missing"],
    )

    def _must_not_serve(*args, **kwargs):
        raise AssertionError("preview server started despite unresolved sections")

    monkeypatch.setattr(
        preview_server.http.server, "ThreadingHTTPServer", _must_not_serve
    )
    monkeypatch.setattr(
        sys, "argv", ["preview_server.py", "testproduct", "--port", "8143"]
    )

    assert preview_server.main() == 1
