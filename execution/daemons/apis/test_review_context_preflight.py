"""Synthetic review prerequisites; no network, credentials, guard or provider."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
for entry in (ROOT, ROOT / "execution/scripts", Path(__file__).parent):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import skill_runner  # noqa: E402
import dispatch_role  # noqa: E402
import harness_lens_runner as runner  # noqa: E402
from lib.daemon_runtime.agent_loader import AgentDefinition  # noqa: E402


def definition(*, tools=None, context=None):
    snapshot = {
        "name": "synthetic-reviewer", "status": "active",
        "prompt_markdown": "Unchanged canonical role.",
        "tool_allowlist": ["Read"] if tools is None else tools,
    }
    if context is not None:
        snapshot["context_entity_types"] = context
    return AgentDefinition(
        entity_id="ent_synthetic_reviewer", name=snapshot["name"],
        prompt_markdown=snapshot["prompt_markdown"],
        tool_allowlist=snapshot["tool_allowlist"],
        raw={"entity_id": "ent_synthetic_reviewer",
             "entity_type": "agent_definition", "snapshot": snapshot},
    )


@pytest.mark.parametrize("tools,context", [
    (["Read"], ["plan"]),
    (["Read", "mcp__mcpsrv_neotoma__retrieve_entities"], None),
    (["mcp__mcpsrv_neotoma__*"], None),
    (["*"], None),
])
def test_record_dependency_refuses_unqualified_transport(tools, context, monkeypatch):
    # Neither ambient nor role-labelled credentials qualify a transport.
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "synthetic-shared")
    monkeypatch.setenv("SYNTHETIC_REVIEWER_NEOTOMA_TOKEN", "synthetic-own")
    role = definition(tools=tools, context=context)
    assert "record-side admission on every read and write" in (
        skill_runner.local_review_context_error(role) or ""
    )
    assert role.tool_allowlist == tools


@pytest.mark.parametrize("context", ["not-json", {}, 1, [None], [""]])
def test_malformed_declaration_refuses(context):
    assert skill_runner.local_review_context_error(definition(context=context)) == (
        "canonical reviewer context declaration is malformed"
    )


def test_non_record_role_is_unchanged_and_operational_portfolio_is_not_a_read_floor():
    role = definition()
    role.raw["snapshot"]["operational_entity_types"] = ["issue", "plan"]
    assert skill_runner.local_review_context_error(role) is None
    assert role.prompt_markdown == "Unchanged canonical role."
    assert role.tools == ["Read"]


@pytest.mark.parametrize("entry", ["dispatch", "run_skill", "attempt"])
def test_shared_entrypoints_refuse_before_selection_or_effects(entry, monkeypatch):
    role = definition(context=["plan"])

    def forbidden(*a, **kw):
        pytest.fail("provider selection or effect preceded context preflight")

    monkeypatch.setattr(skill_runner, "_provider_binaries", forbidden)
    monkeypatch.setattr(skill_runner, "_refresh_usage_snapshot", forbidden)
    monkeypatch.setattr(dispatch_role.mechanical_first, "run_generators", forbidden)
    if entry == "dispatch":
        result = asyncio.run(dispatch_role.dispatch(
            role.name, "task", provider="codex", local_review=True,
            verified_agent_definition=role,
            work_class="regenerate_generated_files", regenerate_commands=["inert"],
        ))
    elif entry == "run_skill":
        result = asyncio.run(skill_runner.run_skill(
            role.name, "task", role=role.name, provider="codex", local_review=True,
            verified_agent_definition=role,
        ))
    else:
        result = asyncio.run(skill_runner._run_skill_once(
            role.name, "task", role=role.name, provider="codex", local_review=True,
            verified_agent_definition=role,
        ))
    assert not result.ok
    assert "record context unavailable" in result.error


@pytest.mark.parametrize("dry_run", [True, False])
def test_cli_refuses_before_worktree_headroom_guard_or_provider(dry_run, monkeypatch, tmp_path):
    role = definition(context=["plan"])
    monkeypatch.setattr(runner, "read_agent_definition", lambda name: role)

    def forbidden(*a, **kw):
        pytest.fail("review preparation preceded context preflight")

    monkeypatch.setattr(runner, "check_headroom", forbidden)
    monkeypatch.setattr(runner.Worktree, "create", forbidden)
    monkeypatch.setattr(runner.HarnessSandbox, "build", forbidden)
    monkeypatch.setattr(runner.dispatch_role, "dispatch", forbidden)
    target = runner.LensTarget(
        repo="example/project", pr=1, head="a" * 40,
        lens="arch", agent=role.name,
    )
    result = asyncio.run(runner.run_one(
        target, provider="codex", post=False, dry_run=dry_run,
        repo_worktree_name="project", scratch_root=tmp_path,
        brief_path=tmp_path / "brief", timeout=1,
    ))
    assert not result["ok"]
    assert result["no_model_call_made"] is True
    assert result["posted"] is False
    assert "record context unavailable" in result["refusal_reason"]


@pytest.mark.parametrize("raw", [None, [], {"snapshot": None}, {"snapshot": []}])
def test_malformed_snapshot_refuses(raw):
    role = definition()
    role.raw = raw
    assert skill_runner.local_review_context_error(role) == (
        "canonical reviewer context declaration is malformed"
    )


def signed_role():
    role = definition(tools=["Read", "mcp__mcpsrv_neotoma__retrieve_entities",
                             "mcp__mcpsrv_neotoma__store"])
    role.aauth_sub = role.raw["snapshot"]["aauth_sub"] = "synthetic-reviewer@example.test"
    return role


def server_config(role=None, **overrides):
    args = dict(binary=Path("/synthetic/bin/neotoma"), key_path=Path("/synthetic/keys/reviewer.jwk"),
                base_url="https://record.example.test", issuer="https://issuer.example.test", key_id="synthetic-key")
    return skill_runner.review_record_server_config(role or signed_role(), **(args | overrides))


def test_existing_signed_bridge_config_has_no_alternate_principal_or_new_gate():
    config = server_config()
    assert config["command"] == "/synthetic/bin/neotoma"
    assert config["args"] == ["mcp", "proxy", "--aauth", "--fail-closed", "--session-preflight",
                              "--downstream-url", "https://record.example.test/mcp"]
    assert config["env"]["NEOTOMA_AAUTH_SUB"] == "synthetic-reviewer@example.test"
    assert config["env"]["MCP_PROXY_BEARER_TOKEN"] == ""
    assert config["env"]["MCP_PROXY_CONNECTION_ID"] == ""
    assert config["env"]["MCP_PROXY_AUTOSTART"] == "0"
    assert config["env"]["NEOTOMA_AAUTH_AUTHORITY_OVERRIDE"] == ""
    assert "privateJwk" not in str(config)
    # A prepared valid identity config does not open the unsupported carrier.
    assert skill_runner.local_review_context_error(signed_role()) is not None


@pytest.mark.parametrize("override", [
    {"base_url": "http://record.example.test"},
    {"base_url": "https://other@record.example.test"},
    {"base_url": "https://record.example.test/path"},
    {"binary": Path("relative")}, {"key_path": Path("relative")},
    {"issuer": ""}, {"key_id": ""},
])
def test_config_refuses_unknown_binding(override):
    with pytest.raises(ValueError):
        server_config(**override)


def test_parsed_loader_subject_cannot_replace_missing_canonical_subject():
    role = signed_role()
    del role.raw["snapshot"]["aauth_sub"]
    with pytest.raises(ValueError, match="explicitly bound"):
        server_config(role)


def test_both_carriers_keep_role_tools_and_legitimate_write_tool():
    import json
    import tomllib

    role = signed_role()
    config = server_config(role)
    codex = tomllib.loads(skill_runner.review_record_config_text("codex", config, role.tools))
    server = codex["mcp_servers"]["mcpsrv_neotoma"]
    assert server["required"] is True
    assert server["enabled_tools"] == ["retrieve_entities", "store"]
    assert "disabled_tools" not in server
    cursor = json.loads(skill_runner.review_record_config_text("cursor", config, role.tools))
    assert cursor == {"mcpServers": {"mcpsrv_neotoma": config}}
    with pytest.raises(ValueError):
        skill_runner.review_record_config_text("unknown", config, role.tools)


def identity_proof():
    session = {
        "attribution": {"tier": "software", "agent_sub": "reviewer", "agent_iss": "issuer",
                        "agent_thumbprint": "synthetic-thumbprint"},
        "aauth": {"verified": True, "admitted": True, "grant_id": "ent_synthetic_grant"},
    }
    grant = {"entity_id": "ent_synthetic_grant", "entity_type": "agent_grant",
             "snapshot": {"status": "active", "match_sub": "reviewer", "match_iss": "issuer",
                          "match_thumbprint": "synthetic-thumbprint"}}
    return session, grant


def test_same_transport_identity_proof_is_positive_prerequisite_only():
    session, grant = identity_proof()
    skill_runner.validate_review_record_identity(
        session, subject="reviewer", issuer="issuer", thumbprint="synthetic-thumbprint", grant=grant)
    assert skill_runner.local_review_context_error(signed_role()) is not None


@pytest.mark.parametrize("group,field,value", [
    ("attribution", "tier", "unverified_client"),
    ("attribution", "agent_sub", "other"),
    ("attribution", "agent_iss", "other"),
    ("attribution", "agent_thumbprint", "other"),
    ("aauth", "verified", False), ("aauth", "admitted", False),
    ("aauth", "grant_id", "other"),
    ("snapshot", "status", "revoked"),
    ("snapshot", "match_sub", "other"),
    ("snapshot", "match_iss", "other"),
    ("snapshot", "match_thumbprint", "other"),
])
def test_identity_proof_refuses_wrong_or_revoked_binding(group, field, value):
    session, grant = identity_proof()
    (grant if group == "snapshot" else session)[group][field] = value
    with pytest.raises(ValueError, match="unverified"):
        skill_runner.validate_review_record_identity(
            session, subject="reviewer", issuer="issuer", thumbprint="synthetic-thumbprint", grant=grant)
