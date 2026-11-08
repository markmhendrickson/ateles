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
    """What a return-only lens really returns: header, verdict, body (the
    dispatcher adds the marker; a response carrying one is refused)."""
    header = sd.attribution_header(agent, f"{lens} lens panelist")
    return f"{B}\n{header}\n**{verdict}**\n{body}\n{E}"


GOOD = returned()
BODY = "\n".join(GOOD.split("\n")[1:-1])  # header, verdict, body


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
    assert sd.parse_returned_lens_review(text, "pavo") is not None


def test_unwrap_turns_an_unparseable_run_into_a_failed_one():
    bad = SkillResult("pavo", True, 0, f"{GOOD}\n{GOOD}", "")
    result, update = sd.unwrap_returned_review(bad, "pavo")
    assert not result.ok and result.stdout == "" and update is None
    good, _ = sd.unwrap_returned_review(SkillResult("pavo", True, 0, GOOD, ""), "pavo")
    assert good.ok and good.stdout == BODY


# ── composition ─────────────────────────────────────────────────────────────


def test_the_comment_is_the_head_marker_then_the_reviewed_bytes_unchanged():
    marker = sd.compose_lens_review_marker("pm", HEAD)
    for text in (BODY, "    indented\n\n  keep   ", BODY + "\n\n_footer_"):
        assert sd.compose_remote_lens_comment("pm", text, HEAD) == f"{marker}\n{text}"


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
        _trigger(head_sha=HEAD), [("pm", BODY if text is None else text)],
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
    import lens_authors

    assert lens_authors._swarm_marker_tokens(body) == ["review:pm"]
    assert lens_authors.claimed_lenses(body) == {"pm"}


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
        ("security", "\n".join(returned("security", "falco").split("\n")[1:-1]))]


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


def _combined_setup(monkeypatch, *, opted_in: bool):
    """A dispatcher whose combined pass (pm + ux) has the first lens's class
    opted in to Fly or not."""
    import skill_runner
    from test_fly_read_only_call_sites import _async_providers
    from test_swarm_dispatch import _StubNotifier, _config

    d = sd.SwarmDispatcher(_StubNotifier(), _config())
    monkeypatch.setattr(skill_runner, "DISPATCHER_LENS_PUBLICATION_READY", opted_in)
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "lens_review:pm,lens_review:ux")
    monkeypatch.setattr(d, "_lens_agent_prompt", lambda agent: "own prompt")
    monkeypatch.setattr(sd, "usable_providers_async", lambda: _async_providers())
    monkeypatch.setattr(sd, "resolve_lens_provider", lambda *a, **k: "claude")
    return d


def _block(lens, inner):
    import review_carry
    return f"{review_carry.combined_delimiter(lens)}\n{inner}"


def _run_combined(monkeypatch, d, reply, *, real_poster=False):
    """Run the combined pass on *reply*; returns (result, the run_skill call,
    what was posted). With `real_poster` the real `_post_combined_comments`
    runs against a recording client (no lens has its own App); otherwise the
    poster is replaced by one recording (lens, text, remote_agents)."""
    import asyncio
    import httpx
    from test_swarm_dispatch import _trigger

    seen: dict = {}

    async def fake_run_skill(skill, prompt, **kwargs):
        seen.update(prompt=prompt, kwargs=kwargs)
        return SkillResult(skill, True, 0, reply, "")

    monkeypatch.setattr(sd, "run_skill", fake_run_skill)
    posted: list = []
    if real_poster:
        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                posted.append(json["body"])

                class _R:
                    def raise_for_status(self):
                        pass

                return _R()

        async def allowed(*a, **k):
            return True

        monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())
        monkeypatch.setattr(d, "_require_github_write_credential", allowed)
        monkeypatch.setattr(gat, "lens_has_own_app", lambda lens: False)
    else:
        async def fake_post(trigger, accepted, head, *, remote_agents=None):
            for lens, res in accepted.items():
                posted.append((lens, res.stdout, dict(remote_agents or {})))
            return set(accepted)

        monkeypatch.setattr(d, "_post_combined_comments", fake_post)
    pm, ux = sd.lens_by_name("pm"), sd.lens_by_name("ux")
    out = asyncio.run(d._run_combined_pass(
        _trigger(), [pm, ux], {}, 80, HEAD, pending_gates=set(), live_gates={}, signals=None))
    return out, seen, posted


UX_GOOD = returned("ux", "accipiter")
UX_BODY = "\n".join(UX_GOOD.split("\n")[1:-1])


def _ro_reply(pm_inner=GOOD, ux_inner=UX_GOOD):
    return f"{_block('pm', pm_inner)}\n{_block('ux', ux_inner)}"


def test_opted_in_combined_pass_runs_return_only_and_posts_the_unwrapped_review(monkeypatch):
    """Red before: the prompt carried no return-only wording, `runner` was
    "local", and each block was posted as written, delimiters included."""
    d = _combined_setup(monkeypatch, opted_in=True)
    assert sd._lens_return_only_on_fly(sd.lens_by_name("pm"))
    out, seen, posted = _run_combined(monkeypatch, d, _ro_reply(), real_poster=True)
    assert sd._NO_NEOTOMA_NOTE in seen["prompt"]
    assert B in seen["prompt"] and "Make no GitHub write" in seen["prompt"]
    assert "only write you make" not in seen["prompt"]
    assert seen["kwargs"].get("runner") != "local"
    assert seen["kwargs"]["github_read_only"] is True
    assert seen["kwargs"]["seated_reviewer"] is True
    assert seen["kwargs"]["action_class"] == "lens_review:pm"
    assert sorted(out) == ["pm", "ux"] and out.remote_lenses == {"pm", "ux"}
    assert out["pm"].stdout == BODY and out["ux"].stdout == UX_BODY
    # The single-lens return-only comment shape, so the readers agree on it.
    assert posted == [
        sd.compose_remote_lens_comment("pm", BODY, HEAD),
        sd.compose_remote_lens_comment("ux", UX_BODY, HEAD),
    ]
    assert all(B not in body and E not in body for body in posted)
    assert sd.verified_remote_comment("pm", "pavo", BODY, HEAD) == posted[0]


def test_a_malformed_block_is_not_accepted_or_posted_and_runs_alone(monkeypatch):
    """Red before: the block's own verdict was readable, so it was accepted
    and posted as written although it is two review sections, not one."""
    d = _combined_setup(monkeypatch, opted_in=True)
    out, _, posted = _run_combined(monkeypatch, d, _ro_reply(pm_inner=f"{GOOD}\n{GOOD}"))
    assert sorted(out) == ["ux"] and out.remote_lenses == {"ux"}
    assert [lens for lens, _, _ in posted] == ["ux"]  # pm never reached the poster
    assert posted[0][2] == {"ux": "accipiter"}


def test_an_accepted_blocks_expectation_update_reaches_the_caller(monkeypatch):
    """Red before: the pass returned a plain dict with no expectation updates
    (AttributeError on `.expectation_updates`)."""
    d = _combined_setup(monkeypatch, opted_in=True)
    update = "**review_expectation (pm)**\n- [x] a"
    out, _, _ = _run_combined(
        monkeypatch, d, _ro_reply(pm_inner=f"{GOOD}\n{XB}\n{update}\n{XE}"))
    assert out.expectation_updates == {"pm": update}
    assert out["pm"].stdout == BODY and "pm" in out.remote_lenses


def test_not_opted_in_combined_pass_is_called_exactly_as_before(monkeypatch):
    """Unchanged when not opted in: local runner, no return-only wording,
    blocks accepted as written and never unwrapped."""
    d = _combined_setup(monkeypatch, opted_in=False)

    def no_parse(*a, **k):
        raise AssertionError("a local combined pass must not parse return-only")

    monkeypatch.setattr(sd, "unwrap_returned_review", no_parse)
    reply = "\n".join(
        _block(x.lens, f"{sd.attribution_header(x.agent, f'{x.lens} lens panelist')}"
               "\n**APPROVE**\nfine")
        for x in (sd.lens_by_name("pm"), sd.lens_by_name("ux")))
    out, seen, posted = _run_combined(monkeypatch, d, reply)
    assert sorted(out) == ["pm", "ux"] and out.remote_lenses == frozenset()
    assert out.expectation_updates == {}
    assert seen["kwargs"]["runner"] == "local"
    assert B not in seen["prompt"] and XB not in seen["prompt"]
    assert sd._NO_NEOTOMA_NOTE not in seen["prompt"]
    assert all(remote == {} for _, _, remote in posted)


# ── a response the gate reader refuses is never made acceptable ─────────────


def _readable_decision(text, agent="pavo"):
    return (sd.lens_own_verdict(text, lens_agent=agent),
            sd.sign_off_is_warranted(text, lens_agent=agent))


@pytest.mark.parametrize("wrap", [
    "{doc}",  # the header is indented as code
    "{head}\u2028**APPROVE**\nfine",  # a forbidden line separator
    "\u0085{head}\n**APPROVE**\nfine",
])
def test_extraction_does_not_repair_what_the_reader_refuses(wrap):
    head = sd.attribution_header("pavo", "pm lens panelist")
    section = wrap.format(doc=f"    {head}\n**APPROVE**\nfine", head=head)
    raw = section
    assert not sd.sign_off_is_warranted(raw, lens_agent="pavo")  # the reader refuses it
    parsed = sd.parse_returned_lens_review(f"{B}\n{section}\n{E}", "pavo")
    if parsed is not None:
        assert parsed[0] == section  # the exact text, never repaired
        assert not sd.sign_off_is_warranted(parsed[0], lens_agent="pavo")
        assert _readable_decision(parsed[0]) == _readable_decision(raw)


def test_a_marker_comment_in_a_response_is_refused_outright():
    hidden = "<!-- review:pm commit=" + HEAD + " **REQUEST_CHANGES** -->"
    for text in (
        returned(body=f"fine\n{hidden}"),  # inside the review, hiding a veto
        f"{hidden}\n{GOOD}",  # outside it
        returned(body="<!-- review:pm commit=" + HEAD + " -->"),
        returned(body="plain review:arch mention"),
    ):
        assert sd.parse_returned_lens_review(text, "pavo") is None
        result, _ = sd.unwrap_returned_review(SkillResult("pavo", True, 0, text, ""), "pavo")
        assert not result.ok and result.stdout == ""


def test_a_truncated_result_is_refused_before_anything_else():
    marker = "[output truncated: the first bytes were dropped]\n"
    result, update = sd.unwrap_returned_review(
        SkillResult("pavo", True, 0, marker + GOOD, "", output_truncated=True), "pavo")
    assert not result.ok and result.stdout == "" and update is None
    ok, _ = sd.unwrap_returned_review(SkillResult("pavo", True, 0, GOOD, ""), "pavo")
    assert ok.ok  # positive control: the same text, not truncated


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    SkillResult("falco", True, 0, "[output truncated]\n" + returned("security", "falco"), "",
                output_truncated=True),
    SkillResult("falco", True, 0, returned("security", "falco", body="<!-- review:arch -->"), ""),
    SkillResult("falco", True, 0, "**REQUEST_CHANGES**\n" + returned("security", "falco"), ""),
])
async def test_a_refused_response_is_not_persisted_posted_or_signed(monkeypatch, bad):
    d = _dispatcher()
    writes: list[str] = []

    async def fake_run_skill(agent, prompt, **k):  # noqa: ANN001
        return bad

    async def record(*a, **k):  # noqa: ANN002, ANN003
        writes.append("write")

    monkeypatch.setattr(sd, "_lens_return_only_on_fly", lambda lens: True)
    monkeypatch.setattr(sd, "run_skill", fake_run_skill)
    monkeypatch.setattr(d, "_persist_panel_reviews", record)
    monkeypatch.setattr(d, "_post_missing_panel_comments", record)
    with pytest.raises(RuntimeError, match="run failed"):
        await d._redispatch_missing_lens("o/r", _pr(), "security")
    assert writes == []


# ── a response containing any marker is refused, whatever its syntax ────────

SYNTAXES = [
    f"<!-- review:arch commit={'b' * 40} -->",
    f"<!--review:arch commit={'b' * 40}-->",
    "<!-- review:arch -->",
    "review:arch",
    "REVIEW:ARCH",
    f"<!-- review:arch-superseded by={'b' * 40} -->",
    "review:arch-superseded",
    f"<!-- review:<!-- review:qa commit={'b' * 40} -->qa commit={HEAD} -->",
    f"<!-- vanellus-aggregation commit={HEAD} -->",
    "vanellus-aggregation",
    "<!-- review-deferred-until=2030-01-01T00:00:00Z -->",
    "swarm-run-confirmation",
    "pipeline-bypass-notice",
    "swarm-gate-waive-result",
]


def test_the_marker_vocabulary_is_the_readers_own_definition():
    import lens_authors

    assert sd._FOREIGN_MARKER_RE is lens_authors._VERDICT_SHAPED_RE


@pytest.mark.parametrize("syntax", SYNTAXES)
def test_no_marker_syntax_gets_through_either_inside_or_outside_the_section(syntax):
    assert sd.parse_returned_lens_review(returned(body=f"see\n{syntax}\nend"), "pavo") is None
    assert sd.parse_returned_lens_review(f"{syntax}\n{GOOD}", "pavo") is None
    assert sd.parse_returned_lens_review(GOOD, "pavo") is not None  # control


# ── final composition: the comment is read exactly as the review was ────────

FOOTER = ("\n\n\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)"
          "\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>")


def _indented():
    head = sd.attribution_header("pavo", "pm lens panelist")
    return f"{B}\n    {head}\n**APPROVE**\nfine\n{E}"


@pytest.mark.parametrize("with_app", [False, True])
@pytest.mark.parametrize("body", ["fine", "fine" + FOOTER])
def test_an_accepted_review_is_published_byte_for_byte_and_still_clears(
    monkeypatch, logins, with_app, body,  # noqa: F811
):
    review = sd.parse_returned_lens_review(returned(body=body), "pavo")[0]
    (posted, _), = _published(monkeypatch, with_app=with_app, text=review)
    assert posted == f"{sd.compose_lens_review_marker('pm', HEAD)}\n{review}"
    assert posted.endswith(body)  # a terminal footer stays terminal
    record = _record(posted)
    assert (record.cleared, record.verdict) == (True, "approve")


@pytest.mark.parametrize("with_app", [False, True])
def test_a_review_the_readers_cannot_read_is_a_failed_run_with_no_comment(
    monkeypatch, logins, with_app,  # noqa: F811
):
    assert sd.parse_returned_lens_review(_indented(), "pavo") is None
    result, _ = sd.unwrap_returned_review(
        SkillResult("pavo", True, 0, _indented(), ""), "pavo", "pm", HEAD)
    assert not result.ok and result.stdout == ""
    # and a comment that did get posted that way would carry no clearance
    unreadable = "\n".join(_indented().split("\n")[1:-1])
    assert _record(f"{sd.compose_lens_review_marker('pm', HEAD)}\n{unreadable}").cleared is False


def _changing_composer(lens, text, head):
    """A composer deliberately altered so the readers decide differently."""
    return f"{sd.compose_lens_review_marker(lens, head)}\n    {text}"


def test_the_final_check_refuses_a_composer_that_changes_the_decision(monkeypatch):
    review = sd.parse_returned_lens_review(GOOD, "pavo")[0]
    assert sd.verified_remote_comment("pm", "pavo", review, HEAD) is not None  # control
    monkeypatch.setattr(sd, "compose_remote_lens_comment", _changing_composer)
    assert sd.verified_remote_comment("pm", "pavo", review, HEAD) is None
    result, _ = sd.unwrap_returned_review(
        SkillResult("pavo", True, 0, GOOD, ""), "pavo", "pm", HEAD)
    assert not result.ok and result.stdout == ""


@pytest.mark.parametrize("with_app", [False, True])
def test_nothing_is_posted_when_the_final_check_refuses(monkeypatch, logins, with_app):  # noqa: F811
    review = sd.parse_returned_lens_review(GOOD, "pavo")[0]
    monkeypatch.setattr(sd, "compose_remote_lens_comment", _changing_composer)
    assert _published(monkeypatch, with_app=with_app, text=review) == []


# ── round 2: a return-only combined reply is validated exactly as received ──
# Each case below went through the real splitter, the real
# `unwrap_returned_review` and the real `sign_off_is_warranted`. Red on head
# c5eaafa7: the combined path cleaned each block (trailing attribution lines,
# outer blank lines, text before the first delimiter) before validating it.

FOOTER = "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)"
PM_HEADER = sd.attribution_header("pavo", "pm lens panelist")


def _cleaning_would_have_accepted(reply):
    """The pm block as the old cleaning split it is a clear verdict the real
    readers accept and sign: so the rejection below is the new rule's doing."""
    import review_carry
    old = review_carry.split_combined_reply(reply, ["pm", "ux"])["pm"]
    unwrapped, _ = sd.unwrap_returned_review(
        SkillResult("pavo", True, 0, old, ""), "pavo", "pm", HEAD)
    return unwrapped.ok and sd.sign_off_is_warranted(unwrapped.stdout, lens_agent="pavo")


@pytest.mark.parametrize("trailing", [PM_HEADER, FOOTER], ids=["attribution", "footer"])
def test_a_trailing_line_after_a_block_is_not_removed_and_the_lens_runs_alone(
    monkeypatch, trailing
):
    reply = _ro_reply(pm_inner=f"{GOOD}\n{trailing}")
    assert _cleaning_would_have_accepted(reply)
    d = _combined_setup(monkeypatch, opted_in=True)
    out, _, posted = _run_combined(monkeypatch, d, reply)
    assert "pm" not in out and "pm" not in out.remote_lenses
    assert "pm" not in out.expectation_updates
    assert [lens for lens, _, _ in posted] == ["ux"]  # pm never posted or signed
    assert out["ux"].stdout == UX_BODY


@pytest.mark.parametrize("leading", [
    "**APPROVE**\n",
    f"{PM_HEADER}\n**APPROVE**\n",
    f"{sd.attribution_header('accipiter', 'ux lens panelist')}\n",
], ids=["verdict", "header-and-verdict", "header"])
def test_text_before_the_first_delimiter_rejects_every_lens_of_the_pass(
    monkeypatch, leading
):
    reply = leading + _ro_reply()
    assert _cleaning_would_have_accepted(reply)
    d = _combined_setup(monkeypatch, opted_in=True)
    out, _, posted = _run_combined(monkeypatch, d, reply)
    assert dict(out) == {} and out.remote_lenses == frozenset()
    assert out.expectation_updates == {}
    assert posted == []


def test_whitespace_around_the_blocks_is_still_accepted(monkeypatch):
    d = _combined_setup(monkeypatch, opted_in=True)
    reply = f"\n  \n{_block('pm', GOOD)}\n\n{_block('ux', UX_GOOD)}\n\n\n"
    out, _, posted = _run_combined(monkeypatch, d, reply)
    assert sorted(out) == ["pm", "ux"] and out.remote_lenses == {"pm", "ux"}
    assert out["pm"].stdout == BODY and out["ux"].stdout == UX_BODY
    assert [lens for lens, _, _ in posted] == ["pm", "ux"]
    assert sd.sign_off_is_warranted(out["pm"].stdout, lens_agent="pavo")


def test_the_local_path_keeps_its_cleaning(monkeypatch):
    """Not opted in: the local pass still drops a trailing footer line."""
    d = _combined_setup(monkeypatch, opted_in=False)
    pm_text = f"{PM_HEADER}\n**APPROVE**\nfine"
    ux_text = f"{sd.attribution_header('accipiter', 'ux lens panelist')}\n**APPROVE**\nfine"
    reply = f"{_block('pm', pm_text)}\n{FOOTER}\n\n{_block('ux', ux_text)}"
    out, _, _ = _run_combined(monkeypatch, d, reply)
    assert sorted(out) == ["pm", "ux"]
    assert out["pm"].stdout == pm_text + "\n"


def test_the_raw_split_returns_every_byte_in_a_block_or_the_leading_text():
    import review_carry
    pm_block = _block("pm", "a\n\n")
    reply = f" x\n{pm_block}{_block('ux', 'b')}\n{FOOTER}\n"
    blocks, leading = review_carry.split_combined_reply_raw(reply, ["pm", "ux"])
    assert leading == " x\n"
    assert blocks == {"pm": "a\n\n", "ux": f"b\n{FOOTER}\n"}
    # A repeated delimiter: that lens has no block, its text stays in another.
    twice = f"{_block('pm', 'a')}\n{_block('ux', 'b')}\n{_block('ux', 'c')}"
    blocks, leading = review_carry.split_combined_reply_raw(twice, ["pm", "ux"])
    assert leading == "" and set(blocks) == {"pm"}
    # A delimiter with nothing after it is an empty block, never accepted.
    blocks, _ = review_carry.split_combined_reply_raw(
        f"{_block('pm', 'a')}\n{review_carry.combined_delimiter('ux')}", ["pm", "ux"])
    assert blocks["ux"] == ""


# ── round 2: the panel loop with a combined return-only reply ───────────────


def test_the_panel_loop_publishes_persists_and_updates_a_combined_return_only_pass(
    monkeypatch,
):
    """Drives `_handle_pr` through `_handle_pr_panel` with pm/qa/ux combined
    and opted in. Deleting the panel-loop branch that adds combined lenses to
    `remote_lenses` and `expectation_updates` fails assertions (c) and (d)."""
    import asyncio
    import review_carry
    import skill_runner
    from test_fly_read_only_call_sites import _async_providers
    from test_swarm_dispatch import _pr_dispatcher_with_stubs, _trigger

    calls: list = []
    d = _pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=calls)
    d.config.combined_review_pass = True
    head = "a" * 40
    monkeypatch.setattr(skill_runner, "DISPATCHER_LENS_PUBLICATION_READY", True)
    monkeypatch.setenv(
        "APIS_FLY_RUNNER_CLASSES", "lens_review:pm,lens_review:qa,lens_review:ux")
    monkeypatch.setattr(d, "_lens_agent_prompt", lambda agent: "own prompt")
    monkeypatch.setattr(sd, "usable_providers_async", lambda: _async_providers())
    monkeypatch.setattr(sd, "resolve_lens_provider", lambda *a, **k: "claude")
    pm, qa, ux = (sd.lens_by_name(x) for x in ("pm", "qa", "ux"))
    monkeypatch.setattr(sd, "select_panel", lambda **k: [pm, qa, ux])

    async def not_held(*a, **k):
        return False

    monkeypatch.setattr(d, "_hold_panel_until_checks_green", not_held)

    update = "**review_expectation (qa)**\n- [x] a"
    texts = {x.lens: returned(x.lens, x.agent) for x in (pm, qa, ux)}
    texts["qa"] = f"{texts['qa']}\n{XB}\n{update}\n{XE}"
    reply = "\n".join(_block(lens, texts[lens]) for lens in ("pm", "qa", "ux"))
    alone: list = []

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        if skill == "vanellus":
            return SkillResult(skill, True, 0, "**APPROVE**\nlgtm", "")
        if skill == pm.agent and "=====REVIEW:" in prompt:
            return SkillResult(skill, True, 0, reply, "")
        alone.append(skill)
        return SkillResult(skill, True, 0, "**COMMENT**\nlgtm", "")

    monkeypatch.setattr(sd, "run_skill", fake_run_skill)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise AssertionError("no shared-identity post for a lens with its own App")

    async def allowed(*a, **k):
        return True

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())
    monkeypatch.setattr(d, "_require_github_write_credential", allowed)
    monkeypatch.setattr(gat, "lens_has_own_app", lambda lens: True)
    remote_posts: list = []

    async def fake_remote(client, url, t, lens, agent, text, head_sha):
        remote_posts.append((lens, agent, text, head_sha))
        return sd.verified_remote_comment(lens, agent, text, head_sha) is not None

    monkeypatch.setattr(d, "_post_remote_lens_verdict", fake_remote)

    def no_local(*a, **k):
        raise AssertionError("the local combined poster must not run")

    monkeypatch.setattr(review_carry, "compose_combined_comment", no_local)
    monkeypatch.setattr(sd, "compose_fallback_comment", no_local)
    persisted: list = []
    backfill: list = []
    published: list = []

    async def fake_persist(t, reviews, agents_by_lens=None, **k):
        persisted.extend(reviews)
        return True

    async def fake_backfill(t, reviews, agents_by_lens=None, **k):
        backfill.append(k.get("remote_lenses"))

    async def fake_publish(t, parent, updates, agents_by_lens):
        published.append(dict(updates))

    monkeypatch.setattr(d, "_persist_panel_reviews", fake_persist)
    monkeypatch.setattr(d, "_post_missing_panel_comments", fake_backfill)
    monkeypatch.setattr(d, "_publish_expectation_updates", fake_publish)

    asyncio.run(d._handle_pr(_trigger(body="Closes #80.", head_sha=head)))

    bodies = {x.lens: "\n".join(returned(x.lens, x.agent).split("\n")[1:-1])
              for x in (pm, qa, ux)}
    # (a) each review persisted once, as the unwrapped review
    assert sorted(persisted) == sorted(bodies.items())
    # (b) each lens published once through the remote path, nothing run alone
    assert sorted((lens, text) for lens, _, text, _ in remote_posts) == sorted(bodies.items())
    assert all(h == head for *_, h in remote_posts)
    assert not set(alone) & {pm.agent, qa.agent, ux.agent}
    # (c) the qa block's expectation update reaches the publisher for qa
    assert published == [{"qa": update}]
    # (d) the combined lenses are passed as remote to the backfill step
    assert backfill and {"pm", "qa", "ux"} <= set(backfill[0])


# ── an identifier containing `review:` is not a marker ──────────────────────


@pytest.mark.parametrize("ident", ["lens_review:pm", "lens_review:arch"])
def test_a_signed_off_review_naming_a_lens_class_is_accepted(ident):
    # Red on main: the class name was read as a swarm marker and refused.
    reply = returned(body=f"The action class `{ident}` keeps its tier. fine")
    parsed = sd.parse_returned_lens_review(reply, "pavo")
    assert parsed is not None and ident in parsed[0]
    result, _ = sd.unwrap_returned_review(SkillResult("pavo", True, 0, reply, ""), "pavo")
    assert result.ok and ident in result.stdout


@pytest.mark.parametrize("marker", ["x review:pm", "(review:pm", "review:pm", "REVIEW:Pm"])
def test_a_reply_with_a_marker_after_space_or_punctuation_is_still_refused(marker):
    assert sd.parse_returned_lens_review(returned(body=f"fine\n{marker}"), "pavo") is None
    assert sd.parse_returned_lens_review(returned(body=f"fine {marker} end"), "pavo") is None
