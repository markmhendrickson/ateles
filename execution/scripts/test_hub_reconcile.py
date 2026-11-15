"""
Safety contract for hub_reconcile.py.

The hub body is arbitrary HTML and `correct` REPLACES the whole field. The tool
therefore interprets none of it: it inserts one escaped card immediately before
one exact marker and confirms the stored body equals that proposed body byte for
byte. These tests pin the splice, refusal paths, private dry-run files, and
token-safe output.

Run with:
    uv run --extra dev python -m pytest execution/scripts/test_hub_reconcile.py -v
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "execution" / "scripts"))

import hub_reconcile as H  # noqa: E402

BASE = "https://example-neotoma.fly.dev"
HUB_ID = "ent_" + "9" * 24
NEW_ID = "ent_" + "c" * 24
MARKER_NAME = "materials"
MARKER = f"<!-- hub-cards:{MARKER_NAME} -->"


@pytest.fixture
def hub() -> str:
    """Arbitrary bytes around the marker, including deliberately odd HTML."""
    return (
        "<h1>Shared materials</h1>\n"
        "<a class='not-a-card cardish' href='/context' href='/other'>context\n"
        "<section data-x='A&B'>\n"
        f"{MARKER}\n"
        "</section><footer>unchanged</footer>"
    )


class FakeNeotoma:
    """In-memory hub. `correct` stores the value unless `drop_writes` is set."""

    def __init__(self, body, drop_writes=False):
        self.body = body
        self.entity_type = "rendered_page"
        self.page_entity_type = "rendered_page"
        self.page_missing = False
        self.drop_writes = drop_writes
        self.conflict = False
        self.writes = []
        self.on_second_read = None
        self.after_write = None
        self.reads = 0
        self.snapshot_calls = []
        self.version = "version-1"

    def snapshot(self, base_url, token, entity_id):
        self.snapshot_calls.append((token, entity_id))
        if entity_id == NEW_ID:
            if self.page_missing:
                raise urllib.error.HTTPError(
                    f"{base_url}/entities/{entity_id}", 404, "Not Found", {}, None
                )
            return {"entity_type": self.page_entity_type}
        self.reads += 1
        if self.reads == 2 and self.on_second_read:
            self.body = self.on_second_read(self.body)
        return {
            "entity_type": self.entity_type,
            "html_body": self.body,
            "entity_version": self.version,
        }

    def request(self, url, token, payload=None):
        self.writes.append(payload)
        if self.conflict:
            raise urllib.error.HTTPError(url, 409, "Conflict", {}, None)
        if not self.drop_writes:
            self.body = payload["value"]
            if self.after_write:
                self.body = self.after_write(self.body)
            self.version = "version-2"
        return {"success": True, "entity_version": self.version}


def _args(**over):
    base = dict(
        hub=HUB_ID,
        page=NEW_ID,
        page_token="page-token",
        marker=MARKER_NAME,
        title="New",
        desc="D",
        badge_class="b-mark",
        badge_text="Recap",
        badge2_class="b-review",
        badge2_text="",
        apply=True,
        dry_run=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def fake(monkeypatch, hub):
    f = FakeNeotoma(hub)
    monkeypatch.setattr(H, "_fetch_snapshot", f.snapshot)
    monkeypatch.setattr(H, "_request", f.request)
    return f


def _href(token="page-token"):
    return f"{BASE}/entities/{NEW_ID}/html?access_token={token}"


def test_marker_missing_is_refused_without_write(fake, capsys):
    fake.body = fake.body.replace(MARKER, "")
    with pytest.raises(SystemExit) as exc:
        H.cmd_add(_args(), BASE, "bearer")
    assert exc.value.code == H.EXIT_ERROR
    assert fake.writes == []
    assert (
        f"marker {MARKER_NAME} must appear exactly once in the hub body "
        "(found 0); add it by hand" in capsys.readouterr().err
    )


def test_marker_present_twice_is_refused_without_write(fake, capsys):
    fake.body += MARKER
    with pytest.raises(SystemExit) as exc:
        H.cmd_add(_args(), BASE, "bearer")
    assert exc.value.code == H.EXIT_ERROR
    assert fake.writes == []
    assert "(found 2); add it by hand" in capsys.readouterr().err


def test_non_rendered_page_hub_is_refused_without_write(fake, capsys):
    fake.entity_type = "note"

    with pytest.raises(SystemExit) as exc:
        H.cmd_add(_args(), BASE, "bearer")

    assert exc.value.code == H.EXIT_ERROR
    assert fake.writes == []
    assert (
        f"hub {HUB_ID} is not a rendered_page; refusing" in capsys.readouterr().err
    )


def test_non_rendered_page_target_is_refused_without_write(
    fake, capsys, tmp_path
):
    fake.page_entity_type = "note"
    dry_run = tmp_path / "hub.html"

    with pytest.raises(SystemExit) as exc:
        H.cmd_add(
            _args(apply=False, dry_run=str(dry_run)),
            BASE,
            "bearer",
        )

    assert exc.value.code == H.EXIT_ERROR
    assert fake.writes == []
    assert not dry_run.exists()
    assert (
        f"page {NEW_ID} is not a readable rendered_page; refusing"
        in capsys.readouterr().err
    )


def test_missing_page_target_is_refused_without_write(fake, capsys, tmp_path):
    fake.page_missing = True
    dry_run = tmp_path / "hub.html"

    with pytest.raises(SystemExit) as exc:
        H.cmd_add(
            _args(apply=False, dry_run=str(dry_run)),
            BASE,
            "bearer",
        )

    assert exc.value.code == H.EXIT_ERROR
    assert fake.writes == []
    assert not dry_run.exists()
    assert (
        f"page {NEW_ID} is not a readable rendered_page; refusing"
        in capsys.readouterr().err
    )


def test_marker_present_once_produces_exact_splice_and_preserves_every_other_byte(
    fake, hub
):
    expected_card = H.build_card(_href(), "New", "D", [("b-mark", "Recap")])
    index = hub.index(MARKER)
    expected = hub[:index] + expected_card + hub[index:]

    assert H.cmd_add(_args(), BASE, "bearer") == H.EXIT_OK

    assert fake.body == expected
    assert fake.writes[0]["value"] == expected
    assert fake.writes[0]["expected_version"] == "version-1"
    assert fake.writes[0]["idempotency_key"].startswith(f"hub-add-card-{NEW_ID}-")
    assert fake.snapshot_calls[0] == ("bearer", NEW_ID)


def test_insert_card_is_the_exact_required_expression(hub):
    card = "\n<card>bytes</card>"
    index = hub.index(MARKER)
    assert H.insert_card(hub, MARKER_NAME, card) == hub[:index] + card + hub[index:]


def _assert_existing_reference_is_refused(fake, capsys):
    with pytest.raises(SystemExit) as exc:
        H.cmd_add(_args(), BASE, "bearer")

    assert exc.value.code == H.EXIT_ERROR
    assert fake.writes == []
    assert (
        f"page {NEW_ID} is already referenced in the hub; "
        "check or update that card by hand"
        in capsys.readouterr().err
    )


def test_correct_and_stale_links_are_refused_without_write(fake, capsys):
    stale = f"{BASE}/entities/{NEW_ID}/html?access_token=stale"
    cards = H.build_card(_href(), "Current", "D", []) + H.build_card(
        stale, "Stale", "D", []
    )
    fake.body = fake.body.replace(MARKER, cards + MARKER)

    _assert_existing_reference_is_refused(fake, capsys)


def test_exact_url_inside_html_comment_is_refused_without_write(fake, capsys):
    fake.body = fake.body.replace(MARKER, f"<!-- {_href()} -->\n{MARKER}")

    _assert_existing_reference_is_refused(fake, capsys)


@pytest.mark.parametrize("suffix", ["&x=1", "#frag"])
def test_exact_url_with_suffix_is_refused_without_write(fake, capsys, suffix):
    fake.body = fake.body.replace(
        MARKER,
        H.build_card(_href() + suffix, "Extended", "D", []) + MARKER,
    )

    _assert_existing_reference_is_refused(fake, capsys)


def test_entity_encoded_page_id_is_refused_without_write(fake, capsys):
    encoded_id = "&#101;nt_" + NEW_ID.removeprefix("ent_")
    fake.body = fake.body.replace(
        MARKER,
        f'<a href="{BASE}/entities/{encoded_id}/html">Encoded</a>{MARKER}',
    )

    _assert_existing_reference_is_refused(fake, capsys)


def test_percent_encoded_page_id_is_refused_without_write(fake, capsys):
    encoded_id = "%65%6E%74_" + NEW_ID.removeprefix("ent_")
    fake.body = fake.body.replace(
        MARKER,
        f'<a href="{BASE}/entities/{encoded_id}/html">Encoded</a>{MARKER}',
    )

    _assert_existing_reference_is_refused(fake, capsys)


def test_readback_one_byte_difference_returns_nonzero(fake, capsys):
    fake.after_write = lambda body: body + "x"

    assert H.cmd_add(_args(), BASE, "bearer") == H.EXIT_FINDINGS

    assert "write not confirmed in the stored body" in capsys.readouterr().out


def test_success_reports_stored_body_verified_but_not_served_page(fake, capsys):
    assert H.cmd_add(_args(), BASE, "bearer") == H.EXIT_OK
    assert (
        "stored hub body verified; the served page was not fetched by this tool, "
        "so open the new card's link as a guest before sharing"
        in capsys.readouterr().out
    )


def test_conflict_409_is_refused(fake, capsys, hub):
    fake.conflict = True
    with pytest.raises(SystemExit) as exc:
        H.cmd_add(_args(), BASE, "bearer")
    assert exc.value.code == H.EXIT_ERROR
    assert "hub changed, re-run" in capsys.readouterr().err
    assert fake.body == hub
    assert len(fake.writes) == 1
    assert fake.writes[0]["expected_version"] == "version-1"


def test_body_changed_between_read_and_write_is_refused(fake):
    fake.on_second_read = lambda body: body.replace("unchanged", "concurrent edit")
    with pytest.raises(SystemExit) as exc:
        H.cmd_add(_args(), BASE, "bearer")
    assert exc.value.code == H.EXIT_ERROR
    assert fake.writes == []


def test_build_card_escapes_all_text_and_attribute_fields():
    card = H.build_card(
        'https://x/entities/e/html?access_token=a"b',
        "</div><img src=x onerror=1> & co",
        "<b>d</b> 'quoted'",
        [("b-mark", '<i> & "badge"')],
    )
    assert "<img" not in card
    assert "<b>" not in card
    assert "<i>" not in card
    assert "&amp; co" in card
    assert "&lt;b&gt;d&lt;/b&gt; &#x27;quoted&#x27;" in card
    assert "&lt;i&gt; &amp; &quot;badge&quot;" in card
    assert "a&quot;b" in card


def test_add_escapes_special_title_and_description(fake):
    assert (
        H.cmd_add(
            _args(title="<script>x</script>", desc='A&B "quoted"'), BASE, "bearer"
        )
        == H.EXIT_OK
    )
    assert "<script>" not in fake.body
    assert "&lt;script&gt;x&lt;/script&gt;" in fake.body
    assert "A&amp;B &quot;quoted&quot;" in fake.body


def test_build_card_rejects_unsafe_badge_class():
    with pytest.raises(ValueError):
        H.build_card("u", "t", "d", [('b" onclick="x', "t")])


def test_unsafe_badge_class_is_refused_by_add(fake):
    with pytest.raises(SystemExit):
        H.cmd_add(_args(badge_class='b" onclick="x'), BASE, "bearer")
    assert fake.writes == []


def test_marker_name_is_validated(fake):
    for marker in ("UPPER", "a_b", "a.b", "", "a" * 65):
        with pytest.raises(SystemExit):
            H.cmd_add(_args(marker=marker), BASE, "bearer")
    assert fake.writes == []


def test_add_is_a_dry_run_without_apply(fake):
    assert H.cmd_add(_args(apply=False), BASE, "bearer") == H.EXIT_OK
    assert fake.writes == []


def test_dry_run_file_is_created_mode_0600(fake, tmp_path):
    path = tmp_path / "hub.html"
    assert H.cmd_add(_args(apply=False, dry_run=str(path)), BASE, "bearer") == H.EXIT_OK
    assert path.stat().st_mode & 0o777 == 0o600


def test_dry_run_file_restricts_preexisting_mode_to_0600(fake, tmp_path):
    path = tmp_path / "hub.html"
    path.write_text("old")
    path.chmod(0o644)
    assert H.cmd_add(_args(apply=False, dry_run=str(path)), BASE, "bearer") == H.EXIT_OK
    assert path.stat().st_mode & 0o777 == 0o600


def test_dry_run_file_refuses_symlink_without_touching_target(fake, tmp_path):
    target = tmp_path / "target.html"
    target.write_text("untouched")
    link = tmp_path / "hub.html"
    link.symlink_to(target)

    with pytest.raises(SystemExit) as exc:
        H.cmd_add(_args(apply=False, dry_run=str(link)), BASE, "bearer")

    assert exc.value.code == H.EXIT_ERROR
    assert target.read_text() == "untouched"


@pytest.mark.parametrize(
    "mode",
    ["success", "already-same", "already-different", "missing-marker", "conflict"],
)
def test_no_captured_output_contains_access_token_or_query_string(fake, capsys, mode):
    fixture_value = "fixture-token-value-0001"
    args = _args(page_token=fixture_value)
    if mode == "already-same":
        fake.body = fake.body.replace(
            MARKER, H.build_card(_href(fixture_value), "Old", "D", []) + MARKER
        )
    elif mode == "already-different":
        fake.body = fake.body.replace(
            MARKER, H.build_card(_href("other"), "Old", "D", []) + MARKER
        )
    elif mode == "missing-marker":
        fake.body = fake.body.replace(MARKER, "")
    elif mode == "conflict":
        fake.conflict = True

    try:
        H.cmd_add(args, BASE, "bearer")
    except SystemExit:
        pass

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert fixture_value not in combined
    assert "access_token" not in combined
    assert "?" not in combined


def test_silently_dropped_write_fails_exact_readback(fake):
    fake.drop_writes = True
    assert H.cmd_add(_args(), BASE, "bearer") == H.EXIT_FINDINGS


def test_idempotency_key_depends_on_exact_proposed_body(fake, hub):
    H.cmd_add(_args(title="One"), BASE, "bearer")
    fake.body = hub
    fake.reads = 0
    H.cmd_add(_args(title="Two"), BASE, "bearer")
    assert fake.writes[0]["idempotency_key"] != fake.writes[1]["idempotency_key"]


def test_fetch_snapshot_preserves_top_level_entity_version(monkeypatch):
    monkeypatch.setattr(
        H,
        "_request",
        lambda *args, **kwargs: {
            "entity_type": "rendered_page",
            "entity_version": "version-7",
            "snapshot": {"html_body": "<p>hub</p>"},
        },
    )
    assert H._fetch_snapshot(BASE, "bearer", HUB_ID) == {
        "entity_type": "rendered_page",
        "entity_version": "version-7",
        "html_body": "<p>hub</p>",
    }


def test_empty_page_token_is_refused(fake):
    with pytest.raises(SystemExit):
        H.cmd_add(_args(page_token="  "), BASE, "bearer")
    assert fake.writes == []


def test_bad_page_id_is_refused(fake):
    with pytest.raises(SystemExit):
        H.cmd_add(_args(page="ent_../x"), BASE, "bearer")
    assert fake.writes == []


def test_guest_token_flags_and_removed_section_flag_are_rejected(monkeypatch):
    argv = [
        "hub_reconcile.py",
        "--hub",
        HUB_ID,
        "add",
        "--page",
        NEW_ID,
        "--page-token",
        "secret",
        "--section",
        "Old heading",
        "--marker",
        MARKER_NAME,
        "--title",
        "T",
        "--desc",
        "D",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        H.main()
    assert exc.value.code == 2


def test_sections_subcommand_is_removed(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hub_reconcile.py", "--hub", HUB_ID, "sections"])
    with pytest.raises(SystemExit) as exc:
        H.main()
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("rejected_args", "rejected_value"),
    [
        (["--page-token", "synthetic-value-0002"], "synthetic-value-0002"),
        (["--page-token=synthetic-value-0003"], "synthetic-value-0003"),
        (["--unknown-option", "synthetic-value-0004"], "synthetic-value-0004"),
    ],
)
def test_argument_errors_do_not_echo_rejected_values(
    monkeypatch, capsys, rejected_args, rejected_value
):
    argv = [
        "hub_reconcile.py",
        "--hub",
        HUB_ID,
        "add",
        "--page",
        NEW_ID,
        "--marker",
        MARKER_NAME,
        "--title",
        "T",
        "--desc",
        "D",
        *rejected_args,
    ]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc:
        H.main()

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert captured.out == ""
    assert captured.err == "invalid arguments; run with --help\n"
    assert rejected_value not in captured.out
    assert rejected_value not in captured.err


def test_subparser_argument_errors_use_safe_message(monkeypatch, capsys):
    argv = [
        "hub_reconcile.py",
        "--hub",
        HUB_ID,
        "add",
        "--page",
        NEW_ID,
        "--marker",
        MARKER_NAME,
        "--title",
        "synthetic-value-0005",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc:
        H.main()

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert captured.out == ""
    assert captured.err == "invalid arguments; run with --help\n"
    assert "synthetic-value-0005" not in captured.err


# credentials -------------------------------------------------------------


def _clear_env(monkeypatch, home):
    for key in ("NEOTOMA_BASE_URL", "NEOTOMA_BEARER_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(H.Path, "home", classmethod(lambda cls: home))


def _profile(home, name, text):
    directory = home / ".config" / "neotoma"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.env").write_text(text)


def test_partial_env_pair_is_never_mixed_with_file(monkeypatch, tmp_path):
    _clear_env(monkeypatch, tmp_path)
    _profile(
        tmp_path,
        "neotoma",
        "NEOTOMA_BASE_URL=https://file.example\nNEOTOMA_BEARER_TOKEN=filetok\n",
    )
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://env.example")
    assert H._load_env("neotoma") == ("https://file.example", "filetok")


def test_file_with_only_half_a_pair_fails(monkeypatch, tmp_path):
    _clear_env(monkeypatch, tmp_path)
    _profile(tmp_path, "neotoma", "NEOTOMA_BEARER_TOKEN=filetok\n")
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://env.example")
    with pytest.raises(SystemExit) as exc:
        H._load_env("neotoma")
    assert exc.value.code == H.EXIT_ERROR


def test_profile_traversal_does_not_read_outside_config_dir(monkeypatch, tmp_path):
    _clear_env(monkeypatch, tmp_path)
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "evil.env").write_text(
        "NEOTOMA_BASE_URL=https://evil.example\nNEOTOMA_BEARER_TOKEN=x\n"
    )
    (tmp_path / ".config" / "neotoma").mkdir()
    with pytest.raises(SystemExit):
        H._load_env("../evil")


def test_env_pair_wins(monkeypatch, tmp_path):
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://env.example/")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "envtok")
    assert H._load_env("neotoma") == ("https://env.example", "envtok")


@pytest.mark.parametrize("profile", ["../x", "a/b", "", "a.b"])
def test_profile_name_is_validated(monkeypatch, tmp_path, profile):
    _clear_env(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        H._load_env(profile)


def test_http_base_url_refused_except_loopback(monkeypatch, tmp_path):
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "t")
    monkeypatch.setenv("NEOTOMA_BASE_URL", "http://remote.example")
    with pytest.raises(SystemExit):
        H._load_env("neotoma")
    monkeypatch.setenv("NEOTOMA_BASE_URL", "http://127.0.0.1:3080")
    assert H._load_env("neotoma")[0] == "http://127.0.0.1:3080"


def test_redirects_are_refused():
    handler = H._NoRedirectHandler()
    req = H.urllib.request.Request("https://a.example/x")
    assert (
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/")
        is None
    )
