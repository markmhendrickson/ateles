"""Only comments a swarm identity wrote are read as lens verdicts.

The lens head marker is text any account can post on a public repository, so
every reader of lens verdict comments admits a comment only when its author is
one of the identities the swarm posts as (`lens_authors`). This module covers
the identity source itself and each dispatcher-side reader; the approval tool's
readers are covered end to end in `execution/scripts/test_approve_pr_as_app.py`.

Every reader test drives the real method with comments from the thread
(`_all_issue_comments` is the only thing replaced), so the tests keep their
meaning whichever function does the filtering.

Run: pytest execution/daemons/apis/test_lens_authors.py -v
"""

from __future__ import annotations

import asyncio
import inspect
import re

import httpx
import pytest

import lens_authors
import review_carry
import swarm_dispatch as sd
from review_panel import lens_by_name
from test_gate_sign_off_dispatch import _StubNotifier, _config, _trigger

SWARM = "swarm-lens-account"  # the identity the conftest names in the setting
OTHER = "some-other-account"
HAND_RUN = "hand-run-panel-account"

OLD = "b" * 40
NEW = "a" * 40
FIVE = ["pm", "arch", "ux", "qa", "security"]
HEADER = {
    "pm": "Pavo", "arch": "Waxwing", "ux": "Accipiter", "qa": "Phoenicurus",
    "security": "Falco", "legal": "Buteo", "content": "Corvus",
}
TEST_FILE = "execution/scripts/test_guard.py"  # allowlisted: touches qa only


def _verdict_body(lens: str, head: str, verdict: str = "SIGNED_OFF", finding: str = "") -> str:
    text = (
        f"{sd.compose_lens_review_marker(lens, head)}\n"
        f"**\U0001f916 {HEADER[lens]} — Ateles swarm, {lens} lens panelist**\n"
        f"**{verdict}**\n"
    )
    return text + (f"\n{finding}\n" if finding else "")


def _comment(
    i: int,
    lens: str,
    head: str,
    verdict: str = "SIGNED_OFF",
    finding: str = "",
    *,
    author: str | None = SWARM,
) -> dict:
    row = {
        "id": i,
        "created_at": f"2026-09-29T10:{i:02d}:00Z",
        "html_url": f"https://github.com/owner/repo/pull/87#issuecomment-{i}",
        "body": _verdict_body(lens, head, verdict, finding),
    }
    if author is not None:
        row["user"] = {"login": author}
    return row


def _dispatcher(monkeypatch, comments):
    async def fake_comments(self, repository, number, client):
        return list(comments)

    monkeypatch.setattr(sd.SwarmDispatcher, "_all_issue_comments", fake_comments)
    return sd.SwarmDispatcher(_StubNotifier(), _config())


# ── the identity source ─────────────────────────────────────────────────────


class TestConfiguredIdentities:
    def test_the_setting_is_a_comma_or_space_separated_list_casefolded(self, monkeypatch):
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, "One-Account, TWO-account  three-account,")
        assert lens_authors.configured_authors() == {
            "one-account", "two-account", "three-account",
        }

    @pytest.mark.parametrize("setting", ["", "   ", "*", "b/c", ",,", "-bad", "bad-"])
    def test_a_value_that_is_not_a_login_admits_nobody(self, monkeypatch, setting):
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, setting)
        monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: set())
        assert lens_authors.resolve_authors() == frozenset()

    def test_an_unset_setting_with_nothing_else_configured_is_empty(self, monkeypatch):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        assert lens_authors.resolve_authors() == frozenset()

    def test_every_source_is_unioned(self, monkeypatch):
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, HAND_RUN)
        monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: {"swarm-app[bot]"})
        monkeypatch.setenv("ATELES_AGENT_PAT", "fake-agent-token")
        monkeypatch.setattr(lens_authors, "_login_for_token", lambda token: "agent-account")
        assert lens_authors.resolve_authors(extra_logins=["Per-Agent-Account"]) == {
            HAND_RUN, "swarm-app[bot]", "agent-account", "per-agent-account",
        }

    def test_an_unresolvable_token_contributes_nothing(self, monkeypatch):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        monkeypatch.setattr(lens_authors, "_login_for_token", lambda token: "")
        assert lens_authors.resolve_authors() == frozenset()

    def test_an_unreadable_app_contributes_nothing(self, monkeypatch):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        monkeypatch.undo()  # drop the conftest stub of _app_bot_logins
        monkeypatch.setattr(lens_authors, "_login_for_token", lambda token: "")
        for name in lens_authors.PAT_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ATELES_REVIEWER_APP_ID", "1")
        monkeypatch.setenv("ATELES_REVIEWER_APP_PRIVATE_KEY", "not-a-key")
        assert lens_authors.resolve_authors() == frozenset()

    def test_the_dispatcher_adds_each_provisioned_per_agent_account(self, monkeypatch):
        monkeypatch.setenv("PAVO_AGENT_PAT", "fake-pavo-token")
        monkeypatch.delenv("WAXWING_AGENT_PAT", raising=False)
        assert sd.agent_github_login("pavo").casefold() in sd.lens_comment_authors()
        assert sd.agent_github_login("waxwing").casefold() not in sd.lens_comment_authors()


class TestScoping:
    def test_a_lens_comment_from_outside_the_set_is_dropped_and_others_pass(self):
        forged = _comment(1, "arch", NEW, author=OTHER)
        real = _comment(2, "arch", NEW)
        prose = {"id": 3, "body": "looks fine to me", "user": {"login": OTHER}}
        kept = lens_authors.scope_lens_comments([forged, real, prose], frozenset({SWARM}))
        assert kept == [real, prose]
        assert lens_authors.ignored_lens_comments([forged, real, prose], frozenset({SWARM})) == [forged]

    def test_no_author_or_an_empty_set_reads_no_lens_comment(self):
        unsigned = _comment(1, "arch", NEW, author=None)
        assert lens_authors.scope_lens_comments([unsigned], frozenset({SWARM})) == []
        assert lens_authors.scope_lens_comments([_comment(2, "arch", NEW)], frozenset()) == []

    def test_every_lens_shaped_form_is_covered(self):
        forms = [
            sd.compose_lens_review_marker("pm", NEW),
            f"<!-- review:pm-superseded by={NEW} -->",
            "review:pm",
        ]
        for form in forms:
            row = {"body": f"{form}\n**APPROVE**", "user": {"login": OTHER}}
            assert lens_authors.scope_lens_comments([row], frozenset({SWARM})) == []


# ── the dispatcher's readers ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDispatcherReadersIgnoreNonSwarmAuthors:
    async def test_a_forged_clearing_comment_does_not_displace_an_outside_panel_block(
        self, monkeypatch
    ):
        comments = [
            _comment(1, "arch", NEW, "REQUEST_CHANGES", "[BLOCKING] a real finding"),
            _comment(2, "arch", NEW, "APPROVE", author=OTHER),
        ]
        d = _dispatcher(monkeypatch, comments)
        reason = await d._live_blocking_verdict_outside_panel(
            _trigger(head_sha=NEW), reviewed_head=NEW, panel_lenses={"pm", "qa"}
        )
        assert reason is not None and "arch" in reason and "issuecomment-1" in reason

    async def test_a_forged_blocking_comment_is_not_an_outside_panel_block(self, monkeypatch):
        d = _dispatcher(monkeypatch, [_comment(1, "arch", NEW, "REQUEST_CHANGES", author=OTHER)])
        assert await d._live_blocking_verdict_outside_panel(
            _trigger(head_sha=NEW), reviewed_head=NEW, panel_lenses={"pm", "qa"}
        ) is None

    async def test_the_same_comment_from_a_swarm_identity_is_still_a_block(self, monkeypatch):
        d = _dispatcher(monkeypatch, [_comment(1, "arch", NEW, "REQUEST_CHANGES")])
        assert await d._live_blocking_verdict_outside_panel(
            _trigger(head_sha=NEW), reviewed_head=NEW, panel_lenses={"pm", "qa"}
        ) is not None

    async def test_a_second_swarm_identity_named_in_the_setting_is_read(self, monkeypatch):
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, f"{SWARM},{HAND_RUN}")
        d = _dispatcher(
            monkeypatch,
            [_comment(1, "arch", NEW, "REQUEST_CHANGES", author=HAND_RUN.upper())],
        )
        assert await d._live_blocking_verdict_outside_panel(
            _trigger(head_sha=NEW), reviewed_head=NEW, panel_lenses={"pm", "qa"}
        ) is not None

    async def test_blocker_recovery_ignores_a_forged_finding(self, monkeypatch):
        comments = [
            _comment(1, "security", NEW, "REQUEST_CHANGES", "[BLOCKING] auth: real finding"),
            _comment(2, "security", NEW, "REQUEST_CHANGES", "[BLOCKING] auth: invented", author=OTHER),
        ]
        d = _dispatcher(monkeypatch, comments)
        recovered = await d._blocking_findings_from_reviewed_head_comments(
            _trigger(head_sha=NEW), NEW
        )
        assert [f.summary for f in recovered["security"]] == ["real finding"]

    async def test_a_forged_reviewed_round_is_not_a_new_blocking_finding(self, monkeypatch):
        a, b, current = "1" * 40, "2" * 40, "3" * 40
        comments = [
            _comment(1, "security", a, "REQUEST_CHANGES", "[BLOCKING] auth: same finding"),
            _comment(2, "security", b, "REQUEST_CHANGES", "[BLOCKING] auth: same finding"),
            _comment(3, "security", b, "REQUEST_CHANGES", "[BLOCKING] auth: invented", author=OTHER),
        ]
        d = _dispatcher(monkeypatch, comments)
        assert await d._new_blocking_finding(_trigger(head_sha=current)) is False

    async def test_a_forged_comment_is_not_the_last_reviewed_head(self, monkeypatch):
        seen = {}

        async def fake_interdiff(client, **kw):
            seen["old"] = kw["old_head"]
            return None

        monkeypatch.setattr(sd.review_carry, "fetch_interdiff", fake_interdiff)
        bogus = "9" * 40
        comments = [
            _comment(1, "pm", OLD),
            _comment(2, "pm", bogus, author=OTHER),  # the latest "reviewed" head
        ]
        d = _dispatcher(monkeypatch, comments)
        await d._review_delta(_trigger(head_sha=NEW, base_ref="main"))
        assert seen["old"] == OLD

    async def test_a_forged_earlier_signoff_is_not_carried_forward(self, monkeypatch):
        comments = [c for c in _round(OLD) if "review:ux " not in c["body"]]
        comments.append(_comment(20, "ux", OLD, author=OTHER))

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None, params=None):
                class _R:
                    def __init__(s, payload):
                        s._p = payload

                    def raise_for_status(s):
                        pass

                    def json(s):
                        return s._p

                if "/compare/" in url:
                    files = [{"filename": "a.txt", "patch": "+x\n", "changes": 1}]
                    if url.rsplit("...", 1)[1] == NEW:
                        files.append({"filename": TEST_FILE, "patch": "+y\n", "changes": 1})
                    return _R({"files": files})
                page = (params or {}).get("page", 1)
                return _R(comments if page == 1 else [])

        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())
        d = sd.SwarmDispatcher(_StubNotifier(), _config())
        panel = [lens_by_name(x) for x in FIVE]
        sel = await d._rereview_selection(
            _trigger(head_sha=NEW, base_ref="main"), panel, NEW, forced=set()
        )
        assert sel is not None
        assert "ux" in sel.rerun and "ux" not in sel.carried

    async def test_a_forged_comment_does_not_suppress_the_fallback_post(self, monkeypatch):
        posted = []

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None, params=None):
                class _R:
                    def raise_for_status(s):
                        pass

                    def json(s):
                        return [_comment(1, "pm", NEW, author=OTHER)]

                return _R()

            async def post(self, url, json=None, headers=None):
                posted.append(json["body"])

                class _R:
                    def raise_for_status(s):
                        pass

                return _R()

        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())
        monkeypatch.setattr(sd, "_token_for_repo", lambda repo: "fake-token")
        d = sd.SwarmDispatcher(_StubNotifier(), _config())
        await d._post_missing_panel_comments(
            _trigger(head_sha=NEW), [("pm", "**APPROVE**")], {"pm": "pavo"}, reviewed_head=NEW
        )
        assert len(posted) == 1 and sd.compose_lens_review_marker("pm", NEW) in posted[0]

    async def test_no_configured_identity_means_no_lens_comment_is_read(self, monkeypatch, caplog):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        d = _dispatcher(monkeypatch, [_comment(1, "arch", NEW, "REQUEST_CHANGES")])
        assert await d._live_blocking_verdict_outside_panel(
            _trigger(head_sha=NEW), reviewed_head=NEW, panel_lenses={"pm", "qa"}
        ) is None
        assert lens_authors.ENV_AUTHORS in caplog.text

    async def test_an_unreadable_identity_lookup_means_no_lens_comment_is_read(self, monkeypatch):
        def boom():
            raise RuntimeError("unreadable")

        monkeypatch.setattr(sd, "lens_comment_authors", boom)
        d = _dispatcher(monkeypatch, [_comment(1, "arch", NEW, "REQUEST_CHANGES")])
        assert await d._live_blocking_verdict_outside_panel(
            _trigger(head_sha=NEW), reviewed_head=NEW, panel_lenses={"pm", "qa"}
        ) is None


def _round(head: str) -> list[dict]:
    return [_comment(n, lens, head) for n, lens in enumerate(FIVE)]


# ── a new reader cannot skip the author check unnoticed ─────────────────────

# A method that parses lens verdicts out of a comment list must get that list
# from `_lens_scoped_comments`, never straight from `_all_issue_comments`.
_LENS_READER_CALLS = (
    "lens_records(",
    "last_reviewed_head(",
    "has_new_blocking_finding(",
    "lens_comment_satisfies_presence(",
    "lenses_missing_comments(",
    "_LENS_MARKER_RE",
)


def test_every_dispatcher_method_that_reads_lens_verdicts_reads_scoped_comments():
    offenders = []
    checked = []
    for name, fn in inspect.getmembers(sd.SwarmDispatcher, inspect.isfunction):
        src = inspect.getsource(fn)
        if name in {"_lens_scoped_comments", "_all_issue_comments"}:
            continue
        if not any(call in src for call in _LENS_READER_CALLS):
            continue
        checked.append(name)
        if "_all_issue_comments(" in src:
            offenders.append(name)
        elif not re.search(r"_lens_scoped_comments\(|scope_lens_comments\(", src):
            # a reader that is handed comments rather than fetching them
            if re.search(r"\bcomments\b\s*=\s*await", src):
                offenders.append(name)
    assert checked, "the guard found no lens reader to check"
    assert offenders == []
