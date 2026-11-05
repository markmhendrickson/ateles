"""A return-only lens (on Fly, or after a Mac fallback of a Fly launch) returns
its review in delimited sections and the dispatcher publishes it as the lens's
own App. Each test below failed before the change (the parser, the composer
and the `remote_lenses` publication path did not exist).

Run: pytest execution/daemons/apis/test_lens_return_only_publication.py -v
"""

from __future__ import annotations

import pytest

import swarm_dispatch as sd
from lib import github_app_token as gat
from skill_runner import SkillResult
from test_lens_per_lens_apps import HEAD, PM_APP, _declare, logins  # noqa: F401
from test_missing_lens_review_recovery import _dispatcher, _pr

B, E = sd.DISPATCHER_REVIEW_BEGIN, sd.DISPATCHER_REVIEW_END
XB, XE = sd.DISPATCHER_EXPECTATION_BEGIN, sd.DISPATCHER_EXPECTATION_END


def returned(lens="pm", agent="pavo", verdict="APPROVE", body="fine", head=HEAD):
    """What a return-only lens really returns: marker, header, verdict, body."""
    header = sd.attribution_header(agent, f"{lens} lens panelist")
    return f"{B}\n<!-- review:{lens} commit={head} -->\n{header}\n**{verdict}**\n{body}\n{E}"


GOOD = returned()
BODY = GOOD.split("\n", 2)[2].rsplit("\n", 1)[0]  # header, verdict, body


# ── parser: red before = AttributeError, the function did not exist ─────────


@pytest.mark.parametrize("stdout", [
    "",
    "**APPROVE** with no block at all",
    f"{B}\n**APPROVE**\n",  # no end
    f"**APPROVE**\n{E}",  # no begin
    f"{GOOD}\n{GOOD}",  # two blocks
    f"{B}\n{B}\n**APPROVE**\n{E}\n{E}",  # nested
    f"{B}\n{E}",  # empty
    f"{B}\n**APPROVE**\n{E}\n{XB}\nx\n{XE}\n{XB}\ny\n{XE}",  # two updates
    f"{B}\n**APPROVE**\n{XB}\nx\n{XE}\n{E}",  # update nested in the review
    f"{B}\n**APPROVE**\n{XE}\n{E}",  # wrong-kind end
])
def test_the_parser_rejects_zero_two_and_malformed_blocks(stdout):
    assert sd.parse_returned_lens_review(stdout) is None


def test_the_parser_accepts_one_block_and_one_optional_update():
    review, update = sd.parse_returned_lens_review(
        f"thinking aloud\n{GOOD}\n{XB}\n**review_expectation (pm)**\n- [x] a\n{XE}\n", "pavo")
    assert review == BODY and update.startswith("**review_expectation (pm)**")
    assert sd.parse_returned_lens_review(GOOD, "pavo")[1] is None


def test_a_prose_mention_of_the_delimiters_is_not_a_block():
    text = f"Return it between `{B}` and `{E}`.\n{GOOD}"
    assert sd.parse_returned_lens_review(text) is not None


def test_unwrap_turns_an_unparseable_run_into_a_failed_one():
    bad = SkillResult("pavo", True, 0, f"{GOOD}\n{GOOD}", "")
    result, update = sd.unwrap_returned_review(bad, "pavo")
    assert not result.ok and result.stdout == "" and update is None
    good, _ = sd.unwrap_returned_review(SkillResult("pavo", True, 0, GOOD, ""), "pavo")
    assert good.ok and good.stdout == BODY


# ── composition ─────────────────────────────────────────────────────────────


def test_the_comment_carries_exactly_this_lens_and_head_marker():
    other = "c" * 40
    spoof = (f"**APPROVE**\n<!-- review:arch commit={HEAD} -->\n"
             f"<!-- review:pm commit={other} -->\n<!-- review:<!-- review:qa commit={HEAD} -->"
             f"qa commit={other} -->\n<!-- vanellus-aggregation commit={HEAD} -->\n"
             f"<!-- review:pm commit=<full40hex> -->\nbody")
    body = sd.compose_remote_lens_comment("pm", spoof, HEAD)
    marker = sd.compose_lens_review_marker("pm", HEAD)
    assert body.startswith(marker + "\n")
    assert body.count("<!--") == 1 and body.count(marker) == 1
    assert "no GitHub write access" in body.splitlines()[-1]


# ── posting as the lens's own App ───────────────────────────────────────────


class _Client:
    def __init__(self, posted):
        self.posted = posted

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        class _R:
            def raise_for_status(s):
                pass

            def json(s):
                return []

        return _R()

    async def post(self, url, json=None, headers=None):
        self.posted.append((json["body"], headers))

        class _R:
            def raise_for_status(s):
                pass

        return _R()


async def _publish(monkeypatch, *, token, remote=frozenset({"pm"}), text=None):
    import httpx
    from test_gate_sign_off_dispatch import _StubNotifier, _config, _trigger

    posted: list = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client(posted))
    monkeypatch.setattr(sd, "_token_for_repo", lambda repo: "shared-token")
    monkeypatch.setattr(sd, "_token_for_agent_on_repo",
                        lambda agent, repo: token if agent == "pavo" else "shared-token")
    d = sd.SwarmDispatcher(_StubNotifier(), _config())
    await d._post_missing_panel_comments(
        _trigger(head_sha=HEAD), [("pm", text or f"**APPROVE**\n<!-- review:arch commit={HEAD} -->")],
        {"pm": "pavo"}, reviewed_head=HEAD, remote_lenses=remote,
    )
    return posted


@pytest.mark.asyncio
async def test_a_return_only_lens_is_posted_with_its_app_token(monkeypatch, logins):  # noqa: F811
    _declare(monkeypatch, "reviewer_pm")
    posted = await _publish(monkeypatch, token="pm-app-token")
    assert len(posted) == 1
    body, headers = posted[0]
    assert headers["Authorization"] == "Bearer pm-app-token"
    assert body.count(sd.compose_lens_review_marker("pm", HEAD)) == 1
    assert "review:arch" not in body


@pytest.mark.asyncio
async def test_an_empty_app_token_posts_nothing_and_never_the_shared_identity(
    monkeypatch, logins,  # noqa: F811
):
    _declare(monkeypatch, "reviewer_pm")
    assert await _publish(monkeypatch, token="") == []


@pytest.mark.asyncio
async def test_a_return_only_lens_with_no_app_posts_once_under_the_shared_identity(
    monkeypatch, logins,  # noqa: F811
):
    posted = await _publish(monkeypatch, token="pm-app-token")
    assert len(posted) == 1
    body, headers = posted[0]
    assert "shared-token" in headers["Authorization"]
    assert "pm-app-token" not in headers["Authorization"]
    assert body.count(sd.compose_lens_review_marker("pm", HEAD)) == 1
    assert body.count("<!--") == 1 and "review:arch" not in body


class _ExpClient(_Client):
    HEADER = "**review_expectation (pm)**"
    patched: list = []

    async def get(self, url, headers=None, params=None):
        class _R:
            def raise_for_status(s):
                pass

            def json(s):
                return [{"id": 7, "body": _ExpClient.HEADER + "\n- [ ] a",
                         "user": {"login": "swarm-lens-account"}}]

        return _R()

    async def patch(self, url, json=None, headers=None):
        self.posted.append((url, json["body"], headers))

        class _R:
            def raise_for_status(s):
                pass

        return _R()


async def _expectation(monkeypatch, *, with_app):
    import httpx
    from test_gate_sign_off_dispatch import _StubNotifier, _config, _trigger

    patched: list = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _ExpClient(patched))
    monkeypatch.setattr(sd, "_token_for_repo", lambda repo: "shared-token")
    monkeypatch.setattr(sd, "_token_for_agent_on_repo",
                        lambda agent, repo: "pm-app-token" if agent == "pavo" else "shared-token")
    monkeypatch.setattr(sd, "lens_comment_authors",
                        lambda: frozenset({"swarm-lens-account", PM_APP}))
    d = sd.SwarmDispatcher(_StubNotifier(), _config())
    await d._publish_expectation_updates(
        _trigger(), 80, {"pm": _ExpClient.HEADER + "\n- [x] a"}, {"pm": "pavo"})
    return patched


@pytest.mark.asyncio
async def test_an_expectation_update_with_no_app_uses_the_shared_headers(
    monkeypatch, logins,  # noqa: F811
):
    patched = await _expectation(monkeypatch, with_app=False)
    assert len(patched) == 1
    url, body, headers = patched[0]
    assert url.endswith("/issues/comments/7") and "- [x] a" in body
    assert "shared-token" in headers["Authorization"]


@pytest.mark.asyncio
async def test_an_expectation_update_uses_the_lens_app_when_it_has_one(
    monkeypatch, logins,  # noqa: F811
):
    _declare(monkeypatch, "reviewer_pm")
    patched = await _expectation(monkeypatch, with_app=True)
    assert patched and patched[0][2]["Authorization"] == "Bearer pm-app-token"


# ── publication keys on the prompt, not on the remote run ───────────────────


def _Ok(stdout: str = returned("security", "falco")) -> SkillResult:
    # remote_run_id stays empty: the Fly launch failed and the Mac ran it.
    return SkillResult("falco", True, 0, stdout, "")


@pytest.mark.asyncio
async def test_a_mac_fallback_of_a_return_only_run_is_still_published(monkeypatch):
    d = _dispatcher()
    seen: dict = {}

    async def fake_run_skill(agent, prompt, **k):  # noqa: ANN001
        seen["prompt"] = prompt
        return _Ok()

    async def fake_persist(trigger, reviews, agents, **kw):  # noqa: ANN001
        seen["reviews"] = reviews

    async def fake_post(trigger, reviews, agents, **kw):  # noqa: ANN001
        seen["remote"] = kw["remote_lenses"]

    monkeypatch.setattr(sd, "_lens_return_only_on_fly", lambda lens: True)
    monkeypatch.setattr(sd, "run_skill", fake_run_skill)
    monkeypatch.setattr(d, "_persist_panel_reviews", fake_persist)
    monkeypatch.setattr(d, "_post_missing_panel_comments", fake_post)
    await d._redispatch_missing_lens("o/r", _pr(), "security")
    assert B in seen["prompt"] and "no Neotoma tools" in seen["prompt"]
    assert seen["remote"] == frozenset({"security"})
    assert seen["reviews"] == [
        ("security", returned("security", "falco").split("\n", 2)[2].rsplit("\n", 1)[0])]


@pytest.mark.asyncio
async def test_a_local_prompt_run_is_not_treated_as_return_only(monkeypatch):
    d = _dispatcher()
    seen: dict = {}

    async def fake_run_skill(agent, prompt, **k):  # noqa: ANN001
        return _Ok("**APPROVE**")

    async def noop(*a, **k):  # noqa: ANN002, ANN003
        return None

    async def fake_post(trigger, reviews, agents, **kw):  # noqa: ANN001
        seen["remote"] = kw["remote_lenses"]

    monkeypatch.setattr(sd, "_lens_return_only_on_fly", lambda lens: False)
    monkeypatch.setattr(sd, "run_skill", fake_run_skill)
    monkeypatch.setattr(d, "_persist_panel_reviews", noop)
    monkeypatch.setattr(d, "_post_missing_panel_comments", fake_post)
    await d._redispatch_missing_lens("o/r", _pr(), "security")
    assert seen["remote"] == frozenset()


@pytest.mark.asyncio
async def test_an_unparseable_return_only_run_posts_nothing_and_fails_the_lens(monkeypatch):
    d = _dispatcher()
    calls: list[str] = []

    async def fake_run_skill(agent, prompt, **k):  # noqa: ANN001
        return _Ok(f"{GOOD}\n{GOOD}")

    async def record(*a, **k):  # noqa: ANN002, ANN003
        calls.append("write")

    monkeypatch.setattr(sd, "_lens_return_only_on_fly", lambda lens: True)
    monkeypatch.setattr(sd, "run_skill", fake_run_skill)
    monkeypatch.setattr(d, "_persist_panel_reviews", record)
    monkeypatch.setattr(d, "_post_missing_panel_comments", record)
    with pytest.raises(RuntimeError, match="well-formed review block"):
        await d._redispatch_missing_lens("o/r", _pr(), "security")
    assert calls == []


# ── a lost Fly lens run is re-dispatchable ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_lost_fly_lens_run_has_no_effects_and_is_run_again(monkeypatch):
    """Nothing reserves a lens receipt before launch, so an unknown outcome
    leaves nothing that holds the lens: the next same-head sweep runs it again."""
    d = _dispatcher()
    runs: list[str] = []
    writes: list[str] = []

    async def lost(agent, prompt, **k):  # noqa: ANN001
        runs.append(agent)
        return SkillResult(agent, False, None, "", "", error="remote outcome unknown",
                           remote_outcome_unknown=True, remote_run_id="fly-1")

    async def record(*a, **k):  # noqa: ANN002, ANN003
        writes.append("write")

    monkeypatch.setattr(sd, "_lens_return_only_on_fly", lambda lens: True)
    monkeypatch.setattr(sd, "run_skill", lost)
    monkeypatch.setattr(d, "_persist_panel_reviews", record)
    monkeypatch.setattr(d, "_post_missing_panel_comments", record)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="run failed"):
            await d._redispatch_missing_lens("o/r", _pr(), "security")
    assert runs == ["falco", "falco"] and writes == []


# ── round trips through the readers the gate and carry use ──────────────────

from lib import github_app_token as _gat  # noqa: E402
import lens_authors  # noqa: E402


def _record(body):
    return sd.lens_records([{"body": body, "created_at": "2026-01-01T00:00:00Z"}])["pm"][0]


def _published(monkeypatch, *, with_app, text):
    import asyncio

    async def go():
        if with_app:
            _declare(monkeypatch, "reviewer_pm")
        return await _publish(monkeypatch, token="pm-app-token", text=text)

    return asyncio.run(go())


def test_shared_return_only_comment_round_trips_through_approval_parser(monkeypatch, logins):  # noqa: F811
    raw = sd.parse_returned_lens_review(returned(), "pavo")[0]
    (body, headers), = _published(monkeypatch, with_app=False, text=raw)
    assert "shared-token" in headers["Authorization"]
    assert sd.sign_off_is_warranted(body, lens_agent="pavo")
    assert body.count(sd._HEADER_EMOJI) == 1
    assert "review:pm\n" not in body.replace(sd.compose_lens_review_marker("pm", HEAD), "")


@pytest.mark.parametrize("with_app", [False, True])
@pytest.mark.parametrize("verdict,cleared,token", [
    ("APPROVE", True, "approve"),
    ("REQUEST_CHANGES", False, "request_changes"),
])
def test_published_verdicts_keep_their_token_and_cleared_value(
    monkeypatch, logins, with_app, verdict, cleared, token,  # noqa: F811
):
    body_text = "[BLOCKING] x: y" if not cleared else "fine"
    raw = sd.parse_returned_lens_review(returned(verdict=verdict, body=body_text), "pavo")[0]
    (body, headers), = _published(monkeypatch, with_app=with_app, text=raw)
    expect = "pm-app-token" if with_app else "shared-token"
    assert expect in headers["Authorization"]
    record = _record(body)
    assert (record.cleared, record.verdict, record.head) == (cleared, token, HEAD)


@pytest.mark.asyncio
async def test_a_mac_fallback_of_a_return_only_run_keeps_its_verdict(monkeypatch, logins):  # noqa: F811
    """The run came back from the Mac (no remote run id) with a return-only
    prompt; what the dispatcher then posts still carries the clearance."""
    import httpx

    d = _dispatcher()
    posted: list = []
    seen: dict = {}

    async def fake_run_skill(agent, prompt, **k):  # noqa: ANN001
        return SkillResult(agent, True, 0, returned("security", "falco"), "")

    async def noop(*a, **k):  # noqa: ANN002, ANN003
        return None

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client(posted))
    monkeypatch.setattr(sd, "_token_for_repo", lambda repo: "shared-token")
    monkeypatch.setattr(sd, "_lens_return_only_on_fly", lambda lens: True)
    monkeypatch.setattr(sd, "run_skill", fake_run_skill)
    monkeypatch.setattr(d, "_persist_panel_reviews", noop)
    await d._redispatch_missing_lens("o/r", _pr(), "security")
    (body, _), = posted
    assert sd.sign_off_is_warranted(body, lens_agent="falco")
    assert body.count(sd._HEADER_EMOJI) == 1


# ── the whole response is judged before anything is removed ─────────────────


def test_a_refusal_outside_the_section_is_not_turned_into_an_approval():
    for outside in ("**REQUEST_CHANGES**", "[BLOCKING] sec: no", "**COMMENT**",
                    sd.attribution_header("pavo", "pm lens panelist")):
        assert sd.parse_returned_lens_review(f"{outside}\n{GOOD}", "pavo") is None, outside
        result, _ = sd.unwrap_returned_review(
            SkillResult("pavo", True, 0, f"{GOOD}\n{outside}", ""), "pavo")
        assert not result.ok


def test_a_refusal_in_a_combined_block_or_its_update_is_not_dropped():
    block = sd.attribution_header("pavo", "pm lens panelist") + "\n**APPROVE**\nfine"
    refusing_update = f"{XB}\n**review_expectation (pm)**\n**REQUEST_CHANGES**\n{XE}"
    assert sd.split_expectation_update(f"{block}\n{refusing_update}", "pavo") is None
    second_verdict = f"{XB}\n**COMMENT**\n{XE}"
    assert sd.split_expectation_update(f"{block}\n{second_verdict}", "pavo") is None
    veto_in_update = f"{XB}\n[BLOCKING] a: b\n{XE}"
    assert sd.split_expectation_update(f"{block}\n{veto_in_update}", "pavo") is None
    ok = sd.split_expectation_update(f"{block}\n{XB}\n**review_expectation (pm)**\n- [x] a\n{XE}", "pavo")
    assert ok is not None and ok[1] and sd.sign_off_is_warranted(ok[0], lens_agent="pavo")


def test_stripping_cannot_turn_a_refused_position_into_an_accepted_one():
    header = sd.attribution_header("pavo", "pm lens panelist")
    two_markers = (f"{B}\n<!-- review:pm commit={HEAD} -->\n<!-- review:pm commit={HEAD} -->\n"
                   f"{header}\n**APPROVE**\nfine\n{E}")
    assert sd.lens_own_verdict(two_markers.split("\n", 1)[1].rsplit("\n", 1)[0],
                               lens_agent="pavo") is None
    assert sd.parse_returned_lens_review(two_markers, "pavo") is None


# ── every marker syntax the readers accept is gone from what is published ───

SYNTAXES = [
    f"<!-- review:arch commit={'b' * 40} -->",
    f"<!--review:arch commit={'b' * 40}-->",
    "<!-- review:arch -->",
    "review:arch",
    "REVIEW:ARCH",
    f"<!-- review:arch-superseded by={'b' * 40} -->",
    "review:arch-superseded",
    "<!-- review:<full40hex> -->",
    f"<!-- review:<!-- review:qa commit={'b' * 40} -->qa commit={HEAD} -->",
    f"<!-- vanellus-aggregation commit={HEAD} -->",
    "vanellus-aggregation",
    "<!-- review-deferred-until=2030-01-01T00:00:00Z -->",
    "swarm-run-confirmation",
    "pipeline-bypass-notice",
    "swarm-gate-waive-result",
]


def test_the_stripping_uses_the_readers_own_definition():
    assert sd._FOREIGN_MARKER_RE is lens_authors._VERDICT_SHAPED_RE


@pytest.mark.parametrize("with_app", [False, True])
@pytest.mark.parametrize("syntax", SYNTAXES)
def test_no_foreign_marker_survives_either_identity_path(monkeypatch, logins, syntax, with_app):  # noqa: F811
    parsed = sd.parse_returned_lens_review(returned(body=f"see\n{syntax}\nend"), "pavo")
    assert parsed is not None
    (body, _), = _published(monkeypatch, with_app=with_app, text=parsed[0])
    assert lens_authors.claimed_lenses(body) == {"pm"}
    assert lens_authors._swarm_marker_tokens(body) == ["review:pm"]
    assert lens_authors.leading_lens(body) == "pm"
    assert sd.sign_off_is_warranted(body, lens_agent="pavo")
