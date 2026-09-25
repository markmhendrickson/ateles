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

import aauth_httpsig as ahs
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


def _anthus_key(tmp_path, monkeypatch) -> None:
    """Give Anthus a signing key and point the client at it."""
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


def test_switched_on_proposals_are_signed_as_anthus(sent, monkeypatch, tmp_path):
    _anthus_key(tmp_path, monkeypatch)
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


class _FakeNeotoma:
    """A stateful stand-in: stores land as entities that later queries return."""

    def __init__(self):
        self.entities: dict[str, dict] = {}  # id -> snapshot (with entity_type)
        self.observations: dict[str, list[dict]] = {}  # id -> observations, oldest first
        self.requests: list[tuple[str, str, dict]] = []

    @staticmethod
    def _provenance(request: httpx.Request) -> dict:
        """What Neotoma records: the signed sub + key thumbprint at `software`, or neither
        for a bearer write. The real server derives agent_thumbprint from the verified
        cnf.jwk in the agent token (RFC 7638), never from the unverified sub claim — this
        fake must do the same so a test that plants a genuine signature is indistinguishable
        from the real server's provenance (PR #1274 round-3 finding)."""
        m = re.fullmatch(r'aasig=jwt;jwt="([^"]+)"', request.headers.get("signature-key", ""))
        if not m:
            return {"agent_sub": None, "attribution_tier": "anonymous"}
        claims = pyjwt.decode(m.group(1), options={"verify_signature": False})
        thumbprint = ahs.jwk_thumbprint(claims["cnf"]["jwk"])
        return {
            "agent_sub": claims["sub"],
            "attribution_tier": "software",
            "agent_thumbprint": thumbprint,
        }

    def observe(self, eid: str, fields: dict, provenance: dict) -> None:
        obs = self.observations.setdefault(eid, [])
        obs.append({"id": f"obs_{eid}_{len(obs)}", "entity_id": eid,
                    "fields": fields, "provenance": provenance})

    def plant(self, snapshot: dict, provenance: dict) -> str:
        """Put an entity on record as if some other writer had stored it."""
        eid = f"ent_{len(self.entities)}"
        self.entities[eid] = dict(snapshot)
        fields = {k: v for k, v in snapshot.items() if k != "entity_type"}
        self.observe(eid, fields, provenance)
        return eid

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}") if request.content else {}
        self.requests.append((request.method, path, body))
        if path.endswith("/observations/query"):
            obs = list(reversed(self.observations.get(body.get("entity_id"), [])))
            off, lim = body.get("offset", 0), body.get("limit", 100)
            return httpx.Response(200, json={"observations": obs[off : off + lim]})
        if request.method == "GET" and path.startswith("/entities/"):
            snap = self.entities.get(path.rsplit("/", 1)[-1])
            if snap is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"entity_type": snap["entity_type"]})
        if path.endswith("/entities/query"):
            ents = [
                {"entity_id": eid, "snapshot": dict(snap)}
                for eid, snap in self.entities.items()
                if snap["entity_type"] == body.get("entity_type")
            ]
            off, lim = body.get("offset", 0), body.get("limit", 100)
            return httpx.Response(200, json={"entities": ents[off : off + lim]})
        if path.endswith("/correct"):
            self.entities[body["entity_id"]][body["field"]] = body["value"]
            self.observe(body["entity_id"], {body["field"]: body["value"]}, self._provenance(request))
            return httpx.Response(
                200, json={"entity_id": body["entity_id"], "observation_id": "obs_c"}
            )
        stored = []
        for ent in body.get("entities", []):
            eid = f"ent_{len(self.entities)}"
            self.entities[eid] = dict(ent)
            self.observe(
                eid, {k: v for k, v in ent.items() if k != "entity_type"}, self._provenance(request)
            )
            stored.append({"entity_id": eid, "observation_id": f"obs_{eid}"})
        return httpx.Response(200, json={"entities": stored})

    def proposals(self) -> list[dict]:
        return [
            s for s in self.entities.values()
            if s["entity_type"] == "strategy_revision_proposal"
        ]


@pytest.fixture
def neotoma(monkeypatch):
    fake = _FakeNeotoma()
    transport = httpx.MockTransport(fake.handler)
    real_async = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async(
            transport=transport, **{k: v for k, v in kw.items() if k != "transport"}
        ),
    )
    monkeypatch.delenv("ATELES_SIGNED_WRITES_ANTHUS", raising=False)
    return fake


def test_a_growing_cluster_keeps_one_open_proposal(neotoma):
    """3 signals on one tick, 4 on the next, same rule: exactly one proposal."""
    asyncio.run(gz._act_on_cluster(_cluster(3), [], 3, "tok"))
    asyncio.run(gz._act_on_cluster(_cluster(4), [], 3, "tok"))

    [proposal] = neotoma.proposals()
    # The fourth signal's evidence was added to the open proposal...
    assert proposal["drift_signal_refs"] == ["ref0", "ref1", "ref2", "ref3"]
    # ...without touching what an approver approves.
    change = json.loads(proposal["proposed_change"])
    assert change["fields"]["rule"] == RULE
    assert json.loads(change["fields"]["body"])["drift_signal_refs"] == [
        "ref0", "ref1", "ref2"
    ]


def test_same_cluster_again_opens_nothing_and_writes_nothing(neotoma):
    asyncio.run(gz._act_on_cluster(_cluster(3), [], 3, "tok"))
    before = len(neotoma.requests)
    asyncio.run(gz._act_on_cluster(_cluster(3), [], 3, "tok"))
    assert len(neotoma.proposals()) == 1
    writes = [r for r in neotoma.requests[before:] if r[0] == "POST" and not r[1].endswith("/entities/query")]
    assert writes == []


def test_a_decided_proposal_is_not_open(neotoma):
    asyncio.run(gz._act_on_cluster(_cluster(3), [], 3, "tok"))
    [(eid, snap)] = [
        (k, v) for k, v in neotoma.entities.items()
        if v["entity_type"] == "strategy_revision_proposal"
    ]
    snap["status"] = "rejected"
    snap["operator_decision"] = "rejected"
    asyncio.run(gz._act_on_cluster(_cluster(4), [], 3, "tok"))
    assert len(neotoma.proposals()) == 2


def test_proposal_identity_ignores_evidence_and_spacing():
    a = gz.proposed_policy_fields(_cluster(3))
    b = gz.proposed_policy_fields(_cluster(5))
    assert a["body"] != b["body"]
    ident = lambda f: gz.proposal_identity(  # noqa: E731
        {"op": "create", "entity_type": "agent_policy", "fields": f}
    )
    assert ident(a) == ident(b)
    assert ident({**a, "rule": "  read a WRITE back   before reporting success "}) == ident(a)
    assert ident({**a, "rule": "A different rule."}) != ident(a)
    assert ident({**a, "applies_when": "on release"}) != ident(a)
    assert ident({**a, "agent_sub": "falco@ateles-swarm"}) != ident(a)


def test_unreadable_open_proposals_opens_nothing(sent, monkeypatch):
    async def fail(*a, **k):
        return None

    monkeypatch.setattr(gz, "_post", fail)
    assert asyncio.run(gz.create_policy_proposal(_cluster(3), "tok")) is None
    assert not _entities(sent, "strategy_revision_proposal")


# ── evidence goes only onto a proposal Anthus itself signed (security round 2) ──


def _planted_twin(neotoma, provenance) -> str:
    """A same-rule open proposal, written by someone else with ``provenance``."""
    fields = gz.proposed_policy_fields(_cluster(3))
    change = {"op": "create", "entity_type": "agent_policy", "fields": fields}
    return neotoma.plant(
        {
            "entity_type": "strategy_revision_proposal",
            "proposing_agent_sub": gz.PROPOSER_SUB,  # self-reported, so it proves nothing
            "target_entity_id": fields["agent_sub"],
            "target_entity_type": "agent_policy",
            "drift_signal_refs": ["ref0", "ref1", "ref2"],
            "proposed_change": gz._canonical(change),
            "operator_decision": "pending",
            "status": "pending",
        },
        provenance,
    )


BEARER_PROVENANCE = {"agent_sub": None, "attribution_tier": "anonymous"}


def test_signed_anthus_does_not_add_evidence_to_a_bearer_written_proposal(
    neotoma, monkeypatch, tmp_path
):
    _anthus_key(tmp_path, monkeypatch)
    monkeypatch.setenv("ATELES_SIGNED_WRITES_ANTHUS", "on")
    planted = _planted_twin(neotoma, BEARER_PROVENANCE)

    eid = asyncio.run(gz.create_policy_proposal(_cluster(4), "tok"))

    # No signed correction onto the planted proposal...
    assert not [r for r in neotoma.requests if r[1].endswith("/correct")]
    assert neotoma.entities[planted]["drift_signal_refs"] == ["ref0", "ref1", "ref2"]
    # ...and the genuine proposal is opened beside it, signed as Anthus.
    assert eid and eid != planted
    assert len(neotoma.proposals()) == 2
    assert neotoma.observations[eid][0]["provenance"]["agent_sub"] == gz.PROPOSER_SUB


def test_signed_anthus_adds_evidence_to_its_own_signed_proposal(neotoma, monkeypatch, tmp_path):
    _anthus_key(tmp_path, monkeypatch)
    monkeypatch.setenv("ATELES_SIGNED_WRITES_ANTHUS", "on")
    asyncio.run(gz._act_on_cluster(_cluster(3), [], 3, "tok"))
    asyncio.run(gz._act_on_cluster(_cluster(4), [], 3, "tok"))

    [proposal] = neotoma.proposals()
    assert proposal["drift_signal_refs"] == ["ref0", "ref1", "ref2", "ref3"]


def test_a_bearer_correction_of_proposed_change_makes_a_proposal_untrusted(
    neotoma, monkeypatch, tmp_path
):
    """Signed at birth, then its proposed_change rewritten on the bearer: not Anthus's any more."""
    _anthus_key(tmp_path, monkeypatch)
    monkeypatch.setenv("ATELES_SIGNED_WRITES_ANTHUS", "on")
    first = asyncio.run(gz.create_policy_proposal(_cluster(3), "tok"))
    snap = neotoma.entities[first]
    neotoma.observe(first, {"proposed_change": snap["proposed_change"]}, BEARER_PROVENANCE)

    second = asyncio.run(gz.create_policy_proposal(_cluster(4), "tok"))
    assert second != first
    assert neotoma.entities[first]["drift_signal_refs"] == ["ref0", "ref1", "ref2"]


EXPECTED_TP = "anthus-own-key-thumbprint"


def test_a_signed_evidence_correction_does_not_vouch_for_the_proposal():
    """Some signed observation on the entity is not enough: the defining fields must be signed."""
    signed = {
        "agent_sub": gz.PROPOSER_SUB,
        "attribution_tier": "software",
        "agent_thumbprint": EXPECTED_TP,
    }
    observations = [
        {"id": "o2", "fields": {"drift_signal_refs": ["r"]}, "provenance": signed},
        {"id": "o1", "fields": {"proposed_change": "{}", "target_entity_id": "x"},
         "provenance": BEARER_PROVENANCE},
    ]
    assert not gz.defining_fields_signed_by(observations, gz.PROPOSER_SUB, EXPECTED_TP)
    observations[1]["provenance"] = signed
    assert gz.defining_fields_signed_by(observations, gz.PROPOSER_SUB, EXPECTED_TP)
    # Another swarm identity is not the proposer.
    observations[1]["provenance"] = {
        "agent_sub": "corvus@ateles-swarm", "attribution_tier": "software", "agent_thumbprint": EXPECTED_TP,
    }
    assert not gz.defining_fields_signed_by(observations, gz.PROPOSER_SUB, EXPECTED_TP)
    # A provenance stored as JSON text is read the same way.
    observations[1]["provenance"] = json.dumps(signed)
    assert gz.defining_fields_signed_by(observations, gz.PROPOSER_SUB, EXPECTED_TP)
    # No observation that set proposed_change at all: refused.
    assert not gz.defining_fields_signed_by(observations[:1], gz.PROPOSER_SUB, EXPECTED_TP)


def test_sub_matches_but_thumbprint_does_not_is_not_accepted():
    """PR #1274 round-3 (Falco): agent_sub is an unverified label; a DIFFERENT key whose
    token happens to carry the proposer's sub must not vouch for the proposal's fields."""
    right_sub_wrong_key = {
        "agent_sub": gz.PROPOSER_SUB,
        "attribution_tier": "software",
        "agent_thumbprint": "a-different-agents-key-thumbprint",
    }
    observations = [
        {"id": "o1", "fields": {"proposed_change": "{}", "target_entity_id": "x"},
         "provenance": right_sub_wrong_key},
    ]
    assert not gz.defining_fields_signed_by(observations, gz.PROPOSER_SUB, EXPECTED_TP)


def test_thumbprint_missing_is_not_accepted():
    signed_no_thumbprint = {"agent_sub": gz.PROPOSER_SUB, "attribution_tier": "software"}
    observations = [
        {"id": "o1", "fields": {"proposed_change": "{}", "target_entity_id": "x"},
         "provenance": signed_no_thumbprint},
    ]
    assert not gz.defining_fields_signed_by(observations, gz.PROPOSER_SUB, EXPECTED_TP)


def test_sub_and_thumbprint_both_matching_is_accepted():
    signed = {
        "agent_sub": gz.PROPOSER_SUB,
        "attribution_tier": "software",
        "agent_thumbprint": EXPECTED_TP,
    }
    observations = [
        {"id": "o1", "fields": {"proposed_change": "{}", "target_entity_id": "x"},
         "provenance": signed},
    ]
    assert gz.defining_fields_signed_by(observations, gz.PROPOSER_SUB, EXPECTED_TP)


def test_proposal_signed_by_proposer_returns_false_for_an_empty_entity_id():
    """An empty entity_id must fail closed rather than sending entity_id: "" — Neotoma's
    /observations/query applies the entity_id filter only when it is truthy, so an empty
    id would widen the read to the caller's most recent observations generally."""
    assert asyncio.run(gz.proposal_signed_by_proposer("", "tok")) is False


def test_proposal_signed_by_proposer_returns_none_with_no_usable_key(monkeypatch, tmp_path):
    """No usable AAuth key (`_writer(bearer).thumbprint` raising `AAuthSigningError`) must
    return None — nothing opened this tick — not fall through to a sub-only check. This
    exercises the `except AAuthSigningError` branch directly, ahead of any HTTP read: an
    empty keys dir means `agent_identity` finds no `anthus.jwk.json` and `_load_signer`
    raises before `proposal_signed_by_proposer` ever calls `observations/query`."""
    monkeypatch.setattr(ns, "AAUTH_KEYS_DIR", str(tmp_path))
    monkeypatch.setenv("ATELES_SIGNED_WRITES_ANTHUS", "on")

    async def fail_if_called(path, body, bearer):
        raise AssertionError(f"observations/query must not be reached: {path}")

    monkeypatch.setattr(gz, "_post", fail_if_called)
    assert asyncio.run(gz.proposal_signed_by_proposer("some-entity-id", "tok")) is None


def test_unreadable_signer_opens_nothing(neotoma, monkeypatch, tmp_path):
    _anthus_key(tmp_path, monkeypatch)
    monkeypatch.setenv("ATELES_SIGNED_WRITES_ANTHUS", "on")
    _planted_twin(neotoma, BEARER_PROVENANCE)
    real_post = gz._post

    async def no_observations(path, body, bearer):
        return None if path == "observations/query" else await real_post(path, body, bearer)

    monkeypatch.setattr(gz, "_post", no_observations)
    assert asyncio.run(gz.create_policy_proposal(_cluster(4), "tok")) is None
    assert len(neotoma.proposals()) == 1
