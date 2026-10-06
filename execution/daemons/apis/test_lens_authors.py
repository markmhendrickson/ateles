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

import ast
import asyncio
import json
from pathlib import Path

import httpx
import pytest

import lens_authors
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


class _Notifier(_StubNotifier):
    def clear_dedupe(self, key):
        pass


def _dispatcher(monkeypatch, comments):
    async def fake_comments(self, repository, number, client):
        return list(comments)

    monkeypatch.setattr(sd.SwarmDispatcher, "_all_issue_comments", fake_comments)
    return sd.SwarmDispatcher(_Notifier(), _config())


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
        monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: (set(), False))
        assert lens_authors.resolve_authors() == frozenset()

    def test_an_unset_setting_with_nothing_else_configured_is_empty(self, monkeypatch):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        assert lens_authors.resolve_authors() == frozenset()

    def test_every_source_is_unioned(self, monkeypatch):
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, HAND_RUN)
        monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: ({"swarm-app[bot]"}, False))
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

    def test_a_lens_agents_own_token_is_resolved_not_guessed(self, monkeypatch):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        monkeypatch.setenv("PAVO_AGENT_PAT", "fake-pavo-token")
        monkeypatch.setattr(
            lens_authors, "_login_for_token", lambda token: "Pavo-Resolved-Account"
        )
        resolution = sd.lens_comment_authors_resolution()
        assert resolution.authors == {"pavo-resolved-account"}
        assert resolution.failed == ()
        # the login the agent's NAME would derive is never admitted on its own
        assert sd.agent_github_login("pavo").casefold() not in resolution.authors

    def test_a_lens_agent_token_that_does_not_resolve_admits_nobody(self, monkeypatch):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        monkeypatch.setenv("PAVO_AGENT_PAT", "fake-pavo-token")
        monkeypatch.setattr(lens_authors, "_login_for_token", lambda token: "")
        resolution = sd.lens_comment_authors_resolution()
        assert resolution.authors == frozenset()
        assert resolution.failed == ("the account behind PAVO_AGENT_PAT",)
        assert sd.agent_github_login("pavo").casefold() not in resolution.authors

    def test_a_configured_app_that_cannot_be_read_is_a_failed_source(self, monkeypatch):
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, SWARM)
        monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: (set(), True))
        resolution = lens_authors.resolve()
        assert resolution.authors == {SWARM}
        assert resolution.failed == ("the swarm App's bot login",)
        assert not resolution.complete

    def test_a_complete_resolution_needs_an_identity_and_no_failed_source(self):
        assert lens_authors.Resolution(frozenset({SWARM})).complete
        assert not lens_authors.Resolution(frozenset()).complete
        assert not lens_authors.Resolution(frozenset({SWARM}), ("x",)).complete


class _FakeUrlopen:
    """Stand-in for urllib's urlopen that serves a login or fails, and counts calls."""

    def __init__(self, login: str | None):
        self.login = login
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        if self.login is None:
            raise OSError("HTTP 429")

        class _R:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s):
                return json.dumps({"login": self_login}).encode()

        self_login = self.login
        return _R()


class TestTokenLookupCaching:
    def _clock(self, monkeypatch):
        now = {"t": 1000.0}
        monkeypatch.setattr(lens_authors.time, "monotonic", lambda: now["t"])
        return now

    def test_a_failed_lookup_is_retried_within_a_minute(self, monkeypatch):
        monkeypatch.undo()
        lens_authors.clear_cache()
        clock = self._clock(monkeypatch)
        fake = _FakeUrlopen(None)
        monkeypatch.setattr(lens_authors.urllib.request, "urlopen", fake)
        assert lens_authors._login_for_token("tok") == ""
        assert fake.calls == 1
        clock["t"] += 10  # still inside the failure window: not asked again
        assert lens_authors._login_for_token("tok") == ""
        assert fake.calls == 1
        clock["t"] += lens_authors._LIVE_LOGIN_FAILURE_TTL_S  # window over
        fake.login = "recovered-account"
        assert lens_authors._login_for_token("tok") == "recovered-account"
        assert fake.calls == 2

    def test_the_failure_window_is_at_most_a_minute(self):
        assert 0 < lens_authors._LIVE_LOGIN_FAILURE_TTL_S <= 60

    def test_a_successful_lookup_is_kept_for_the_long_window(self, monkeypatch):
        monkeypatch.undo()
        lens_authors.clear_cache()
        clock = self._clock(monkeypatch)
        fake = _FakeUrlopen("agent-account")
        monkeypatch.setattr(lens_authors.urllib.request, "urlopen", fake)
        assert lens_authors._login_for_token("tok") == "agent-account"
        clock["t"] += lens_authors._LIVE_LOGIN_FAILURE_TTL_S + 5
        assert lens_authors._login_for_token("tok") == "agent-account"
        assert fake.calls == 1
        clock["t"] += lens_authors._LIVE_LOGIN_TTL_S
        lens_authors._login_for_token("tok")
        assert fake.calls == 2


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

    async def test_an_objection_from_a_non_admitted_account_holds_the_merge(self, monkeypatch):
        d = _dispatcher(monkeypatch, [_comment(1, "arch", NEW, "REQUEST_CHANGES", author=OTHER)])
        reason = await d._live_blocking_verdict_outside_panel(
            _trigger(head_sha=NEW), reviewed_head=NEW, panel_lenses={"pm", "qa"}
        )
        # it can only delay a merge: held, naming the account and the setting
        assert reason is not None
        assert OTHER in reason and lens_authors.ENV_AUTHORS in reason and "issuecomment-1" in reason

    async def test_a_clearing_comment_from_a_non_admitted_account_never_clears(
        self, monkeypatch
    ):
        d = _dispatcher(monkeypatch, [_comment(1, "arch", NEW, "REQUEST_CHANGES")])
        d2 = _dispatcher(
            monkeypatch,
            [
                _comment(1, "arch", NEW, "REQUEST_CHANGES"),
                _comment(2, "arch", NEW, "APPROVE", author=OTHER),
            ],
        )
        for dispatcher in (d, d2):
            assert await dispatcher._live_blocking_verdict_outside_panel(
                _trigger(head_sha=NEW), reviewed_head=NEW, panel_lenses={"pm", "qa"}
            ) is not None

    async def test_a_non_admitted_unreadable_comment_does_not_hold(self, monkeypatch):
        junk = _comment(1, "arch", NEW, author=OTHER)
        junk["body"] = sd.compose_lens_review_marker("arch", NEW) + "\nhello"
        d = _dispatcher(monkeypatch, [junk])
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

    async def test_no_configured_identity_holds_the_merge(self, monkeypatch):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        d = _dispatcher(monkeypatch, [])  # not even a comment: the guard cannot see any
        reason = await d._live_blocking_verdict_outside_panel(
            _trigger(head_sha=NEW), reviewed_head=NEW, panel_lenses={"pm", "qa"}
        )
        assert reason is not None and lens_authors.ENV_AUTHORS in reason

    async def test_an_unreadable_identity_lookup_holds_the_merge(self, monkeypatch):
        def boom():
            raise RuntimeError("unreadable")

        monkeypatch.setattr(sd, "lens_comment_authors_resolution", boom)
        d = _dispatcher(monkeypatch, [])
        assert await d._live_blocking_verdict_outside_panel(
            _trigger(head_sha=NEW), reviewed_head=NEW, panel_lenses={"pm", "qa"}
        ) is not None

    async def test_an_identity_source_that_failed_holds_the_merge(self, monkeypatch):
        monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: (set(), True))
        d = _dispatcher(monkeypatch, [])
        reason = await d._live_blocking_verdict_outside_panel(
            _trigger(head_sha=NEW), reviewed_head=NEW, panel_lenses={"pm", "qa"}
        )
        assert reason is not None and "App" in reason

    async def test_the_other_readers_read_nothing_without_an_identity(self, monkeypatch, caplog):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        d = _dispatcher(monkeypatch, [_comment(1, "arch", NEW, "REQUEST_CHANGES")])
        recovered = await d._blocking_findings_from_reviewed_head_comments(
            _trigger(head_sha=NEW), NEW
        )
        assert recovered == {}
        assert lens_authors.ENV_AUTHORS in caplog.text


def _round(head: str) -> list[dict]:
    return [_comment(n, lens, head) for n, lens in enumerate(FIVE)]


# ── the dispatcher's merge path, end to end ─────────────────────────────────
#
# The real `_gate_merge_readiness` with auto-merge on, a verified approval
# receipt, green CI, and ONE real objection from a swarm account on a lens that
# is not in the panel. Only the network edges are replaced.


def _gate_harness(monkeypatch, comments):
    merged: list = []
    head = NEW

    async def fake_head(self, trigger):
        return head

    async def fake_ci(self, trigger):
        return "green"

    async def fake_merge(self, *args, **kwargs):
        merged.append((args, kwargs))
        return True, "f" * 40

    monkeypatch.setattr(sd.SwarmDispatcher, "_pr_head_sha", fake_head)
    monkeypatch.setattr(sd.SwarmDispatcher, "_required_ci_state", fake_ci)
    monkeypatch.setattr(sd.SwarmDispatcher, "_merge_pr", fake_merge)
    dispatcher = _dispatcher(monkeypatch, comments)
    dispatcher.config.auto_merge = True
    receipt = sd.ReviewBindingReceipt(
        review_id="200",
        reviewer_login=sd.agent_github_login("vanellus"),
        commit_id=head,
        state="APPROVED",
    )
    panel = [
        sd.Lens(agent="pavo", lens="pm", gate="pm", checks="", always=True),
        sd.Lens(agent="phoenicurus", lens="qa", gate="qa", checks="", always=True),
    ]
    asyncio.run(
        dispatcher._gate_merge_readiness(
            _trigger(head_sha=head),
            parent=80,
            panel=panel,
            reviewed_head=head,
            binding_receipt=receipt,
        )
    )
    return merged, dispatcher


def _real_objection():
    return [_comment(9, "arch", NEW, "REQUEST_CHANGES", "[BLOCKING] a real finding")]


class TestAutoMergeHoldsWhenTheGuardCannotSeeObjections:
    def test_with_identities_resolved_a_real_objection_holds(self, monkeypatch):
        merged, d = _gate_harness(monkeypatch, _real_objection())
        assert merged == []
        assert any("auto-merge HELD" in m for m in d.notifier.sent)

    def test_with_no_setting_an_unreadable_app_and_a_rate_limited_lookup_it_still_holds(
        self, monkeypatch
    ):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: (set(), True))
        monkeypatch.setenv("ATELES_AGENT_PAT", "fake-agent-token")
        monkeypatch.setattr(lens_authors, "_login_for_token", lambda token: "")  # HTTP 429
        merged, d = _gate_harness(monkeypatch, _real_objection())
        assert merged == []
        assert any("auto-merge HELD" in m for m in d.notifier.sent)

    def test_a_non_empty_set_that_omits_the_poster_still_holds(self, monkeypatch):
        monkeypatch.setenv(lens_authors.ENV_AUTHORS, "an-unrelated-account")
        merged, d = _gate_harness(monkeypatch, _real_objection())
        assert merged == []
        sent = " ".join(d.notifier.sent)
        assert "auto-merge HELD" in sent and SWARM in sent and lens_authors.ENV_AUTHORS in sent

    def test_a_partial_resolution_holds_even_without_an_objection(self, monkeypatch):
        monkeypatch.setattr(lens_authors, "_app_bot_logins", lambda: (set(), True))
        merged, _ = _gate_harness(monkeypatch, [])
        assert merged == []

    def test_a_clean_thread_with_resolved_identities_still_merges(self, monkeypatch):
        merged, _ = _gate_harness(monkeypatch, [])
        assert len(merged) == 1

    def test_a_clearing_verdict_from_a_non_admitted_account_does_not_release_the_hold(
        self, monkeypatch
    ):
        comments = _real_objection() + [_comment(10, "arch", NEW, "APPROVE", author=OTHER)]
        merged, _ = _gate_harness(monkeypatch, comments)
        assert merged == []


# ── the aggregation comment is author-scoped like a lens comment ────────────


def _agg(i: int, verdict: str, head: str = NEW, *, author: str = SWARM) -> dict:
    return {
        "id": i,
        "created_at": f"2026-09-29T10:{i:02d}:00Z",
        "html_url": f"https://github.com/owner/repo/pull/87#issuecomment-{i}",
        "user": {"login": author},
        "body": sd.compose_vanellus_fallback_comment(f"**{verdict}**", head),
    }


@pytest.mark.asyncio
class TestAggregationReadersIgnoreNonSwarmAuthors:
    async def test_a_newer_aggregation_from_another_account_does_not_clear_the_merge_gate(
        self, monkeypatch
    ):
        d = _dispatcher(
            monkeypatch,
            [_agg(1, "REQUEST_CHANGES"), _agg(2, "APPROVE", author=OTHER)],
        )
        assert await d._pr_review_is_clear("owner/repo", 87, NEW) is False

    async def test_the_swarms_own_newer_aggregation_still_clears_it(self, monkeypatch):
        d = _dispatcher(monkeypatch, [_agg(1, "REQUEST_CHANGES"), _agg(2, "APPROVE")])
        assert await d._pr_review_is_clear("owner/repo", 87, NEW) is True

    async def test_the_verdict_recovery_does_not_recover_a_non_swarm_aggregation(
        self, monkeypatch
    ):
        async def fake_head(self, trigger):
            return NEW

        monkeypatch.setattr(sd.SwarmDispatcher, "_pr_head_sha", fake_head)
        real = _agg(1, "REQUEST_CHANGES")
        d = _dispatcher(monkeypatch, [real, _agg(2, "APPROVE", author=OTHER)])
        verdict, fired = await d._resolve_review_verdict(
            _trigger(head_sha=NEW), "no verdict token in stdout"
        )
        assert (verdict, fired) == ("request_changes", True)
        d_only = _dispatcher(monkeypatch, [_agg(2, "APPROVE", author=OTHER)])
        assert await d_only._resolve_review_verdict(
            _trigger(head_sha=NEW), "no verdict token in stdout"
        ) == (None, False)

    async def test_no_identity_means_no_aggregation_is_read(self, monkeypatch):
        monkeypatch.delenv(lens_authors.ENV_AUTHORS, raising=False)
        d = _dispatcher(monkeypatch, [_agg(1, "APPROVE")])
        assert await d._pr_review_is_clear("owner/repo", 87, NEW) is False

    @pytest.mark.parametrize("author, expected", [(SWARM, 1), (OTHER, 0)])
    async def test_the_missing_lens_sweep_reads_only_a_swarm_aggregation(
        self, monkeypatch, author, expected
    ):
        # the neotoma#2153 shape: a clear aggregation that withholds for a lens
        body = (
            "<!-- vanellus-aggregation -->\n**COMMENT**\n\n"
            "- **security:** **NOT RECEIVED** — no verdict was found.\n\n"
            "**Blocking: 0** confirmed findings. Merge is withheld because the "
            "security lens verdict is missing for this round.\n"
        )

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
                        if url.endswith("/pulls"):
                            return [{"number": 87}]
                        return [
                            {
                                "id": 1,
                                "created_at": "2026-09-29T10:00:00Z",
                                "user": {"login": author},
                                "body": body,
                            }
                        ]

                return _R()

        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())
        d = sd.SwarmDispatcher(_StubNotifier(), _config())
        assert len(await d._prs_with_missing_lens("owner/repo")) == expected

    async def test_a_non_swarm_aggregation_does_not_suppress_the_fallback_post(
        self, monkeypatch
    ):
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
                        return [_agg(1, "APPROVE", author=OTHER)]

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
        result = sd.SkillResult("vanellus", True, 0, "**REQUEST_CHANGES**\nfinding", "")
        await d._post_missing_vanellus_comment(_trigger(head_sha=NEW), result, reviewed_head=NEW)
        assert len(posted) == 1

    async def test_a_deferral_marker_from_another_account_is_not_a_live_deferral(
        self, monkeypatch
    ):
        iso = "2099-01-01T00:00:00Z"

        def client_for(author):
            class _Client:
                async def get(self, url, headers=None, params=None):
                    class _R:
                        def raise_for_status(s):
                            pass

                        def json(s):
                            return [
                                {
                                    "id": 1,
                                    "user": {"login": author},
                                    "body": f"<!-- review-deferred-until:{iso} -->",
                                }
                            ]

                    return _R()

            return _Client()

        d = sd.SwarmDispatcher(_StubNotifier(), _config())
        assert await d._has_live_deferral_marker(client_for(SWARM), "owner/repo", 87) is True
        assert await d._has_live_deferral_marker(client_for(OTHER), "owner/repo", 87) is False


class TestNoticeMarkersCannotBePreSeededByAnotherAccount:
    """A comment carrying a once-only notice marker from a non-swarm account
    must not make the dispatcher believe the notice was already posted."""

    def _client(self, comments, patches, posts):
        class _Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kw):
                class _R:
                    status_code = 200

                    def raise_for_status(s):
                        pass

                    def json(s):
                        return comments

                return _R()

            async def patch(self, url, **kw):
                patches.append(url)

                class _R:
                    status_code = 200

                    def raise_for_status(s):
                        pass

                return _R()

            async def post(self, url, **kw):
                posts.append(url)

                class _R:
                    status_code = 201

                    def raise_for_status(s):
                        pass

                    def json(s):
                        return {"id": 1}

                return _R()

        return _Client

    def test_a_pre_seeded_confirmation_marker_is_not_edited_as_if_it_were_ours(
        self, monkeypatch
    ):
        from test_swarm_dispatch import _swarm_run_trigger

        patches, posts = [], []
        comments = [
            {"id": 7, "user": {"login": OTHER}, "body": "<!-- swarm-run-confirmation -->"}
        ]
        monkeypatch.setattr(httpx, "AsyncClient", self._client(comments, patches, posts))
        monkeypatch.setattr(sd, "_token_for_repo", lambda repo: "fake-token")
        d = sd.SwarmDispatcher(_StubNotifier(), _config())
        asyncio.run(d._post_swarm_run_comment(_swarm_run_trigger()))
        assert patches == [] and len(posts) == 1

    def test_a_pre_seeded_bypass_marker_does_not_suppress_the_notice(self, monkeypatch):
        patches, posts = [], []
        comments = [
            {"id": 7, "user": {"login": OTHER}, "body": "<!-- pipeline-bypass-notice -->"}
        ]
        monkeypatch.setattr(httpx, "AsyncClient", self._client(comments, patches, posts))
        monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
        d = sd.SwarmDispatcher(_StubNotifier(), _config())
        asyncio.run(d._post_pipeline_bypass_comment(_trigger()))
        assert patches == [] and len(posts) == 1


# ── a new reader cannot skip the author check unnoticed ─────────────────────
#
# What this guard is, and is not. It parses a module and flags any function
# that (1) fetches PR comments (calls `_all_issue_comments` /
# `_fetch_issue_comments`, or builds a ".../comments" URL) AND (2) touches a
# lens-verdict parser or swarm-marker matcher, directly or through any
# function in the same module that does (the set grows to a fixed point), AND
# (3) never references an author scope (`_lens_scoped_comments`,
# `_swarm_authored`, `scope_lens_comments`, `is_swarm_comment`,
# `lens_comment_authors`). A function that is handed comments rather than
# fetching them is not flagged: its caller is, if the caller fetches. Limits:
# it is name-based (a reader that parses with an unlisted, unrelated routine
# is invisible), and it cannot tell whether the scope helper it finds is
# applied to the comments that are parsed; the per-reader behaviour tests above
# cover that. `_GUARD_SELF_TESTS` shows it fails on the three shapes it exists
# for.

_PARSER_SEEDS = frozenset(
    {
        "lens_own_verdict",
        "sign_off_is_warranted",
        "lens_records",
        "last_reviewed_head",
        "has_new_blocking_finding",
        "lens_comment_satisfies_presence",
        "lenses_missing_comments",
        "latest_aggregation_comment",
        "vanellus_comment_missing",
        "parse_aggregation_marker",
        "compose_lens_review_marker",
        "_LENS_MARKER_RE",
        "_AGGREGATION_MARKER_RE",
        "_VANELLUS_COMMENT_MARKER",
        "_REVIEW_DEFERRED_RE",
        "_matching_comments",
        "_latest_matching_comment",
    }
)
_SCOPE_HELPERS = frozenset(
    {
        "_lens_scoped_comments",
        "_swarm_authored",
        "scope_lens_comments",
        "is_swarm_comment",
        "lens_comment_authors",
        "lens_comment_authors_resolution",
    }
)
_FETCHERS = frozenset({"_all_issue_comments", "_fetch_issue_comments"})


def _guard_violations(source: str, *, allow: frozenset[str] = frozenset()) -> list[str]:
    tree = ast.parse(source)
    funcs: dict[str, tuple[set[str], bool]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names: set[str] = set()
        has_comments_url = False
        has_get_call = False
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                names.add(sub.attr)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if "/comments" in sub.value:
                    has_comments_url = True
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "get"
            ):
                has_get_call = True
        fetches = (has_comments_url and has_get_call) or bool(names & _FETCHERS)
        funcs[node.name] = (names, fetches)
    reach = set(_PARSER_SEEDS)
    changed = True
    while changed:
        changed = False
        for name, (names, _) in funcs.items():
            if name in reach or names & _SCOPE_HELPERS or name in _FETCHERS:
                continue
            if names & reach:
                reach.add(name)
                changed = True
    return sorted(
        name
        for name, (names, fetches) in funcs.items()
        if name not in allow
        and name not in _FETCHERS
        and fetches
        and names & reach
        and not names & _SCOPE_HELPERS
    )


_DISPATCHER = Path(sd.__file__)
_APPROVE = Path(__file__).resolve().parents[2] / "scripts" / "approve_pr_as_app.py"
# `_supersede_stale_verdicts` reads comments only to decide which BOT-authored
# markers to retire and checks each author itself (`_is_bot_author`).
# `_handle_panel_session_limit` lists comments only to DELETE stale deferral
# markers before posting a fresh one; it reads no state from them.
_ALLOWED = frozenset({"_supersede_stale_verdicts", "_handle_panel_session_limit"})


@pytest.mark.parametrize("path", [_DISPATCHER, _APPROVE], ids=lambda p: p.name)
def test_every_function_that_fetches_and_parses_swarm_comments_scopes_by_author(path):
    assert _guard_violations(path.read_text(), allow=_ALLOWED) == []


_REVERTED_MERGE_GUARD = """
class D:
    async def _live_blocking_verdict_outside_panel(self, trigger):
        comments = await self._all_issue_comments(trigger.repository, trigger.number, None)
        for c in reversed(comments):
            if compose_lens_review_marker("arch", "x") in c["body"]:
                return not sign_off_is_warranted(c["body"], lens_agent="waxwing")
"""
_LENS_OWN_VERDICT_ALONE = """
class D:
    async def _new_reader(self, repo, n, client):
        comments = await self._all_issue_comments(repo, n, client)
        return [lens_own_verdict(c["body"], lens_agent="x") for c in comments]
"""
_INLINE_FETCH = """
class D:
    async def _new_reader(self, repo, n, client):
        resp = await client.get(f"https://api.github.com/repos/{repo}/issues/{n}/comments")
        return [latest_aggregation_comment(resp.json())]
"""
_VIA_A_NEW_PARSER = """
def my_new_parser(comments):
    return [lens_own_verdict(c["body"], lens_agent="x") for c in comments]

class D:
    async def _caller(self, repo, n, client):
        return my_new_parser(await self._all_issue_comments(repo, n, client))
"""
_SCOPED_OK = """
class D:
    async def _reader(self, repo, n, client):
        comments = await self._lens_scoped_comments(repo, n, client)
        return [lens_own_verdict(c["body"], lens_agent="x") for c in comments]
"""


@pytest.mark.parametrize(
    "source, flagged",
    [
        (_REVERTED_MERGE_GUARD, "_live_blocking_verdict_outside_panel"),
        (_LENS_OWN_VERDICT_ALONE, "_new_reader"),
        (_INLINE_FETCH, "_new_reader"),
        (_VIA_A_NEW_PARSER, "_caller"),
    ],
    ids=["reverted-merge-guard", "lens_own_verdict-alone", "inline-fetch", "via-a-new-parser"],
)
def test_the_guard_flags_the_shapes_it_exists_for(source, flagged):
    assert flagged in _guard_violations(source)


def test_the_guard_accepts_a_scoped_reader():
    assert _guard_violations(_SCOPED_OK) == []
