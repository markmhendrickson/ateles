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


def test_individual_lens_entrance_keeps_local_prompt_and_read_only_runner_contract(
    monkeypatch, fly_env, seam,
) -> None:
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


def _enclosing_function(tree: ast.AST, call: ast.Call) -> str:
    inner = ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= call.lineno <= (node.end_lineno or node.lineno):
                inner = node.name
    return inner


def test_every_pr_lens_dispatch_names_its_repository() -> None:
    # A Fly job's read-only GitHub token is scoped to the PR's repository and
    # refused without one, so a lens that reviews a pull request must pass it.
    # The issue-spec pipeline reviews an issue, not a PR, and is exempt.
    # Red before: the panel, missing-lens re-dispatch and fix-guidance calls
    # passed no `repository`.
    tree = ast.parse(SWARM_DISPATCH.read_text(encoding="utf-8"))
    missing = []
    for kind, call in _review_call_sites():
        if kind != "lens":
            continue
        if _enclosing_function(tree, call) == "_run_issue_spec_pipeline":
            continue
        if not any(kw.arg == "repository" for kw in call.keywords):
            missing.append(f"run_skill at line {call.lineno}")
    assert not missing, "PR lens dispatches without repository: " + ", ".join(missing)


def test_panel_lens_on_fly_hands_build_job_the_pr_repository(
    monkeypatch, fly_env, seam,
) -> None:
    # The 8 Nov incident: with a lens class opted onto Fly, the panel passed no
    # repository, so the job's token mint refused "no owner/name repository".
    # Red before: build_job saw repository="" for every panel lens.
    dispatcher = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="**REQUEST_CHANGES**\nblocking", calls=[]
    )
    dispatcher.config.combined_review_pass = False
    original = swarm_dispatch.run_skill
    lens_kwargs = []

    async def recording_run_skill(skill, prompt, **kwargs):
        if str(kwargs.get("action_class", "")).startswith("lens_review:"):
            lens_kwargs.append(kwargs)
        return await original(skill, prompt, **kwargs)

    monkeypatch.setattr(swarm_dispatch, "run_skill", recording_run_skill)
    monkeypatch.setattr(skill_runner, "DISPATCHER_LENS_PUBLICATION_READY", True)
    classes = {f"lens_review:{lens}" for lens in ("pm", "arch", "security", "qa", "ux")}
    monkeypatch.setattr(fly_runner, "FLY_VETTED_CLASSES", frozenset(classes))
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", ",".join(sorted(classes)))
    built = []

    def recording_build_job(**kw):
        built.append(kw["repository"])
        # Stop short of a launch; the run falls back to the local fake.
        raise fly_runner.FlyIneligible("test stops before launch")

    monkeypatch.setattr(fly_runner, "build_job", recording_build_job)
    trigger = _trigger(body="Closes #80.")
    asyncio.run(dispatcher._handle_pr(trigger))
    assert lens_kwargs
    assert all(kw["repository"] == trigger.repository for kw in lens_kwargs)
    # The stubbed panel run_skill stops at the call; drive each captured
    # dispatch through the real runner, opted onto Fly, to the job builder.
    spawned, fake_exec = seam
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        for kw in lens_kwargs:
            asyncio.run(skill_runner._run_skill_once(
                "lanius", "Review the diff", provider="claude", role="lanius",
                model="test", action_class=kw["action_class"],
                repository=kw["repository"], github_token="local-token",
                include_github_contract=True,
                github_read_only=kw["github_read_only"],
            ))
    assert len(built) == len(lens_kwargs), "a panel lens never reached the Fly job builder"
    assert all(repo == "owner/repo" for repo in built)
    # The token mint's own precondition holds for what the panel passed.
    for repo in built:
        try:
            fly_runner.mint_job_github_token(repo, "NO_SUCH_APP_PREFIX")
        except fly_runner.FlyIneligible as exc:
            assert "no owner/name repository" not in str(exc)
