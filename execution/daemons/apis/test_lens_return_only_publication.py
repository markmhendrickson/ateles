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
GOOD = f"{B}\n<!-- review:pm commit={HEAD} -->\n**APPROVE**\nfine\n{E}"


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
        f"thinking aloud\n{GOOD}\n{XB}\n**review_expectation (pm)**\n- [x] a\n{XE}\n")
    assert review == "**APPROVE**\nfine" and update.startswith("**review_expectation (pm)**")
    assert sd.parse_returned_lens_review(GOOD)[1] is None


def test_a_prose_mention_of_the_delimiters_is_not_a_block():
    text = f"Return it between `{B}` and `{E}`.\n{GOOD}"
    assert sd.parse_returned_lens_review(text) is not None


def test_unwrap_turns_an_unparseable_run_into_a_failed_one():
    bad = SkillResult("pavo", True, 0, f"{GOOD}\n{GOOD}", "")
    result, update = sd.unwrap_returned_review(bad)
    assert not result.ok and result.stdout == "" and update is None
    good, _ = sd.unwrap_returned_review(SkillResult("pavo", True, 0, GOOD, ""))
    assert good.ok and good.stdout == "**APPROVE**\nfine"


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


async def _publish(monkeypatch, *, token, remote=frozenset({"pm"})):
    import httpx
    from test_gate_sign_off_dispatch import _StubNotifier, _config, _trigger

    posted: list = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client(posted))
    monkeypatch.setattr(sd, "_token_for_repo", lambda repo: "shared-token")
    monkeypatch.setattr(sd, "_token_for_agent_on_repo",
                        lambda agent, repo: token if agent == "pavo" else "shared-token")
    d = sd.SwarmDispatcher(_StubNotifier(), _config())
    await d._post_missing_panel_comments(
        _trigger(head_sha=HEAD), [("pm", f"**APPROVE**\n<!-- review:arch commit={HEAD} -->")],
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
async def test_a_lens_with_no_declared_app_is_not_posted_as_the_shared_identity(
    monkeypatch, logins,  # noqa: F811
):
    assert await _publish(monkeypatch, token="pm-app-token") == []


# ── publication keys on the prompt, not on the remote run ───────────────────


def _Ok(stdout: str = GOOD.replace("pm", "security")) -> SkillResult:
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
    assert seen["reviews"] == [("security", "**APPROVE**\nfine")]


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
