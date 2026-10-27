"""Tests for post_lens_verdict.py — the cross-repo lens verdict poster.

Every GitHub call and every App mint is faked. The posted comment is checked by
the SAME readers the approval tool uses (`swarm_dispatch.lens_own_verdict`,
`sign_off_is_warranted`, `lens_authors.is_swarm_comment`), so a change to what
the gate reads breaks these.

Run: pytest execution/scripts/test_post_lens_verdict.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent
_DAEMON_DIR = _SCRIPTS.parents[0] / "daemons" / "apis"
_REPO_ROOT = _SCRIPTS.parents[1]
for _p in (str(_REPO_ROOT), str(_SCRIPTS), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import lens_authors  # noqa: E402
import post_lens_verdict as target  # noqa: E402
import swarm_dispatch  # noqa: E402
from lib import github_app_token  # noqa: E402

REPO = "owner/private-app"
PR = 9
HEAD = "a" * 40
OTHER_HEAD = "c" * 40
APP_LOGIN = "swarm-app[bot]"
APP_TOKEN = "ghs_fake_installation_token_value"
PAT_VALUE = "ghp_fake_pat_value_for_tests"

GOOD_BODY = "**\U0001f916 Waxwing — Ateles swarm, arch review**\n**SIGNED_OFF**\n\nNo findings.\n"


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Client:
    def __init__(self, *, head=HEAD, state="open", merged_at=None, comments=None,
                 pr_status=200, post_status=201, readback_body=None, pat_login="agent-pat-user",
                 author_login=APP_LOGIN):
        self.author_login = author_login
        self.head, self.state, self.merged_at = head, state, merged_at
        self.comments = list(comments or [])
        self.pr_status, self.post_status = pr_status, post_status
        self.readback_body = readback_body
        self.pat_login = pat_login
        self.writes: list[tuple[str, str, dict]] = []
        self.auth_seen: list[str] = []
        self._last: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _note(self, headers):
        self.auth_seen.append((headers or {}).get("Authorization", ""))

    def get(self, url, headers=None, params=None):
        self._note(headers)
        if url.endswith("/user"):
            return _Resp({"login": self.pat_login})
        if url.endswith(f"/pulls/{PR}"):
            if self.pr_status >= 400:
                return _Resp({"message": "Not Found"}, self.pr_status)
            return _Resp({"state": self.state, "merged_at": self.merged_at, "head": {"sha": self.head}})
        if url.endswith(f"/issues/{PR}/comments"):
            return _Resp(self.comments if (params or {}).get("page", 1) == 1 else [])
        if "/issues/comments/" in url:
            row = dict(self._last)
            if self.readback_body is not None:
                row["body"] = self.readback_body
            return _Resp(row)
        raise AssertionError(f"unexpected GET {url}")

    def _write(self, method, url, headers, json):
        self._note(headers)
        self.writes.append((method, url, json))
        self._last = {
            "id": 555, "body": json["body"], "html_url": f"https://github.com/{REPO}/pull/{PR}#issuecomment-555",
            "user": {"login": self.author_login},
        }
        return _Resp(self._last, self.post_status)

    def post(self, url, headers=None, json=None):
        return self._write("POST", url, headers, json)

    def patch(self, url, headers=None, json=None):
        return self._write("PATCH", url, headers, json)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith(("ATELES_APP_", "ATELES_REVIEWER_APP", "ATELES_EXPECTED_LENS_APPS")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(github_app_token, "installation_token", lambda repo=None, *, prefix="": APP_TOKEN)
    monkeypatch.setattr(github_app_token, "bot_login", lambda prefix="": APP_LOGIN)
    monkeypatch.setattr(swarm_dispatch, "lens_comment_authors", lambda: frozenset({APP_LOGIN}))


def _run(monkeypatch, tmp_path, client, *, body=GOOD_BODY, lens="arch", extra=(), capsys=None):
    f = tmp_path / "body.md"
    f.write_text(body, encoding="utf-8")
    monkeypatch.setattr(target, "_new_client", lambda: client)
    argv = ["--repo", REPO, "--pr", str(PR), "--lens", lens, "--body-file", str(f), *extra]
    return target.main(argv)


def test_posts_the_marker_and_the_body_as_the_app(monkeypatch, tmp_path, capsys):
    client = _Client()
    assert _run(monkeypatch, tmp_path, client) == 0
    assert len(client.writes) == 1
    method, url, payload = client.writes[0]
    assert method == "POST" and url.endswith(f"/repos/{REPO}/issues/{PR}/comments")
    body = payload["body"]
    assert body.startswith(swarm_dispatch.compose_lens_review_marker("arch", HEAD) + "\n")
    # exactly what the approval tool's own reader clears on
    assert swarm_dispatch.sign_off_is_warranted(body, lens_agent="waxwing")
    probe = {"user": {"login": APP_LOGIN}, "body": body}
    assert lens_authors.is_swarm_comment(probe, frozenset({APP_LOGIN}))
    out = capsys.readouterr().out
    assert "posted:" in out and APP_LOGIN in out


def test_no_token_is_ever_printed_or_posted_in_the_body(monkeypatch, tmp_path, capsys):
    client = _Client()
    assert _run(monkeypatch, tmp_path, client) == 0
    captured = capsys.readouterr()
    assert APP_TOKEN not in captured.out + captured.err
    assert APP_TOKEN not in client.writes[0][2]["body"]
    assert client.auth_seen and all(a == f"Bearer {APP_TOKEN}" for a in client.auth_seen)


def test_the_app_is_the_default_identity_and_never_calls_user(monkeypatch, tmp_path):
    client = _Client()
    seen = []
    orig = client.get

    def spy(url, headers=None, params=None):
        seen.append(url)
        return orig(url, headers=headers, params=params)

    client.get = spy
    assert _run(monkeypatch, tmp_path, client) == 0
    assert not any(u.endswith("/user") for u in seen)


def test_a_head_that_differs_from_the_pr_head_is_refused(monkeypatch, tmp_path, capsys):
    client = _Client(head=OTHER_HEAD)
    assert _run(monkeypatch, tmp_path, client, extra=["--head", HEAD]) == 1
    assert client.writes == []
    assert "not the" in capsys.readouterr().out


def test_a_matching_head_is_accepted(monkeypatch, tmp_path):
    client = _Client()
    assert _run(monkeypatch, tmp_path, client, extra=["--head", HEAD.upper()]) == 0


def test_a_malformed_head_is_refused(monkeypatch, tmp_path):
    client = _Client()
    assert _run(monkeypatch, tmp_path, client, extra=["--head", "abc123"]) == 1
    assert client.writes == []


@pytest.mark.parametrize(
    "word", ["BLOCKED", "REQUEST_CHANGES", "**blocked**", "[BLOCKING] a finding"],
)
def test_a_sign_off_carrying_a_blocking_token_is_refused(monkeypatch, tmp_path, capsys, word):
    body = GOOD_BODY + f"\nThe earlier round said {word} about this.\n"
    client = _Client()
    assert _run(monkeypatch, tmp_path, client, body=body) == 1
    assert client.writes == []
    assert "read it as blocking" in capsys.readouterr().out


@pytest.mark.parametrize("word", ["BLOCKING", "request_changes", "blocked"])
def test_looser_blocking_wording_posts_with_a_warning(monkeypatch, tmp_path, capsys, word):
    body = GOOD_BODY + f"\nThe earlier round said {word} about this.\n"
    client = _Client()
    assert _run(monkeypatch, tmp_path, client, body=body) == 0
    assert len(client.writes) == 1
    assert "warning:" in capsys.readouterr().out


def test_a_clean_sign_off_draws_no_warning(monkeypatch, tmp_path, capsys):
    assert _run(monkeypatch, tmp_path, _Client()) == 0
    assert "warning:" not in capsys.readouterr().out


def test_a_real_block_is_postable(monkeypatch, tmp_path):
    body = (
        "**\U0001f916 Waxwing — Ateles swarm, arch review**\n**BLOCKED**\n\n"
        "[BLOCKING] the contract is broken.\n"
    )
    client = _Client()
    assert _run(monkeypatch, tmp_path, client, body=body) == 0
    assert len(client.writes) == 1


def test_a_body_without_a_readable_verdict_is_refused(monkeypatch, tmp_path, capsys):
    client = _Client()
    assert _run(monkeypatch, tmp_path, client, body="Looks fine to me.\n") == 1
    assert client.writes == []
    assert "fixed position" in capsys.readouterr().out


def test_a_header_naming_another_agent_is_refused(monkeypatch, tmp_path):
    body = GOOD_BODY.replace("Waxwing", "Falco")
    assert _run(monkeypatch, tmp_path, _Client(), body=body) == 1


def test_a_body_opening_with_the_right_marker_is_accepted_once(monkeypatch, tmp_path):
    marker = swarm_dispatch.compose_lens_review_marker("arch", HEAD)
    client = _Client()
    assert _run(monkeypatch, tmp_path, client, body=marker + "\n" + GOOD_BODY) == 0
    assert client.writes[0][2]["body"].count("review:arch") == 1


def test_a_body_opening_with_a_marker_for_another_head_or_lens_is_refused(monkeypatch, tmp_path):
    for marker in (
        swarm_dispatch.compose_lens_review_marker("arch", OTHER_HEAD),
        swarm_dispatch.compose_lens_review_marker("security", HEAD),
    ):
        client = _Client()
        assert _run(monkeypatch, tmp_path, client, body=marker + "\n" + GOOD_BODY) == 1
        assert client.writes == []


def test_an_identity_the_approval_tool_would_not_read_is_refused(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(swarm_dispatch, "lens_comment_authors", lambda: frozenset({"somebody-else"}))
    client = _Client()
    assert _run(monkeypatch, tmp_path, client) == 1
    assert client.writes == []
    assert "would ignore the comment" in capsys.readouterr().out


def test_a_shared_identity_is_refused_for_a_lens_that_has_its_own_app(monkeypatch, tmp_path):
    scope = lens_authors.LensAppScope(lens_logins=(("arch", "arch-app[bot]"),))
    authors = lens_authors.ScopedAuthors({APP_LOGIN, "arch-app[bot]"}, scope)
    monkeypatch.setattr(swarm_dispatch, "lens_comment_authors", lambda: authors)
    client = _Client()
    assert _run(monkeypatch, tmp_path, client) == 1
    assert client.writes == []


def test_a_second_post_for_the_same_head_edits_in_place(monkeypatch, tmp_path, capsys):
    marker = swarm_dispatch.compose_lens_review_marker("arch", HEAD)
    existing = {"id": 41, "body": marker + "\nold\n", "user": {"login": APP_LOGIN}}
    client = _Client(comments=[existing])
    assert _run(monkeypatch, tmp_path, client) == 0
    method, url, _ = client.writes[0]
    assert method == "PATCH" and url.endswith("/issues/comments/41")
    assert "edited:" in capsys.readouterr().out


def test_two_existing_matches_are_refused(monkeypatch, tmp_path):
    marker = swarm_dispatch.compose_lens_review_marker("arch", HEAD)
    rows = [{"id": i, "body": marker + "\nold\n", "user": {"login": APP_LOGIN}} for i in (41, 42)]
    client = _Client(comments=rows)
    assert _run(monkeypatch, tmp_path, client) == 1
    assert client.writes == []


def test_another_accounts_comment_is_not_edited(monkeypatch, tmp_path):
    marker = swarm_dispatch.compose_lens_review_marker("arch", HEAD)
    rows = [{"id": 41, "body": marker + "\nold\n", "user": {"login": "someone-else"}}]
    client = _Client(comments=rows)
    assert _run(monkeypatch, tmp_path, client) == 0
    assert client.writes[0][0] == "POST"


@pytest.mark.parametrize(
    "kwargs", [{"state": "closed"}, {"merged_at": "2026-10-27T00:00:00Z"}]
)
def test_a_closed_or_merged_pr_is_refused(monkeypatch, tmp_path, kwargs):
    client = _Client(**kwargs)
    assert _run(monkeypatch, tmp_path, client) == 1
    assert client.writes == []


def test_an_unreadable_pr_names_the_app_remedy(monkeypatch, tmp_path, capsys):
    assert _run(monkeypatch, tmp_path, _Client(pr_status=404)) == 1
    assert "swarm App" in capsys.readouterr().out


def test_a_readback_that_does_not_match_fails(monkeypatch, tmp_path, capsys):
    client = _Client(readback_body="something else")
    assert _run(monkeypatch, tmp_path, client) == 1
    assert "does not match" in capsys.readouterr().out


def test_a_failed_write_is_reported(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, _Client(post_status=403)) == 1


def test_dry_run_checks_everything_and_posts_nothing(monkeypatch, tmp_path, capsys):
    client = _Client()
    assert _run(monkeypatch, tmp_path, client, extra=["--dry-run"]) == 0
    assert client.writes == []
    assert "would create" in capsys.readouterr().out


def test_a_failed_app_mint_is_a_refusal(monkeypatch, tmp_path, capsys):
    def boom(repo=None, *, prefix=""):
        raise github_app_token.AppTokenError("GitHub App not configured")

    monkeypatch.setattr(github_app_token, "installation_token", boom)
    client = _Client()
    assert _run(monkeypatch, tmp_path, client) == 1
    assert client.writes == []
    assert "installation token" in capsys.readouterr().out


def test_a_declared_but_unusable_lens_app_is_not_replaced_by_the_shared_app(monkeypatch, tmp_path):
    monkeypatch.setenv("ATELES_APP_REVIEWER_ARCH_ID", "123")  # id with no key
    client = _Client()
    assert _run(monkeypatch, tmp_path, client) == 1
    assert client.writes == []


def test_an_expected_lens_app_missing_here_is_not_replaced_by_the_shared_app(monkeypatch, tmp_path):
    monkeypatch.setenv("ATELES_EXPECTED_LENS_APPS", "arch")
    assert _run(monkeypatch, tmp_path, _Client()) == 1


def test_the_pat_path_posts_as_the_account_the_token_resolves_to(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_PAT", PAT_VALUE)
    monkeypatch.setattr(swarm_dispatch, "lens_comment_authors", lambda: frozenset({"agent-pat-user"}))
    client = _Client(author_login="agent-pat-user")
    assert _run(monkeypatch, tmp_path, client, extra=["--token-env", "MY_PAT"]) == 0
    assert client.auth_seen[0] == f"Bearer {PAT_VALUE}"
    assert PAT_VALUE not in client.writes[0][2]["body"]


def test_an_unset_pat_env_is_refused_without_printing_it(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("MY_PAT", raising=False)
    assert _run(monkeypatch, tmp_path, _Client(), extra=["--token-env", "MY_PAT"]) == 1
    assert "unset or empty" in capsys.readouterr().out


def test_as_app_and_token_env_together_are_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_PAT", PAT_VALUE)
    client = _Client()
    assert _run(monkeypatch, tmp_path, client, extra=["--as-app", "--token-env", "MY_PAT"]) == 1
    assert client.writes == []


@pytest.mark.parametrize("bad", ["notarepo", "a/b/c", "a b/c"])
def test_a_malformed_repo_is_refused(monkeypatch, tmp_path, bad):
    f = tmp_path / "b.md"
    f.write_text(GOOD_BODY)
    monkeypatch.setattr(target, "_new_client", lambda: _Client())
    assert target.main(["--repo", bad, "--pr", "1", "--lens", "arch", "--body-file", str(f)]) == 1


def test_an_unknown_lens_is_refused(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, _Client(), lens="nobody") == 1


def test_an_unreadable_body_file_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(target, "_new_client", lambda: _Client())
    argv = ["--repo", REPO, "--pr", "1", "--lens", "arch", "--body-file", str(tmp_path / "missing.md")]
    assert target.main(argv) == 1
