"""The generalizer proposes agent_policy rows; it never writes one (ateles#1270).

Operator ruling, 2026-09-25: Anthus writes rule PROPOSALS only; a proposal
becomes a live rule only when a different signed swarm identity approves it.
These tests intercept every HTTP request the generalizer makes, so they judge
what reaches Neotoma rather than which helper was called.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re

import generalizer as gz
import httpx
import jwt as pyjwt
import neotoma_signed as ns
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from drift import cluster_signals, parse_drift_signals

RULE = "Read a write back before reporting success."


def _cluster(n: int = 3):
    sigs = []
    for i in range(n):
        sigs.extend(
            parse_drift_signals(f"[corvus] strategy_drift_signal: {RULE}", f"ref{i}")
        )
    return cluster_signals(sigs)[0]


@pytest.fixture
def sent(monkeypatch):
    """Every request the generalizer sends, as (path, headers, json body)."""
    out: list[tuple[str, httpx.Headers, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        out.append(
            (request.url.path, request.headers, json.loads(request.content or b"{}"))
        )
        if request.url.path.endswith("/entities/query"):
            return httpx.Response(200, json={"entities": []})
        return httpx.Response(
            200, json={"entities": [{"entity_id": "ent_new", "observation_id": "obs"}]}
        )

    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(
            transport=transport, **{k: v for k, v in kw.items() if k != "transport"}
        ),
    )
    monkeypatch.delenv("ATELES_SIGNED_WRITES_ANTHUS", raising=False)
    return out


def _entities(sent, entity_type):
    return [
        (path, headers, ent)
        for path, headers, body in sent
        for ent in body.get("entities", [])
        if ent.get("entity_type") == entity_type
    ]


def _no_agent_policy_write(sent):
    assert not _entities(sent, "agent_policy"), "stored an agent_policy row"
    corrections = [
        b for p, _, b in sent if p.endswith("/correct") or p.endswith("/corrections")
    ]
    assert not [b for b in corrections if b.get("entity_type") == "agent_policy"], (
        "corrected an agent_policy row"
    )


def test_agent_local_cluster_becomes_a_proposal_not_an_agent_policy(sent):
    cluster = _cluster()
    asyncio.run(gz._act_on_cluster(cluster, [], 3, "tok"))

    _no_agent_policy_write(sent)
    proposals = _entities(sent, "strategy_revision_proposal")
    assert len(proposals) == 1
    _, _, proposal = proposals[0]
    assert proposal["target_entity_type"] == "agent_policy"
    assert proposal["target_entity_id"] == "corvus@ateles-swarm"
    assert proposal["proposing_agent_sub"] == "anthus@ateles-swarm"
    assert proposal["status"] == "pending"
    change = json.loads(proposal["proposed_change"])
    assert change["op"] == "create" and change["entity_type"] == "agent_policy"
    assert change["fields"]["rule"] == cluster.representative_text
    assert change["fields"]["rule_kind"] == "advisory"
    assert change["fields"]["agent_sub"] == "corvus@ateles-swarm"
    assert "status" not in change["fields"], "the approval decides the live status"


def test_same_cluster_twice_proposes_under_one_idempotency_key(sent):
    asyncio.run(gz._act_on_cluster(_cluster(), [], 3, "tok"))
    asyncio.run(gz._act_on_cluster(_cluster(), [], 3, "tok"))
    keys = {
        body["idempotency_key"]
        for _, _, body in sent
        if any(
            e.get("entity_type") == "strategy_revision_proposal"
            for e in body.get("entities", [])
        )
    }
    assert len(keys) == 1


def test_contradiction_proposes_suspension_not_a_status_write(sent):
    policy = {
        "_entity_id": "ent_live_policy",
        "agent_sub": "corvus@ateles-swarm",
        "rule": RULE,
        "status": "active",
        "body": gz.PolicyState(auto_generated=True).to_notes(),
    }
    signal = gz.DriftSignal(
        agent="corvus", text="Do not read writes back.", source_ref="ref9"
    )
    asyncio.run(gz.register_contradiction(policy, signal, "tok"))

    _no_agent_policy_write(sent)
    [(_, _, proposal)] = _entities(sent, "strategy_revision_proposal")
    assert proposal["target_entity_type"] == "agent_policy"
    assert proposal["target_entity_id"] == "ent_live_policy"
    change = json.loads(proposal["proposed_change"])
    assert change == {
        "op": "correct",
        "entity_type": "agent_policy",
        "entity_id": "ent_live_policy",
        "fields": {"status": "suspended"},
    }


def test_switch_unset_proposals_stay_bearer_writes(sent):
    asyncio.run(gz._act_on_cluster(_cluster(), [], 3, "tok"))
    [(_, headers, _)] = _entities(sent, "strategy_revision_proposal")
    assert headers["authorization"] == "Bearer tok"
    assert "signature" not in headers


def test_switched_on_proposals_are_signed_as_anthus(sent, monkeypatch, tmp_path):
    nums = ec.generate_private_key(ec.SECP256R1()).private_numbers()

    def b64u(i: int) -> str:
        return base64.urlsafe_b64encode(i.to_bytes(32, "big")).rstrip(b"=").decode()

    (tmp_path / "anthus.jwk.json").write_text(
        json.dumps(
            {
                "kty": "EC",
                "crv": "P-256",
                "d": b64u(nums.private_value),
                "x": b64u(nums.public_numbers.x),
                "y": b64u(nums.public_numbers.y),
                "sub": "anthus@ateles-swarm",
                "kid": "anthus-test",
            }
        )
    )
    monkeypatch.setattr(ns, "AAUTH_KEYS_DIR", str(tmp_path))
    monkeypatch.setenv("ATELES_SIGNED_WRITES_ANTHUS", "on")

    asyncio.run(gz._act_on_cluster(_cluster(), [], 3, "tok"))

    proposals = _entities(sent, "strategy_revision_proposal")
    assert proposals, "no proposal was sent"
    for _, headers, _ in proposals:
        assert "authorization" not in headers, (
            "a switched-on write still carried the bearer"
        )
        token = re.fullmatch(
            r'aasig=jwt;jwt="([^"]+)"', headers["signature-key"]
        ).group(1)
        assert (
            pyjwt.decode(token, options={"verify_signature": False})["sub"]
            == "anthus@ateles-swarm"
        )
