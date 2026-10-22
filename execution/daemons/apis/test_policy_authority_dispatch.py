"""An unreadable governing-policy corpus must stop before provider/tool effects.

The real loader and runner are exercised together; only transport and effects
are replaced. A verified empty corpus is a separate positive control.
"""

import asyncio
import copy

import httpx
import pytest

import skill_runner
from lib.daemon_runtime import agent_loader as al


POLICIES = {
    "entities": [
        {
            "entity_id": "ent_policy",
            "snapshot": {
                "status": "active",
                "scope": "agent",
                "rule_kind": "mandatory",
                "rule": "Keep the required policy canary.",
            },
        }
    ]
}
EDGES = {
    "relationships": [
        {"source_entity_id": "ent_policy", "target_entity_id": "ent_role"}
    ]
}


@pytest.fixture
def admission(monkeypatch):
    role = al.AgentDefinition(
        entity_id="ent_role",
        name="pavo",
        status="active",
        prompt_markdown="# Pavo",
        tool_allowlist=["Read"],
    )
    state = {"query": POLICIES, "edges": EDGES, "effects": []}
    monkeypatch.setattr(skill_runner, "_load_agent_def", lambda name: role)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: False)

    def transport(self, method, url, body=None, deadline=None):
        if url.endswith("/list_relationships"):
            value = state["edges"]
        elif body.get("entity_type") == "agent_policy":
            value = state["query"]
        else:
            return {
                "entities": [{"entity_id": "ent_role", "snapshot": {"name": "pavo"}}]
            }
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(al.AgentLoader, "_neotoma", transport)

    class Response:
        def __init__(self, value):
            self.value = value

        def raise_for_status(self):
            pass

        def json(self):
            return self.value

    # The red-proof run also exercises the pre-fix direct HTTP edge reader;
    # keep that transport deterministic and entirely off the network.
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: Response(transport(None, "POST", url, kw.get("json"))),
    )
    monkeypatch.setattr(al, "resolve_agent_definition_id", lambda *a, **kw: "ent_role")

    def provider_effect():
        state["effects"].append("provider")
        return {}

    async def child_effect(*a, **kw):
        state["effects"].append("child")
        raise AssertionError("policy failure must not reach a child/tool")

    monkeypatch.setattr(skill_runner, "_provider_binaries", provider_effect)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", child_effect)
    return state


def once(provider="codex"):
    return asyncio.run(
        skill_runner._run_skill_once("pavo", "task", role="pavo", provider=provider)
    )


@pytest.mark.parametrize("stage", ["query", "edges"])
@pytest.mark.parametrize("provider", ["claude", "codex", "cursor", "claude-local"])
def test_authority_timeout_prevents_all_provider_and_child_effects(
    admission, stage, provider
):
    admission[stage] = httpx.ReadTimeout("private transport detail")
    result = once(provider)
    assert not result.ok
    assert admission["effects"] == [], "no provider, runner or tool may be invoked"
    assert "agent_policy unavailable" in result.error
    assert "private transport detail" not in result.error


@pytest.mark.parametrize(
    "answer",
    [
        None,
        {},
        {"error": "failed", "entities": []},
        {"entities": [], "has_more": True},
        {"entities": [], "total": 1},
        {"entities": ["malformed"]},
    ],
)
def test_malformed_or_partial_policy_response_is_not_verified_absence(
    admission, answer
):
    admission["query"] = answer
    result = once()
    assert not result.ok
    assert admission["effects"] == []
    assert "agent_policy unavailable" in result.error


def test_verified_empty_policy_read_continues_to_provider_admission(admission):
    admission["query"] = {"entities": [], "total": 0}
    result = once()
    assert admission["effects"] == ["provider"]
    assert "binary unavailable" in result.error


@pytest.mark.parametrize(
    "answer",
    [
        None,
        {},
        {"relationships": [], "has_more": True},
        {"relationships": [], "total": 1},
        {"relationships": ["malformed"]},
        {"relationships": [{"source_entity_id": "ent_policy"}]},
    ],
)
def test_unreadable_edge_map_cannot_remove_or_broaden_policies(admission, answer):
    admission["edges"] = answer
    result = once()
    assert admission["effects"] == []
    assert "agent_policy unavailable" in result.error


def test_no_configured_token_does_not_skip_policy_authority(admission, monkeypatch):
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "")
    admission["query"] = httpx.ReadTimeout("open-mode store unavailable")
    result = once()
    assert admission["effects"] == []
    assert "agent_policy unavailable" in result.error


@pytest.mark.parametrize("rule", [None, {}, {"text": "unusable rule"}, [], 7, False, "", "  ", "<!-- tier: A -->"])
def test_bound_malformed_rule_text_refuses_before_provider_effects(admission, rule):
    admission["query"] = copy.deepcopy(POLICIES)
    admission["query"]["entities"][0]["snapshot"]["rule"] = rule
    result = once()
    assert admission["effects"] == []
    assert result.policy_unavailable
    assert "content, bad_response" in result.error


def test_description_does_not_substitute_for_missing_governing_rule(admission):
    admission["query"] = copy.deepcopy(POLICIES)
    admission["query"]["entities"][0]["snapshot"].update(rule=None, description="Metadata only")
    result = once()
    assert result.policy_unavailable
    assert admission["effects"] == []


def test_malformed_rule_for_a_different_agent_does_not_block_this_role(admission):
    admission["query"] = copy.deepcopy(POLICIES)
    admission["query"]["entities"][0]["snapshot"]["rule"] = None
    admission["edges"] = {"relationships": [{"source_entity_id": "ent_policy",
                                             "target_entity_id": "ent_other"}]}
    result = once()
    assert not result.policy_unavailable
    assert admission["effects"] == ["provider"]


def test_unresolved_policy_principal_is_not_a_no_policy_answer(admission, monkeypatch):
    monkeypatch.setattr(al, "resolve_agent_definition_id", lambda *a, **kw: None)
    with pytest.raises(al.AgentPolicyUnavailable, match="definition"):
        skill_runner._load_active_policy_prompt("pavo")


def test_policy_recovery_is_read_again_and_carries_the_rule(admission):
    admission["query"] = httpx.ReadTimeout("unavailable")
    first = once()
    assert admission["effects"] == []
    assert not first.ok
    admission["query"] = POLICIES
    prompt = skill_runner._load_active_policy_prompt("pavo")
    assert "Keep the required policy canary." in prompt


@pytest.mark.parametrize("stage", ["query", "edges"])
def test_failed_policy_read_cannot_build_or_launch_a_provider(
    admission, stage, monkeypatch, tmp_path
):
    admission[stage] = httpx.ReadTimeout("private error")
    skill = tmp_path / ".claude" / "skills" / "pavo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Do the task.")
    monkeypatch.setattr(skill_runner, "ATELES_REPO", tmp_path)
    monkeypatch.setattr(
        skill_runner, "_provider_binaries", lambda: {"claude": "/bin/true"}
    )
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://record.example")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: None)

    def command_effect(*args, **kwargs):
        admission["effects"].append("runner command")
        return ["/bin/true"], None

    monkeypatch.setattr(skill_runner, "_provider_command", command_effect)
    result = once("claude")
    assert not result.ok
    assert admission["effects"] == []
    assert "agent_policy unavailable" in result.error


def test_failed_policy_read_stops_failover_without_cooling(monkeypatch):
    async def attempt(provider, **kwargs):
        return skill_runner.SkillResult(
            "pavo",
            False,
            None,
            "",
            "",
            error="agent_policy unavailable: timeout",
            provider=provider,
            policy_unavailable=True,
        )

    attempts = []

    async def tracked(provider, **kwargs):
        attempts.append(provider)
        return await attempt(provider, **kwargs)

    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "codex,claude")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"codex":1.0,"claude":1.0}')
    cooled = []
    monkeypatch.setattr(skill_runner, "cool_down", lambda *a, **kw: cooled.append(a))
    result = asyncio.run(
        skill_runner._run_provider_attempts(
            "pavo",
            tracked,
            binaries={"codex": "/bin/true", "claude": "/bin/true"},
            preferred_provider="codex",
        )
    )
    assert result.policy_unavailable
    assert attempts == ["codex"]
    assert cooled == []
