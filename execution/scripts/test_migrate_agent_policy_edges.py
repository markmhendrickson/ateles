"""Tests for execution/scripts/migrate_agent_policy_edges.py (decision 114).

No test hits the real network — every Neotoma call goes through a
monkeypatched `httpx.post` (this script's own, or `agent_loader`'s, since the
script reuses `agent_loader.fetch_governs_edges` /
`agent_loader.resolve_agent_definition_id`).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import migrate_agent_policy_edges as mig  # noqa: E402
import agent_loader as al  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


def _policy_row(entity_id, agent_sub="", scope="agent", status="active"):
    return {
        "entity_id": entity_id,
        "snapshot": {
            "agent_sub": agent_sub,
            "scope": scope,
            "status": status,
            "rule": f"rule for {entity_id}",
        },
    }


def test_dry_run_classifies_rows_without_writing(monkeypatch, capsys):
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "tok")

    policy_payload = {
        "entities": [
            _policy_row("ent_migrate_me", agent_sub="corvus@ateles-swarm"),
            _policy_row("ent_already_edged", agent_sub="pavo@ateles-swarm"),
            _policy_row("ent_no_sub", agent_sub=""),
            _policy_row("ent_swarm_wide", agent_sub="", scope="swarm"),
        ]
    }
    definition_payload = {
        "entities": [{"entity_id": "ent_corvus_def", "snapshot": {"name": "corvus"}}]
    }
    relationships_payload = {
        "relationships": [
            {"source_entity_id": "ent_already_edged", "target_entity_id": "ent_pavo_def"},
        ]
    }

    calls = {"create_relationship": 0}

    def fake_post(url, json=None, **kw):
        if url.endswith("/list_relationships"):
            return _Resp(relationships_payload)
        if url.endswith("/create_relationship"):
            calls["create_relationship"] += 1
            return _Resp({"relationship_key": "should-not-be-created"})
        if url.endswith("/entities/query") and (json or {}).get("entity_type") == "agent_definition":
            return _Resp(definition_payload)
        return _Resp(policy_payload)

    monkeypatch.setattr(mig.httpx, "post", fake_post)
    monkeypatch.setattr(al.httpx, "post", fake_post)

    exit_code = mig.migrate("https://neotoma.example", apply=False)

    assert exit_code == 0
    assert calls["create_relationship"] == 0, "dry-run must never call create_relationship"
    out = capsys.readouterr().out
    assert "ent_migrate_me" in out
    assert "candidates to migrate: 1" in out
    assert "already carry a GOVERNS edge (skipped): 1" in out
    # ent_no_sub has no agent_sub and is unresolvable; ent_swarm_wide is never
    # selected at all (scope != "agent", filtered before classification).
    assert "unresolvable" in out.lower() or "no agent_sub" in out.lower()


def test_apply_creates_and_reads_back_each_edge(monkeypatch, capsys):
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "tok")

    policy_payload = {"entities": [_policy_row("ent_to_migrate", agent_sub="corvus@ateles-swarm")]}
    definition_payload = {
        "entities": [{"entity_id": "ent_corvus_def", "snapshot": {"name": "corvus"}}]
    }

    state = {"edge_created": False}

    def fake_post(url, json=None, **kw):
        if url.endswith("/create_relationship"):
            assert json["relationship_type"] == "GOVERNS"
            assert json["source_entity_id"] == "ent_to_migrate"
            assert json["target_entity_id"] == "ent_corvus_def"
            state["edge_created"] = True
            return _Resp({"relationship_key": "rk_1"})
        if url.endswith("/list_relationships"):
            if state["edge_created"]:
                return _Resp(
                    {
                        "relationships": [
                            {
                                "source_entity_id": "ent_to_migrate",
                                "target_entity_id": "ent_corvus_def",
                            }
                        ]
                    }
                )
            return _Resp({"relationships": []})
        if url.endswith("/entities/query") and (json or {}).get("entity_type") == "agent_definition":
            return _Resp(definition_payload)
        return _Resp(policy_payload)

    monkeypatch.setattr(mig.httpx, "post", fake_post)
    monkeypatch.setattr(al.httpx, "post", fake_post)

    exit_code = mig.migrate("https://neotoma.example", apply=True)

    assert exit_code == 0
    assert state["edge_created"] is True
    out = capsys.readouterr().out
    assert "Migrated 1/1" in out


def test_apply_reports_but_does_not_crash_on_unregistered_type(monkeypatch, capsys):
    """The registration blocker this script's docstring names: until an
    admitted agent_grant registers GOVERNS, create_relationship 4xxs with
    `unregistered_relationship_type`. The batch must report the failure and
    continue, never raise out of `migrate()`.
    """
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "tok")

    policy_payload = {"entities": [_policy_row("ent_blocked", agent_sub="corvus@ateles-swarm")]}
    definition_payload = {
        "entities": [{"entity_id": "ent_corvus_def", "snapshot": {"name": "corvus"}}]
    }

    def fake_post(url, json=None, **kw):
        if url.endswith("/create_relationship"):
            return _Resp(
                {"error_code": "unregistered_relationship_type", "message": "not registered"},
                status=400,
            )
        if url.endswith("/list_relationships"):
            return _Resp({"relationships": []})
        if url.endswith("/entities/query") and (json or {}).get("entity_type") == "agent_definition":
            return _Resp(definition_payload)
        return _Resp(policy_payload)

    monkeypatch.setattr(mig.httpx, "post", fake_post)
    monkeypatch.setattr(al.httpx, "post", fake_post)

    exit_code = mig.migrate("https://neotoma.example", apply=True)

    assert exit_code == 0  # a per-row failure is reported, not a crash
    out = capsys.readouterr().out
    assert "Migrated 0/1" in out


def test_missing_bearer_token_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    exit_code = mig.migrate("https://neotoma.example", apply=False)
    assert exit_code == 1
    assert "NEOTOMA_BEARER_TOKEN" in capsys.readouterr().err


def test_already_edged_row_is_never_re_migrated(monkeypatch, capsys):
    """A row that already carries a GOVERNS edge is skipped even under
    --apply — no second edge, no duplicate create_relationship call
    (idempotent re-run)."""
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "tok")

    policy_payload = {"entities": [_policy_row("ent_done", agent_sub="corvus@ateles-swarm")]}
    relationships_payload = {
        "relationships": [
            {"source_entity_id": "ent_done", "target_entity_id": "ent_corvus_def"},
        ]
    }
    calls = {"create_relationship": 0}

    def fake_post(url, json=None, **kw):
        if url.endswith("/create_relationship"):
            calls["create_relationship"] += 1
            return _Resp({"relationship_key": "rk"})
        if url.endswith("/list_relationships"):
            return _Resp(relationships_payload)
        return _Resp(policy_payload)

    monkeypatch.setattr(mig.httpx, "post", fake_post)
    monkeypatch.setattr(al.httpx, "post", fake_post)

    exit_code = mig.migrate("https://neotoma.example", apply=True)

    assert exit_code == 0
    assert calls["create_relationship"] == 0
    assert "Migrated 0/0" in capsys.readouterr().out
