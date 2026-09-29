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

import json
import sys
from pathlib import Path

import pytest

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


def test_authored_section_reads_repo_file_verbatim(tmp_repo):
    repo_root, gen_dir = tmp_repo
    (repo_root / "README.md").write_text("# A Title\n\nSome authored body text.\n")

    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "sections": [
                    {"id": "body", "origin": "authored", "source": "README.md"}
                ],
            }
        ],
    )

    blockers = build_site.build("testproduct", repo_root / "dist" / "site")
    assert blockers == []
    html = (repo_root / "dist" / "site" / "testproduct" / "index.html").read_text()
    assert "Some authored body text." in html
    assert "README.md" in html  # source path must be visibly attributed


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


def test_positioning_mirror_present_renders_current_content(tmp_repo):
    repo_root, gen_dir = tmp_repo
    mirror_path = repo_root / "docs" / "positioning" / "testproduct" / "icp.md"
    mirror_path.write_text(
        '<!-- generated mirror, do not edit -->\n---\ntitle: "Current ICP"\n---\n\n# Current ICP\n\nThe reconciled anti-profile.\n'
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
                    }
                ],
            }
        ],
    )

    blockers = build_site.build("testproduct", repo_root / "dist" / "site")
    assert blockers == []
    html = (repo_root / "dist" / "site" / "testproduct" / "index.html").read_text()
    assert "The reconciled anti-profile." in html
    assert "ent_fake123" in html  # source entity id attributed on the page


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
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "nav_label": "Home",
                "title": "Home",
                "sections": [
                    {"id": "body", "origin": "authored", "source": "README.md"}
                ],
            },
            {
                "slug": "design",
                "nav_label": "Design",
                "title": "Design",
                "sections": [
                    {"id": "body", "origin": "authored", "source": "README.md"}
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
        'href="https://github.com/example/project/blob/main/docs/foundation/"'
        in html
    )


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

    monkeypatch.setattr(preview_server.http.server, "ThreadingHTTPServer", _must_not_serve)
    monkeypatch.setattr(
        sys, "argv", ["preview_server.py", "testproduct", "--port", "8143"]
    )

    assert preview_server.main() == 1


# ---------------------------------------------------------------------------
# Security: link schemes, source-path containment, product/slug names, CSS.
# Each test below was run against the pre-fix generator and failed there.
# ---------------------------------------------------------------------------

from templates.safe_url import safe_href  # noqa: E402


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert%28document.cookie%29",
        "JaVaScRiPt:alert%281%29",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "vbscript:msgbox",
        "file:///etc/passwd",
        "java\tscript:alert%281%29",
        " javascript:alert%281%29",
        "java&#x09;script:alert%281%29",
        "javascript&#58;alert%281%29",
        "​javascript:alert%281%29",
    ],
)
def test_safe_href_rejects_dangerous_schemes(href):
    assert safe_href(href) == "#"


@pytest.mark.parametrize(
    "href",
    [
        "https://example.com/a?b=c",
        "http://example.com",
        "mailto:someone@example.com",
        "/docs/page/",
        "#section",
        "relative/path.md",
        "../up.md",
        "?q=1",
    ],
)
def test_safe_href_keeps_ordinary_links(href):
    assert safe_href(href) == href


def test_markdown_link_with_dangerous_scheme_is_neutralised():
    for payload in (
        "[x](javascript:alert%28document.cookie%29)",
        "[x](JaVaScRiPt:alert%281%29)",
        "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    ):
        out = mdlib.to_html(payload)
        assert 'href="#"' in out
        assert "javascript" not in out.lower()
        assert "data:text" not in out


def test_markdown_link_keeps_https_and_rewrites_relative_with_base():
    out = mdlib.to_html("[a](https://example.com/x) [b](y.md)", link_base="https://h/o/")
    assert 'href="https://example.com/x"' in out
    assert 'href="https://h/o/y.md"' in out


def test_page_specific_cta_hrefs_are_gated(tmp_repo):
    repo_root, gen_dir = tmp_repo
    (gen_dir / "content" / "testproduct" / "hero.json").write_text(
        json.dumps(
            {
                "headline": "H",
                "secondary_cta": {"label": "Go", "href": "javascript:alert%281%29"},
            }
        )
    )
    (gen_dir / "content" / "testproduct" / "other.json").write_text(
        json.dumps(
            {
                "headline": "O",
                "lede": "l",
                "sibling": [{"title": "S", "body": "b", "href": "data:text/html,x"}],
            }
        )
    )
    _write_inventory(
        gen_dir,
        [
            {
                "slug": "index",
                "title": "T",
                "primary_cta": {"label": "Nav", "href": "JAVASCRIPT:alert%281%29"},
                "sections": [
                    {"id": "hero", "origin": "page_specific", "source": "content/testproduct/hero.json"},
                    {"id": "the-other-half", "origin": "page_specific", "source": "content/testproduct/other.json"},
                ],
            }
        ],
    )
    assert build_site.build("testproduct", repo_root / "dist" / "site") == []
    html = (repo_root / "dist" / "site" / "testproduct" / "index.html").read_text()
    assert "javascript" not in html.lower()
    assert "data:text" not in html


def _single_section_inventory(gen_dir, origin, source):
    _write_inventory(
        gen_dir,
        [{"slug": "index", "title": "T", "sections": [{"id": "s", "origin": origin, "source": source}]}],
    )


@pytest.mark.parametrize(
    "origin,source",
    [
        ("authored", "../outside.md"),
        ("authored", "/etc/passwd"),
        ("positioning_mirror", "../../README.md"),
        ("positioning_mirror", "../../../../../outside.md"),
        ("positioning_mirror", "docs/positioning/../../sibling_of_docs.md"),
        ("page_specific", "content/../../../outside.json"),
    ],
)
def test_source_paths_that_escape_their_root_are_blockers_and_not_read(tmp_repo, origin, source):
    repo_root, gen_dir = tmp_repo
    canary = "CANARY-OUTSIDE-ROOT"
    (repo_root / "sibling_of_docs.md").write_text(canary)
    (repo_root / "README.md").write_text(canary)
    (repo_root.parent / "outside.md").write_text(canary)
    (gen_dir.parent.parent / "outside.json").write_text(json.dumps({"headline": canary}))
    _single_section_inventory(gen_dir, origin, source)
    out_dir = repo_root / "dist" / "site"
    blockers = build_site.build("testproduct", out_dir)
    assert blockers, "an escaping source must block the build"
    assert not (out_dir / "testproduct").exists()


def test_symlink_out_of_root_is_refused(tmp_repo, tmp_path):
    repo_root, gen_dir = tmp_repo
    outside = tmp_path / "outside.md"
    outside.write_text("CANARY-OUTSIDE-ROOT")
    (repo_root / "docs" / "positioning" / "testproduct" / "link.md").symlink_to(outside)
    _single_section_inventory(gen_dir, "positioning_mirror", "docs/positioning/testproduct/link.md")
    assert build_site.build("testproduct", repo_root / "dist" / "site")


def test_authored_source_must_be_markdown_and_not_a_dotenv(tmp_repo):
    repo_root, gen_dir = tmp_repo
    (repo_root / ".env").write_text("K=V")
    (repo_root / "config.json").write_text("{}")
    for source in (".env", "config.json"):
        _single_section_inventory(gen_dir, "authored", source)
        assert build_site.build("testproduct", repo_root / "dist" / "site")


def test_design_tokens_path_escape_is_a_blocker(tmp_repo):
    repo_root, gen_dir = tmp_repo
    (repo_root.parent / "evil.json").write_text(json.dumps(FIXTURE_TOKENS))
    inv = {"product": "testproduct", "design_tokens": "../../../../evil.json", "pages": []}
    (gen_dir / "inventory" / "testproduct.json").write_text(json.dumps(inv))
    with pytest.raises(build_site.BuildBlocker):
        build_site.render_site("testproduct")


@pytest.mark.parametrize("product", ["../evil", "a/b", "A", "", "x;y", "..", "-x"])
def test_product_name_is_validated_before_any_path_join(tmp_repo, product):
    repo_root, gen_dir = tmp_repo
    # A real inventory one level above INVENTORY_DIR: an unvalidated join
    # would load it.
    (gen_dir / "evil.json").write_text(json.dumps({"product": "evil", "design_tokens": "x", "pages": []}))
    with pytest.raises(build_site.BuildBlocker):
        build_site.load_inventory(product)


def test_preview_server_rejects_bad_product_before_serving(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["preview_server.py", "../evil", "--no-build"])
    assert preview_server.main() == 1


def test_page_slug_is_validated(tmp_repo):
    repo_root, gen_dir = tmp_repo
    _write_inventory(gen_dir, [{"slug": "../../escape", "title": "T", "sections": []}])
    with pytest.raises(build_site.BuildBlocker):
        build_site.render_site("testproduct")


@pytest.mark.parametrize(
    "where,value",
    [
        ("heading_font_family", "x}</style><script>alert(1)</script>"),
        ("body_font_family", "a; background:url(http://evil/x)"),
        ("h1_size", "3rem;}body{display:none"),
        ("code_font_family", "mono /* c */"),
    ],
)
def test_unsafe_design_token_values_stop_the_build(where, value):
    import copy

    tokens = copy.deepcopy(FIXTURE_TOKENS)
    tokens["product"]["type_scale"][where] = value
    with pytest.raises(tpl.TokenError):
        tpl.build_css(tokens)


def test_unsafe_palette_value_stops_the_build():
    import copy

    tokens = copy.deepcopy(FIXTURE_TOKENS)
    tokens["product"]["color_palette"]["light"]["ink"] = "#000;}</style>"
    with pytest.raises(tpl.TokenError):
        tpl.build_css(tokens)


def test_token_error_surfaces_as_build_blocker(tmp_repo):
    repo_root, gen_dir = tmp_repo
    import copy

    tokens = copy.deepcopy(FIXTURE_TOKENS)
    tokens["product"]["type_scale"]["h1_size"] = "3rem;}"
    (gen_dir / "design_tokens" / "testproduct.json").write_text(json.dumps(tokens))
    _write_inventory(gen_dir, [{"slug": "index", "title": "T", "sections": []}])
    with pytest.raises(build_site.BuildBlocker):
        build_site.render_site("testproduct")


# ---------------------------------------------------------------------------
# Preview server bind address. The default must be loopback: the served tree
# includes the internal /brand/ route and the server has no authentication.
# ---------------------------------------------------------------------------


class _StopServing(Exception):
    pass


def _capture_bind(monkeypatch, tmp_path, argv):
    (tmp_path / "dist" / "site" / "testproduct").mkdir(parents=True)
    monkeypatch.setattr(preview_server, "REPO_ROOT", tmp_path)
    seen = {}

    def fake_server(address, handler):
        seen["address"] = address
        raise _StopServing

    monkeypatch.setattr(preview_server.http.server, "ThreadingHTTPServer", fake_server)
    monkeypatch.setattr(sys, "argv", ["preview_server.py", "testproduct", "--no-build", *argv])
    with pytest.raises(_StopServing):
        preview_server.main()
    return seen["address"]


def test_preview_server_binds_loopback_by_default(monkeypatch, tmp_path):
    assert _capture_bind(monkeypatch, tmp_path, ["--port", "8199"]) == ("127.0.0.1", 8199)


def test_preview_server_binds_all_interfaces_only_with_lan_flag(monkeypatch, tmp_path):
    assert _capture_bind(monkeypatch, tmp_path, ["--port", "8199", "--lan"]) == ("0.0.0.0", 8199)
