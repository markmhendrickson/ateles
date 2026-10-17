"""Synthetic canonical-role admission through the real sibling review path."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
for entry in (ROOT, ROOT / "execution" / "daemons" / "apis", Path(__file__).parent):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import harness_lens_runner as runner  # noqa: E402
from lib.daemon_runtime.agent_loader import AgentDefinition, AgentLoader  # noqa: E402
import skill_runner  # noqa: E402


def definition() -> AgentDefinition:
    return AgentDefinition(
        entity_id="ent_synthetic_role",
        name="pavo",
        prompt_markdown="Canonical synthetic role",
        status="active",
        tool_allowlist=["Read"],
        raw={
            "entity_id": "ent_synthetic_role",
            "entity_type": "agent_definition",
            "snapshot": {
                "name": "pavo",
                "status": "active",
                "prompt_markdown": "Canonical synthetic role",
                "tool_allowlist": ["Read"],
            },
        },
    )


def test_same_runner_shape_uses_canonical_prompt_for_ateles_and_sibling(
    tmp_path, monkeypatch
):
    """Real run_one; inert guard/report boundaries never launch a provider."""
    role = definition()
    monkeypatch.setattr(AgentLoader, "load_for_lens", lambda self: role, raising=False)
    monkeypatch.setattr(runner, "check_headroom", lambda provider: None)
    monkeypatch.setattr(
        runner.Worktree, "create", lambda wt, **kw: wt.path.mkdir(parents=True)
    )
    monkeypatch.setattr(runner.Worktree, "remove", lambda wt: None)
    monkeypatch.setattr(
        runner.HarnessSandbox, "build", lambda *a, **kw: SimpleNamespace()
    )
    monkeypatch.setattr(runner, "render_lens_task", lambda target, brief: target.repo)
    seen = []

    def report(*a, task_text, **kw):
        seen.append(task_text)
        return {"ok": True}

    monkeypatch.setattr(runner, "dry_run_report", report)
    for repo in ("example/ateles", "example/neotoma"):
        target = runner.LensTarget(
            repo=repo, pr=1, head="a" * 40, lens="pm", agent="pavo"
        )
        result = asyncio.run(
            runner.run_one(
                target,
                provider="codex",
                post=False,
                dry_run=True,
                repo_worktree_name=repo.split("/")[-1],
                scratch_root=tmp_path,
                brief_path=tmp_path / "brief",
                timeout=900,
            )
        )
        assert result["ok"] is True
    assert len(seen) == 2
    assert all(role.prompt_markdown in task for task in seen)


def test_definition_pinned_from_dispatch_through_every_attempt(monkeypatch):
    role = definition()
    seen = []

    async def fake_once(skill, prompt, **kwargs):
        seen.append(kwargs.get("verified_agent_definition"))
        return skill_runner.SkillResult(
            skill, False, None, "", "", error="inert refusal"
        )

    async def attempts(skill, attempt, **kwargs):
        await attempt("codex")
        return await attempt("cursor")

    monkeypatch.setattr(skill_runner, "_run_skill_once", fake_once)
    monkeypatch.setattr(skill_runner, "_run_provider_attempts", attempts)
    monkeypatch.setattr(
        skill_runner, "_provider_binaries", lambda: {"codex": "/bin/false"}
    )
    asyncio.run(
        runner.dispatch_role.dispatch(
            "pavo",
            "task",
            provider="codex",
            local_review=True,
            verified_agent_definition=role,
        )
    )
    assert seen == [role, role]


@pytest.mark.parametrize("bad", [False, True])
def test_provider_attempt_never_reloads_generic_definition(monkeypatch, bad):
    role = definition()
    if bad:
        role.tool_allowlist = ["*"]
    monkeypatch.setattr(
        skill_runner,
        "_load_agent_def",
        lambda name: pytest.fail("generic role reloaded"),
    )
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {"codex": None})
    result = asyncio.run(
        skill_runner._run_skill_once(
            "pavo",
            "task",
            provider="codex",
            local_review=True,
            verified_agent_definition=role,
        )
    )
    assert not result.ok
    assert ("absent or changed" if bad else "binary unavailable") in result.error


@pytest.mark.parametrize("boundary", ["dispatch", "run_skill"])
@pytest.mark.parametrize("bad", ["outage", "wrong_role", "missing_tools", "status"])
def test_shared_local_review_admits_before_any_provider_work(
    monkeypatch, boundary, bad
):
    """Exercise the exported boundary, not a missing executable or skill."""
    role = definition()
    transport = []
    effects = []
    monkeypatch.setenv("PAVO_AGENT_DEFINITION_ID", role.entity_id)

    def read(self, method, url, body=None):
        transport.append((method, url))
        if bad == "outage":
            raise OSError("synthetic record outage")
        import copy

        data = copy.deepcopy(role.raw)
        if bad == "wrong_role":
            data["snapshot"]["name"] = "other"
        elif bad == "missing_tools":
            del data["snapshot"]["tool_allowlist"]
        else:
            data["snapshot"]["status"] = "retired"
        return data

    async def provider(*args, **kwargs):
        effects.append("provider")
        return skill_runner.SkillResult("pavo", True, 0, "inert", "")

    async def attempts(skill, attempt, **kwargs):
        return await attempt("codex")

    monkeypatch.setattr(AgentLoader, "_neotoma", read)
    monkeypatch.setattr(skill_runner, "_run_skill_once", provider)
    monkeypatch.setattr(skill_runner, "_run_provider_attempts", attempts)
    monkeypatch.setattr(
        skill_runner, "_provider_binaries", lambda: {"codex": "/bin/false"}
    )
    monkeypatch.setattr(skill_runner, "_fly_routing", lambda *a: (frozenset(), ()))
    monkeypatch.setattr(
        runner.dispatch_role,
        "run_skill",
        provider if boundary == "dispatch" else skill_runner.run_skill,
    )
    entry = (
        runner.dispatch_role.dispatch
        if boundary == "dispatch"
        else skill_runner.run_skill
    )
    result = asyncio.run(entry("pavo", "task", provider="codex", local_review=True))
    assert not result.ok
    assert "canonical lens" in result.error
    assert len(transport) == 1
    assert effects == []


@pytest.mark.parametrize("supplied", [None, "mutated"])
def test_attempt_refuses_unadmitted_review_before_generic_or_binary(
    monkeypatch, supplied
):
    calls = []
    role = definition() if supplied else None
    if role:
        role.tool_allowlist = ["*"]
    monkeypatch.setattr(
        skill_runner,
        "_load_agent_def",
        lambda name: calls.append("generic") or definition(),
    )
    monkeypatch.setattr(
        skill_runner,
        "_provider_binaries",
        lambda: calls.append("binary") or {"codex": None},
    )
    result = asyncio.run(
        skill_runner._run_skill_once(
            "pavo",
            "task",
            provider="codex",
            local_review=True,
            verified_agent_definition=role,
        )
    )
    assert not result.ok
    assert "canonical lens" in result.error
    assert calls == []


@pytest.mark.parametrize("boundary", ["dispatch", "run_skill"])
def test_shared_local_review_resolves_once_and_pins_all_attempts(monkeypatch, boundary):
    role = definition()
    loads, seen = [], []
    monkeypatch.setenv("PAVO_AGENT_DEFINITION_ID", role.entity_id)

    def read(self, method, url, body=None):
        loads.append((method, url))
        assert method == "GET" and url.endswith("/" + role.entity_id)
        return role.raw

    async def once(skill, task, **kwargs):
        seen.append(kwargs["verified_agent_definition"])
        return skill_runner.SkillResult(
            skill, False, None, "", "", error="inert attempt"
        )

    async def attempts(skill, attempt, **kwargs):
        await attempt("codex")
        return await attempt("cursor")

    monkeypatch.setattr(AgentLoader, "_neotoma", read)
    monkeypatch.setattr(skill_runner, "_run_skill_once", once)
    monkeypatch.setattr(skill_runner, "_run_provider_attempts", attempts)
    monkeypatch.setattr(
        skill_runner, "_provider_binaries", lambda: {"codex": "/bin/false"}
    )
    monkeypatch.setattr(skill_runner, "_fly_routing", lambda *a: (frozenset(), ()))
    entry = (
        runner.dispatch_role.dispatch
        if boundary == "dispatch"
        else skill_runner.run_skill
    )
    asyncio.run(entry("pavo", "task", provider="codex", local_review=True))
    assert len(loads) == 1
    assert len(seen) == 2 and seen[0] is seen[1]
    assert seen[0].raw == role.raw
    assert seen[0].prompt_markdown == role.prompt_markdown
    assert seen[0].tool_allowlist == role.tool_allowlist


def test_local_review_authority_precedes_deterministic_branch(monkeypatch):
    calls = []
    role = definition()
    role.is_stub = True
    monkeypatch.setattr(AgentLoader, "load_for_lens", lambda self: role)

    def generate(*args):
        calls.append("generator")
        return SimpleNamespace(status="done", summary="inert", changed_files=[])

    monkeypatch.setattr(
        runner.dispatch_role.mechanical_first, "run_generators", generate
    )
    result = asyncio.run(
        runner.dispatch_role.dispatch(
            "pavo",
            "task",
            local_review=True,
            work_class="regenerate_generated_files",
            regenerate_commands=["inert"],
        )
    )
    assert not result.ok
    assert calls == []


def test_real_confidence_caller_uses_shared_strict_admission(monkeypatch):
    import producer_confidence

    role = definition()
    role.name = role.raw["snapshot"]["name"] = "apis"
    loads, attempts = [], []
    monkeypatch.setenv("APIS_AGENT_DEFINITION_ID", role.entity_id)
    monkeypatch.setattr(
        AgentLoader,
        "_neotoma",
        lambda self, method, url, body=None: loads.append((method, url)) or role.raw,
    )
    monkeypatch.setattr(skill_runner, "_refresh_usage_snapshot", lambda *a: None)
    monkeypatch.setattr(
        skill_runner, "_provider_binaries", lambda: {"claude": "/bin/false"}
    )
    monkeypatch.setattr(
        skill_runner, "_tier_bound_binaries", lambda binaries, *a, **kw: binaries
    )
    monkeypatch.setattr(skill_runner, "_fly_routing", lambda *a: (frozenset(), ()))

    async def once(skill, prompt, **kwargs):
        attempts.append(kwargs)
        return skill_runner.SkillResult(
            skill, True, 0, '{"confidence":0.5}', "", provider="claude"
        )

    async def route(skill, attempt, **kwargs):
        assert kwargs["provider"] == skill_runner.INFERENCE_ONLY_PROVIDER
        return await attempt(kwargs["provider"])

    monkeypatch.setattr(skill_runner, "_run_skill_once", once)
    monkeypatch.setattr(skill_runner, "_run_provider_attempts", route)
    result = asyncio.run(
        producer_confidence._default_runner("score", SimpleNamespace(tier="mid"))
    )
    assert result[0]
    assert len(loads) == len(attempts) == 1
    assert attempts[0]["verified_agent_definition"].raw == role.raw
    assert attempts[0]["inference_only"] and attempts[0]["local_review"]
    assert attempts[0]["provider"] == skill_runner.INFERENCE_ONLY_PROVIDER


def test_generic_nonlocal_definition_path_remains_compatible(monkeypatch):
    calls = []
    monkeypatch.setattr(
        skill_runner,
        "_load_agent_def",
        lambda name: calls.append("generic") or definition(),
    )
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {"codex": None})
    result = asyncio.run(skill_runner._run_skill_once("pavo", "task", provider="codex"))
    assert not result.ok and "binary unavailable" in result.error
    assert calls == ["generic"]


@pytest.mark.parametrize(
    "failure,category",
    [
        ("connection", "source unavailable"),
        ("timeout", "source unavailable"),
        ("not_found", "missing or ambiguous"),
        ("query_not_found", "source unavailable"),
        ("access", "source unavailable"),
        ("server", "source unavailable"),
        ("malformed", "metadata inadmissible"),
        ("unexpected", None),
    ],
)
def test_canonical_refusal_category_is_safe_and_actionable(
    monkeypatch, caplog, failure, category
):
    import httpx

    monkeypatch.setenv("PAVO_AGENT_DEFINITION_ID", "ent_synthetic_role")
    sentinel = "SYNTHETIC-PRIVATE-DETAIL-MUST-NOT-REACH-CLI"

    def fail(*args, **kwargs):
        if failure == "connection":
            raise httpx.ConnectError(sentinel)
        if failure == "timeout":
            raise httpx.ReadTimeout(sentinel)
        if failure in ("not_found", "query_not_found", "server", "access"):
            status = 404 if failure in ("not_found", "query_not_found") else 503
            if failure == "access":
                status = 403
            method = "POST" if failure == "query_not_found" else "GET"
            response = httpx.Response(
                status, request=httpx.Request(method, "https://example.invalid/role")
            )
            raise httpx.HTTPStatusError(
                sentinel, request=response.request, response=response
            )
        if failure == "unexpected":
            raise RuntimeError(sentinel)
        raise ValueError(sentinel)

    monkeypatch.setattr(AgentLoader, "_neotoma", fail)
    with pytest.raises(runner.LensRoleUnavailable) as exc:
        runner.read_agent_definition("pavo")
    if category is None:
        assert str(exc.value) == "canonical lens role could not be loaded"
    else:
        assert category in str(exc.value)
    assert sentinel not in str(exc.value)
    assert sentinel not in caplog.text


@pytest.mark.parametrize("category", [None, "unrecognized", [], {}])
def test_unknown_refusal_category_never_infers_authority(monkeypatch, category):
    role = definition()
    role.is_stub = True
    role.load_error = "SYNTHETIC-PRIVATE-DETAIL-MUST-NOT-REACH-CLI"
    role.load_error_category = category
    monkeypatch.setattr(AgentLoader, "load_for_lens", lambda self: role)
    with pytest.raises(runner.LensRoleUnavailable) as exc:
        runner.read_agent_definition("pavo")
    assert str(exc.value) == "canonical lens role could not be loaded"


def test_canonical_diagnostic_preserves_valid_object_identity(monkeypatch):
    role = definition()
    monkeypatch.setattr(AgentLoader, "load_for_lens", lambda self: role)
    assert runner.read_agent_definition("pavo") is role
    assert getattr(role, "load_error_category", None) is None
