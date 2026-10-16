"""A dispatch whose role definition cannot be loaded is refused, not run on a stand-in.

The loader's own behaviour is pinned in lib/daemon_runtime/test_agent_definition_fail_closed.py.
These tests pin what the dispatcher does with it: nothing is spawned, the result
names a repair state (not a success, not a provider capacity limit), no provider is
cooled down or failed over to, and a failure is never remembered.

Run: pytest execution/daemons/apis/test_agent_definition_dispatch.py -v
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import skill_runner
from lib.daemon_runtime import agent_loader as al

REAL = {
    "entities": [
        {
            "entity_id": "ent_real",
            "snapshot": {
                "name": "pavo",
                "prompt_markdown": "# the real role prompt",
                "tool_allowlist": ["Read"],
                "status": "active",
                "aauth_sub": "pavo@ateles-swarm",
            },
        }
    ]
}


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def neotoma(monkeypatch):
    state = {"script": ["timeout"], "calls": 0, "spawned": []}
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(al, "_sleep", lambda s: None, raising=False)
    skill_runner._agent_def_cache.clear()
    import concurrent.futures
    import threading

    monkeypatch.setattr(al, "_load_pool", concurrent.futures.ThreadPoolExecutor(max_workers=4))
    monkeypatch.setattr(al, "_load_permits", threading.BoundedSemaphore(4))

    def post(url, **kwargs):
        state["calls"] += 1
        outcome = state["script"].pop(0) if len(state["script"]) > 1 else state["script"][0]
        if outcome == "timeout":
            raise httpx.ReadTimeout("timed out")
        if outcome == "none":
            return _Resp({"entities": []})
        return _Resp(REAL)

    monkeypatch.setattr(httpx, "post", post)

    async def no_spawn(*args, **kwargs):
        state["spawned"].append(args)
        raise AssertionError("a process was spawned although the definition did not load")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    monkeypatch.setattr(
        skill_runner, "_provider_binaries", lambda: {"codex": "/bin/true", "claude": "/bin/true"}
    )
    yield state
    skill_runner._agent_def_cache.clear()


def _once(provider="codex"):
    return asyncio.run(
        skill_runner._run_skill_once("pavo", "a task", provider=provider, role="pavo")
    )


def test_a_failed_definition_load_refuses_the_dispatch_and_spawns_nothing(neotoma):
    result = _once()

    assert result.ok is False
    assert result.definition_unavailable is True
    assert result.error.startswith(skill_runner.DEFINITION_UNAVAILABLE_PREFIX)
    assert result.returncode is None
    assert neotoma["spawned"] == []


def test_the_refusal_names_a_repair_state_and_carries_no_response_text(neotoma):
    result = _once()
    assert "repair" in result.error
    assert "timed out" not in result.error and "http" not in result.error.lower()


def test_the_refusal_is_not_read_as_provider_capacity(neotoma):
    result = _once()
    assert skill_runner._provider_failure_kind(result.error, result.stderr, result.stdout) is None


def test_a_failure_is_not_remembered(neotoma):
    """The first load fails; the next dispatch asks Neotoma again and gets the real one."""
    neotoma["script"] = ["timeout", "timeout", "timeout", "real"]

    first = _once()
    assert first.definition_unavailable
    assert "pavo" not in skill_runner._agent_def_cache

    agent_def = skill_runner._load_agent_def("pavo")
    assert agent_def.prompt_markdown == "# the real role prompt"
    assert skill_runner._agent_def_cache["pavo"] is agent_def


def test_run_skill_ends_at_the_first_provider_and_cools_nothing(neotoma):
    """Every provider would hit the same missing definition: no failover, no cooldown."""
    from harness_router import cooled_until_all

    result = asyncio.run(skill_runner.run_skill("pavo", "a task", role="pavo"))

    assert result.ok is False and result.definition_unavailable is True
    assert len(result.attempted_providers) == 1
    assert cooled_until_all({"codex": "/bin/true", "claude": "/bin/true"}) in ({}, None, [])
    assert neotoma["spawned"] == []


def test_a_role_with_no_definition_at_all_still_runs_degraded_by_opt_in(neotoma, monkeypatch):
    """Neotoma answered and has no such definition: this dispatcher's explicit opt-in,
    unchanged. The dispatch is not refused at the definition stage."""
    neotoma["script"] = ["none"]
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {})

    result = _once()

    assert result.definition_unavailable is False
    assert "binary unavailable" in result.error, "the dispatch got past the definition stage"


def test_a_timeout_then_success_dispatches_on_the_real_definition(neotoma, monkeypatch):
    neotoma["script"] = ["timeout", "real"]
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {})

    result = _once()

    assert result.definition_unavailable is False
    assert skill_runner._agent_def_cache["pavo"].prompt_markdown == "# the real role prompt"
    assert skill_runner._agent_def_cache["pavo"].tools == ["Read"]


def test_planted_red_a_stand_in_would_dispatch_with_wildcard_tools(neotoma, monkeypatch):
    """Reinstate the old behaviour: a failed load becomes a stand-in. The dispatch then
    gets past the definition stage with an empty prompt and wildcard tools."""
    real_load = al.AgentLoader.load

    def reinstated(self, *, allow_undefined=False):
        try:
            return real_load(self, allow_undefined=allow_undefined)
        except al.AgentDefinitionUnavailable as exc:
            return self._undefined_definition(exc.code)

    monkeypatch.setattr(al.AgentLoader, "load", reinstated)
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {})

    result = _once()

    assert result.definition_unavailable is False, "the reinstated stand-in lets the dispatch proceed"
    stand_in = skill_runner._load_agent_def("pavo")
    assert stand_in.prompt_markdown == "" and stand_in.tools == ["*"]


def test_a_retry_safe_dispatch_does_not_fail_over_or_cool_a_provider(neotoma, monkeypatch):
    """The retry-safe lane would otherwise treat any failed result as a reason to cool
    the provider and try the next one. A missing definition is neither."""
    cooled = []
    monkeypatch.setattr(skill_runner, "cool_down", lambda provider: cooled.append(provider))
    attempts = []

    async def attempt(selected, fallback_model=None):
        attempts.append(selected)
        return skill_runner.SkillResult(
            "pavo", False, None, "", "",
            error=f"{skill_runner.DEFINITION_UNAVAILABLE_PREFIX}: test",
            provider=selected, definition_unavailable=True,
        )

    result = asyncio.run(
        skill_runner._run_provider_attempts(
            "pavo", attempt,
            binaries={"codex": "/bin/true", "claude": "/bin/true"},
            role="pavo", retry_safe=True,
        )
    )

    assert result.definition_unavailable is True
    assert len(attempts) == 1, attempts
    assert cooled == []


def test_only_a_validated_found_definition_is_cached(neotoma):
    """The "no definition" result serves one dispatch and is not remembered; a role
    registered afterwards is picked up on the next one."""
    neotoma["script"] = ["none", "real"]

    first = skill_runner._load_agent_def("pavo")
    assert first.is_stub is True
    assert "pavo" not in skill_runner._agent_def_cache

    second = skill_runner._load_agent_def("pavo")
    assert second.prompt_markdown == "# the real role prompt"
    assert skill_runner._agent_def_cache["pavo"] is second


@pytest.mark.parametrize("malformed", [{"error": "boom"}, {"results": []}, {"entities": "x"}])
def test_a_malformed_lookup_is_refused_not_degraded(neotoma, monkeypatch, malformed):
    """The degraded path is for authoritative absence only."""
    class _Bad(_Resp):
        pass

    monkeypatch.setattr(httpx, "post", lambda url, **kw: _Bad(malformed))
    monkeypatch.setattr(skill_runner, "_provider_binaries", lambda: {})

    result = _once()

    assert result.definition_unavailable is True
    assert skill_runner._agent_def_cache == {}


class _Transport:
    """A response-like object: a definition-shaped body behind a chosen status."""

    def __init__(self, status_code, content_type=None):
        self.status_code = status_code
        self.headers = {"content-type": content_type} if content_type else {}

    def raise_for_status(self):
        return None

    def json(self):
        return REAL


@pytest.mark.parametrize(
    "answer",
    [_Transport(201), _Transport(204), _Transport(302), _Transport(404), _Transport(500),
     _Transport("200"), _Transport(200, "text/html")],
    ids=["201", "204", "302", "404", "500", "status-string", "html"],
)
def test_a_bad_bearer_result_means_zero_dispatch_and_nothing_cached(neotoma, monkeypatch, answer):
    monkeypatch.setattr(httpx, "post", lambda url, **kw: answer)

    result = _once()

    assert result.definition_unavailable is True
    assert skill_runner._agent_def_cache == {}
    assert neotoma["spawned"] == []


@pytest.mark.parametrize("answer", [(201, REAL), (302, REAL), ("200", REAL), (200,), None],
                         ids=["201", "302", "status-string", "one-element", "none"])
def test_a_bad_signed_result_means_zero_dispatch_and_nothing_cached(neotoma, monkeypatch, answer):
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: True)
    monkeypatch.setattr(al.ns, "agent_identity", lambda name, sub=None: {"key": "k"})
    monkeypatch.setattr(al.ns, "signed_request", lambda *a, **k: answer)
    before = neotoma["calls"]

    result = _once()

    assert result.definition_unavailable is True
    assert skill_runner._agent_def_cache == {}
    assert neotoma["spawned"] == []
    assert neotoma["calls"] == before, "no bearer fallback after a returned signed result"
