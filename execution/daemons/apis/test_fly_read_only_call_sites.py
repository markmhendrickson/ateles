"""Read-class dispatch call sites and executable Fly refusal checks.

A lens reviews and never delivers, so each ``run_skill`` call whose
``action_class`` is a lens review or a carry-forward check must pass
``github_read_only=True``. The static scan stays alongside prompt and runner
checks that use the existing offline Fly and dispatcher fakes.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

import fly_runner
import skill_runner
import swarm_dispatch
from skill_runner import SkillResult
from test_fly_runner import seam, fly_env
from test_swarm_dispatch import (
    SwarmDispatcher,
    _StubNotifier,
    _config,
    _pr_dispatcher_with_stubs,
    _sample_lens,
    _trigger,
)

SWARM_DISPATCH = Path(__file__).with_name("swarm_dispatch.py")


def _func_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _classify(action_class: ast.AST) -> str | None:
    if (
        isinstance(action_class, ast.Call)
        and isinstance(action_class.func, ast.Attribute)
        and action_class.func.attr == "lens_review_class"
    ):
        return "lens"
    if (
        isinstance(action_class, ast.Attribute)
        and action_class.attr == "ACTION_CARRY_FORWARD_CHECK"
    ):
        return "carry_forward"
    return None


def _review_call_sites() -> list[tuple[str, ast.Call]]:
    tree = ast.parse(SWARM_DISPATCH.read_text(encoding="utf-8"))
    sites: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _func_name(node.func) != "run_skill":
            continue
        for kw in node.keywords:
            if kw.arg == "action_class":
                kind = _classify(kw.value)
                if kind:
                    sites.append((kind, node))
    return sites


def test_lens_publication_is_enabled_by_default() -> None:
    # The dispatcher publishes a return-only lens's review itself, so the flag
    # that refused every remote lens is on. The tests below that pin the
    # refusal switch it off explicitly. Red before: the flag was False.
    assert skill_runner.DISPATCHER_LENS_PUBLICATION_READY is True


def test_every_lens_and_carry_forward_dispatch_is_read_only() -> None:
    missing = []
    for kind, call in _review_call_sites():
        flag = next((kw for kw in call.keywords if kw.arg == "github_read_only"), None)
        if not (
            flag is not None
            and isinstance(flag.value, ast.Constant)
            and flag.value.value is True
        ):
            missing.append(f"{kind} run_skill at line {call.lineno}")
    assert not missing, "run_skill calls without github_read_only=True: " + ", ".join(
        missing
    )


def test_the_scan_found_the_known_call_sites() -> None:
    kinds = [kind for kind, _ in _review_call_sites()]
    assert kinds.count("lens") >= 5, kinds
    assert kinds.count("carry_forward") >= 2, kinds


def test_panelist_prompt_return_only_and_local_contract() -> None:
    trigger = _trigger()
    lens = _sample_lens()
    expectation = "- [ ] Verify the reported effect\n"
    local = SwarmDispatcher._panelist_prompt(trigger, lens, expectation, parent=80)
    read_only = SwarmDispatcher._panelist_prompt(
        trigger, lens, expectation, parent=80, read_only=True
    )
    assert "Post your review as a PR comment using the gh CLI" in local
    assert "gh api -X PATCH" in local
    assert "<<<DISPATCHER_REVIEW>>>" in read_only
    assert "<<<END_DISPATCHER_REVIEW>>>" in read_only
    assert "<<<DISPATCHER_EXPECTATION_UPDATE>>>" in read_only
    assert "<<<END_DISPATCHER_EXPECTATION_UPDATE>>>" in read_only
    assert "Verify the reported effect" in read_only
    assert "Post your review as a PR comment" not in read_only
    assert "gh api -X PATCH" not in read_only
    assert "only write you make" not in read_only


def test_combined_prompt_return_only_and_local_contract(monkeypatch) -> None:
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    monkeypatch.setattr(
        dispatcher, "_lens_agent_prompt",
        lambda agent: "Review this lens. Post a PR comment with gh.",
    )
    trigger = _trigger()
    lenses = [_sample_lens(), _sample_lens(forward_looking=True)]
    expectations = {lenses[0].agent: "- [ ] Verify the reported effect\n"}
    local = dispatcher._combined_prompt(trigger, lenses, expectations, "a" * 40, parent=80)
    read_only = dispatcher._combined_prompt(
        trigger, lenses, expectations, "a" * 40, parent=80, read_only=True
    )
    assert "only write you make" in local
    assert "gh api -X PATCH" in local
    assert "Make no GitHub write" in read_only
    assert "<<<DISPATCHER_EXPECTATION_UPDATE>>>" in read_only
    assert "gh api -X PATCH" not in read_only
    assert "Post a PR comment with gh" not in read_only
    assert "only write you make" not in read_only


def _give_every_lens_its_own_identity(monkeypatch) -> None:
    """A lens with a PAT of its own is not handed a read-only token, so it
    keeps the local prompt: that is the contract these two tests pin."""
    import review_panel

    for lens in review_panel.LENSES:
        monkeypatch.setenv(f"{lens.agent.upper()}_AGENT_PAT", f"own-pat-{lens.agent}-0000")


def test_individual_lens_entrance_keeps_local_prompt_and_read_only_runner_contract(
    monkeypatch, fly_env, seam,
) -> None:
    _give_every_lens_its_own_identity(monkeypatch)
    monkeypatch.setattr(skill_runner, "DISPATCHER_LENS_PUBLICATION_READY", False)
    calls = []
    dispatcher = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="**REQUEST_CHANGES**\nblocking", calls=calls
    )
    dispatcher.config.combined_review_pass = False
    original = swarm_dispatch.run_skill
    captured = []

    async def recording_run_skill(skill, prompt, **kwargs):
        if str(kwargs.get("action_class", "")).startswith("lens_review:"):
            captured.append((prompt, kwargs))
        return await original(skill, prompt, **kwargs)

    monkeypatch.setattr(swarm_dispatch, "run_skill", recording_run_skill)
    asyncio.run(dispatcher._handle_pr(_trigger(body="Closes #80.")))
    assert captured
    assert all(kwargs["github_read_only"] is True for _, kwargs in captured)
    assert all("Post your review as a PR comment" in prompt for prompt, _ in captured)
    _assert_lens_runner_guard(monkeypatch, captured[0][1])
    monkeypatch.setattr(skill_runner, "DISPATCHER_LENS_PUBLICATION_READY", True)
    monkeypatch.setattr(
        fly_runner, "FLY_VETTED_CLASSES",
        frozenset({kwargs["action_class"] for _, kwargs in captured}),
    )
    monkeypatch.setenv(
        "APIS_FLY_RUNNER_CLASSES",
        ",".join(kwargs["action_class"] for _, kwargs in captured),
    )
    captured.clear()
    asyncio.run(dispatcher._handle_pr(_trigger(body="Closes #80.")))
    assert captured
    assert all("<<<DISPATCHER_REVIEW>>>" in prompt for prompt, _ in captured)
    assert all("Post your review as a PR comment" not in prompt for prompt, _ in captured)


def test_combined_lens_entrance_keeps_local_prompt_and_read_only_runner_contract(
    monkeypatch, fly_env, seam,
) -> None:
    _give_every_lens_its_own_identity(monkeypatch)
    monkeypatch.setattr(skill_runner, "DISPATCHER_LENS_PUBLICATION_READY", False)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    monkeypatch.setattr(
        dispatcher, "_lens_agent_prompt",
        lambda agent: "Review this lens. Post a PR comment with gh.",
    )
    monkeypatch.setattr(swarm_dispatch, "usable_providers_async", lambda: _async_providers())
    monkeypatch.setattr(swarm_dispatch, "resolve_lens_provider", lambda *a, **k: "claude")
    captured = []

    async def fake_run_skill(skill, prompt, **kwargs):
        captured.append((prompt, kwargs))
        return SkillResult(skill, False, None, "", "", error="stopped before publication")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    result = asyncio.run(dispatcher._run_combined_pass(
        _trigger(), [_sample_lens(), _sample_lens(forward_looking=True)],
        {"phoenicurus": "- [ ] Verify the reported effect"}, 80, "a" * 40,
        pending_gates=set(), live_gates={}, signals=None,
    ))
    assert result == {}
    assert len(captured) == 1
    prompt, kwargs = captured[0]
    assert kwargs["github_read_only"] is True
    assert "only write you make" in prompt
    _assert_lens_runner_guard(monkeypatch, kwargs)
    monkeypatch.setattr(skill_runner, "DISPATCHER_LENS_PUBLICATION_READY", True)
    monkeypatch.setattr(fly_runner, "FLY_VETTED_CLASSES", frozenset({kwargs["action_class"]}))
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", kwargs["action_class"])
    captured.clear()
    asyncio.run(dispatcher._run_combined_pass(
        _trigger(), [_sample_lens(), _sample_lens(forward_looking=True)],
        {"phoenicurus": "- [ ] Verify the reported effect"}, 80, "a" * 40,
        pending_gates=set(), live_gates={}, signals=None,
    ))
    assert len(captured) == 1
    # The combined pass is out of scope for Fly: even opted in it keeps the
    # local prompt and is pinned to the Mac.
    assert "<<<DISPATCHER_EXPECTATION_UPDATE>>>" not in captured[0][0]
    assert "only write you make" in captured[0][0]
    assert captured[0][1]["runner"] == "local"


async def _async_providers():
    return ["claude"]


def _assert_lens_runner_guard(monkeypatch, dispatch_kwargs) -> None:
    monkeypatch.setattr(fly_runner, "build_job", lambda **kw: pytest.fail("Fly job built"))
    result = asyncio.run(skill_runner._run_skill_once(
        "lanius", "Review the diff", provider="claude", role="lanius",
        runner="fly", action_class=dispatch_kwargs["action_class"],
        model="test", repository="owner/repo", github_token="local-token",
        include_github_contract=True,
        github_read_only=dispatch_kwargs["github_read_only"],
    ))
    assert not result.ok
    assert "dispatcher-owned verdict and expectation publication" in result.error


@pytest.mark.parametrize("forced", [True, False])
def test_read_only_lens_fly_refusal_and_existing_local_fallback(
    fly_env, seam, monkeypatch, forced,
) -> None:
    spawned, fake_exec = seam
    monkeypatch.setattr(skill_runner, "DISPATCHER_LENS_PUBLICATION_READY", False)
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "lens_review:qa")
    monkeypatch.setattr(fly_runner, "FLY_VETTED_CLASSES", frozenset({"lens_review:qa"}))
    monkeypatch.setattr(fly_runner, "build_job", lambda **kw: pytest.fail("Fly job built"))
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(skill_runner._run_skill_once(
            "lanius", "Review the diff", provider="claude", role="lanius",
            runner="fly" if forced else None, action_class="lens_review:qa",
            model="test", repository="owner/repo", github_token="local-token",
            include_github_contract=True, github_read_only=True,
        ))
    if forced:
        assert not result.ok
        assert "dispatcher-owned verdict and expectation publication" in result.error
        assert spawned == []
    else:
        assert result.ok and result.stdout == "local output"
        assert len(spawned) == 1


# ── a local run that gets a read-only token cannot write ─────────────────────


@pytest.fixture
def _no_own_identity(monkeypatch):
    """No lens has an App or a PAT of its own: every local seated lens run is
    handed a read-only token, so it must be told not to write."""
    import os

    for name in list(os.environ):
        if name.endswith("_AGENT_PAT") or name.startswith("ATELES_APP_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ATELES_EXPECTED_LENS_APPS", raising=False)
    monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES", raising=False)


def _returned_reply(lens: str, agent: str) -> str:
    header = swarm_dispatch.attribution_header(agent, f"{lens} lens panelist")
    return (
        f"{swarm_dispatch.DISPATCHER_REVIEW_BEGIN}\n{header}\n**APPROVE**\nlooks fine\n"
        f"{swarm_dispatch.DISPATCHER_REVIEW_END}\n"
        f"{swarm_dispatch.DISPATCHER_EXPECTATION_BEGIN}\n"
        f"**{swarm_dispatch.EXPECTATION_MARKER} ({lens})**\n- [x] item\n"
        f"{swarm_dispatch.DISPATCHER_EXPECTATION_END}\n"
    )


def _assert_told_not_to_write(prompt: str) -> None:
    assert "<<<DISPATCHER_EXPECTATION_UPDATE>>>" in prompt
    assert "gh api -X PATCH" not in prompt
    assert "only write you make" not in prompt
    assert "Post your review as a PR comment" not in prompt


def test_local_read_token_individual_publishes_expectation_update(
    monkeypatch, _no_own_identity,
) -> None:
    import review_panel

    calls: list = []
    dispatcher = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=calls
    )
    dispatcher.config.combined_review_pass = False

    class _Everyone(dict):
        def get(self, key, default=None):
            return "- [ ] item"

    async def expectations(self, repo, parent):
        return _Everyone()

    monkeypatch.setattr(SwarmDispatcher, "_preregistered_expectations", expectations)
    published: list = []

    async def record_publish(self, t, parent, updates, agents_by_lens):
        published.append((parent, dict(updates)))

    monkeypatch.setattr(SwarmDispatcher, "_publish_expectation_updates", record_publish)
    by_agent = {lens.agent: lens for lens in review_panel.LENSES}
    seen: list = []
    stub = swarm_dispatch.run_skill  # the helper's fake: lanius, vanellus, panelists

    async def run_skill_for_lenses(skill, prompt, **kwargs):
        if str(kwargs.get("action_class", "")).startswith("lens_review:"):
            seen.append((skill, prompt, kwargs))
            lens = by_agent[skill]
            return SkillResult(skill, True, 0, _returned_reply(lens.lens, skill), "")
        return await stub(skill, prompt, **kwargs)

    monkeypatch.setattr(swarm_dispatch, "run_skill", run_skill_for_lenses)
    asyncio.run(dispatcher._handle_pr(_trigger(body="Closes #80.")))
    assert seen, "no lens was seated"
    # The selection is the local read-token one, not the Fly one.
    assert not any(swarm_dispatch._lens_return_only_on_fly(by_agent[s]) for s, _, _ in seen)
    for _, prompt, kwargs in seen:
        assert kwargs["github_read_only"] is True
        _assert_told_not_to_write(prompt)
    assert published, "the returned update never reached the dispatcher's publication"
    parent, updates = published[-1]
    assert parent == 80
    assert set(updates) == {by_agent[s].lens for s, _, _ in seen}
    assert all("- [x] item" in text for text in updates.values())


def test_local_read_token_combined_publishes_expectation_updates(
    monkeypatch, _no_own_identity,
) -> None:
    import review_carry
    import review_panel

    lenses = [l for l in review_panel.LENSES if l.lens in review_carry.COMBINED_LENSES][:2]
    assert len(lenses) == 2
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    monkeypatch.setattr(
        dispatcher, "_lens_agent_prompt",
        lambda agent: "Review this lens. Post a PR comment with gh.",
    )
    monkeypatch.setattr(swarm_dispatch, "usable_providers_async", lambda: _async_providers())
    monkeypatch.setattr(swarm_dispatch, "resolve_lens_provider", lambda *a, **k: "claude")
    captured: list = []

    def block(lens) -> str:
        header = swarm_dispatch.attribution_header(lens.agent, f"{lens.lens} lens panelist")
        return (
            f"{review_carry.combined_delimiter(lens.lens)}\n{header}\n**APPROVE**\nfine\n"
            f"{swarm_dispatch.DISPATCHER_EXPECTATION_BEGIN}\n"
            f"**{swarm_dispatch.EXPECTATION_MARKER} ({lens.lens})**\n- [x] item\n"
            f"{swarm_dispatch.DISPATCHER_EXPECTATION_END}\n"
        )

    async def fake_run_skill(skill, prompt, **kwargs):
        captured.append((prompt, kwargs))
        return SkillResult(skill, True, 0, "".join(block(l) for l in lenses), "")

    posted_bodies: dict = {}

    async def fake_post(self, trigger, accepted, review_head):
        posted_bodies.update({lens: res.stdout for lens, res in accepted.items()})
        return set(accepted)

    published: list = []

    async def record_publish(self, t, parent, updates, agents_by_lens):
        published.append((parent, dict(updates), dict(agents_by_lens)))

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_post_combined_comments", fake_post)
    monkeypatch.setattr(SwarmDispatcher, "_publish_expectation_updates", record_publish)
    result = asyncio.run(dispatcher._run_combined_pass(
        _trigger(), lenses, {l.agent: "- [ ] item" for l in lenses}, 80, "a" * 40,
        pending_gates=set(), live_gates={}, signals=None,
    ))
    assert set(result) == {l.lens for l in lenses}
    (prompt, kwargs), = captured
    assert kwargs["github_read_only"] is True and kwargs["runner"] == "local"
    _assert_told_not_to_write(prompt)
    assert published, "the returned updates never reached the dispatcher's publication"
    parent, updates, agents = published[-1]
    assert parent == 80
    assert set(updates) == {l.lens for l in lenses}
    assert agents == {l.lens: l.agent for l in lenses}
    # The update goes to publication, not into the posted verdict comment.
    assert all("DISPATCHER_EXPECTATION" not in body for body in posted_bodies.values())
    assert all(body.splitlines()[1] == "**APPROVE**" for body in posted_bodies.values())


def test_prompt_selection_agrees_with_the_run_eligibility(monkeypatch, _no_own_identity) -> None:
    import review_panel

    lens = review_panel.LENSES[0]
    assert swarm_dispatch._lens_return_only(lens, "owner/repo")
    # An agent with an identity of its own keeps the local prompt and its own token.
    monkeypatch.setenv(f"{lens.agent.upper()}_AGENT_PAT", "pat-of-its-own")
    assert not swarm_dispatch._lens_return_only(lens, "owner/repo")
