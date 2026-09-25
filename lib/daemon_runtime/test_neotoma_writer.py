"""Tests for NeotomaWriter, the signed Neotoma write client (ateles#1270 step 2a).

What is pinned:

- a signed write round-trips: the exact bytes sent verify against the key the
  signature carries (always on), and against Neotoma's real verifier,
  ``@hellocoop/httpsig``, when ``NEOTOMA_HTTPSIG_MODULE`` points at it (the same
  opt-in ``test_neotoma_aauth_contract.py`` uses);
- a governance write never reaches the server with the bearer token, whatever
  the daemon's switch says, including when signing fails or the server refuses
  the signature;
- non-governance writes follow ``ATELES_SIGNED_WRITES_<DAEMON>``, defaulting
  to today's bearer write;
- the governance set covers every governance type the design names;
- the read-back helper passes only an observation signed as the expected sub.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils

import aauth_httpsig as ahs
import neotoma_signed as ns

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = "https://neotoma.example"
ANTHUS = "anthus@ateles-swarm"


# ── fixtures ────────────────────────────────────────────────────────────────


def _b64u(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()


def _write_jwk(keys_dir: Path, agent: str, sub: str | None = None) -> dict:
    nums = ec.generate_private_key(ec.SECP256R1()).private_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "d": _b64u(nums.private_value),
        "x": _b64u(nums.public_numbers.x),
        "y": _b64u(nums.public_numbers.y),
        "sub": sub or f"{agent}@ateles-swarm",
        "kid": f"{agent}-test-kid",
    }
    keys_dir.mkdir(parents=True, exist_ok=True)
    (keys_dir / f"{agent}.jwk.json").write_text(json.dumps(jwk))
    return jwk


class _Recorder:
    """Routes every httpx request through a handler and records it."""

    def __init__(self, respond):
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


def _ok_store(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"entities": [{"entity_id": "ent_1", "observation_id": "obs_1"}]}
    )


@pytest.fixture
def http(monkeypatch):
    """Install a MockTransport on every httpx client; returns a factory."""
    real_async, real_sync = httpx.AsyncClient, httpx.Client

    def install(respond=_ok_store) -> _Recorder:
        rec = _Recorder(respond)
        transport = httpx.MockTransport(rec.handler)
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: real_async(
                transport=transport, **{k: v for k, v in kw.items() if k != "transport"}
            ),
        )
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kw: real_sync(
                transport=transport, **{k: v for k, v in kw.items() if k != "transport"}
            ),
        )
        return rec

    return install


def _writer(
    tmp_path: Path, *, mode, with_key: bool = True, bearer: str = "tok"
) -> ns.NeotomaWriter:
    keys = tmp_path / "keys"
    keys.mkdir(exist_ok=True)
    if with_key:
        _write_jwk(keys, "anthus")
    return ns.NeotomaWriter(
        "anthus", mode=mode, base_url=BASE, bearer=bearer, keys_dir=keys
    )


def _policy_store_body() -> dict:
    return {
        "entities": [
            {
                "entity_type": "agent_policy",
                "rule": "x",
                "agent_sub": "corvus@ateles-swarm",
            }
        ],
        "idempotency_key": "k1",
    }


def _report_store_body() -> dict:
    return {
        "entities": [{"entity_type": "daemon_report", "message": "hi"}],
        "idempotency_key": "k2",
    }


# ── signing round-trip ──────────────────────────────────────────────────────


def _verify_locally(request: httpx.Request) -> dict:
    """Rebuild the RFC 9421 base from what was SENT and verify it, then the JWT.

    Independent of the signer's own code path: the digest is recomputed from the
    request's bytes, the base from its headers, and the key comes out of the
    aa-agent+jwt the request carries.
    """
    body = request.content
    digest = (
        "sha-256=:"
        + base64.b64encode(__import__("hashlib").sha256(body).digest()).decode()
        + ":"
    )
    assert request.headers["content-digest"] == digest, (
        "digest does not cover the sent bytes"
    )

    sig_input = request.headers["signature-input"]
    m = re.fullmatch(r"aasig=\(([^)]*)\);created=(\d+)", sig_input)
    assert m, sig_input
    components = re.findall(r'"([^"]+)"', m.group(1))
    values = {
        "@method": request.method,
        "@authority": request.url.netloc.decode(),
        "@path": request.url.path,
    }
    lines = []
    for c in components:
        lines.append(f'"{c}": {values[c] if c.startswith("@") else request.headers[c]}')
    lines.append(f'"@signature-params": ({m.group(1)});created={m.group(2)}')
    base = "\n".join(lines).encode()

    token = re.fullmatch(
        r'aasig=jwt;jwt="([^"]+)"', request.headers["signature-key"]
    ).group(1)
    claims = pyjwt.decode(token, options={"verify_signature": False})
    jwk = claims["cnf"]["jwk"]
    pub = ec.EllipticCurvePublicNumbers(
        int.from_bytes(base64.urlsafe_b64decode(jwk["x"] + "=="), "big"),
        int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + "=="), "big"),
        ec.SECP256R1(),
    ).public_key()
    raw = base64.b64decode(
        re.fullmatch(r"aasig=:(.+):", request.headers["signature"]).group(1)
    )
    der = asym_utils.encode_dss_signature(
        int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    )
    pub.verify(der, base, ec.ECDSA(hashes.SHA256()))  # raises on mismatch
    pyjwt.decode(token, key=pub, algorithms=["ES256"], options={"verify_aud": False})
    assert {"@method", "@authority", "@path", "content-digest", "signature-key"} <= set(
        components
    )
    return claims


def test_signed_store_round_trips_and_carries_no_bearer(tmp_path, http):
    rec = http()
    w = _writer(tmp_path, mode=ns.SigningMode.ON)
    result = asyncio.run(
        w.astore(
            [{"entity_type": "daemon_report", "message": "hi"}], idempotency_key="k"
        )
    )
    assert result.signed and result.sub == ANTHUS
    [req] = rec.requests
    assert "authorization" not in req.headers
    assert req.headers["x-agent-label"] == ANTHUS
    claims = _verify_locally(req)
    assert claims["sub"] == ANTHUS
    assert json.loads(req.content)["entities"][0]["entity_type"] == "daemon_report"


def _real_verify(module: Path, request: dict) -> dict:
    script = r"""
const fs = require("fs");
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const { verify } = require(payload.module);
verify(payload.request, { strictAAuth: true })
  .then((r) => process.stdout.write(JSON.stringify({
    verified: r.verified, sub: r.jwt && r.jwt.payload && r.jwt.payload.sub,
    thumbprint: r.thumbprint, error: r.error || null })))
  .catch((e) => process.stdout.write(JSON.stringify({ verified: false, error: String(e) })));
"""
    done = subprocess.run(
        ["node", "-e", script],
        input=json.dumps({"module": str(module), "request": request}),
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return json.loads(done.stdout)


def test_signed_write_verifies_against_real_neotoma_verifier(tmp_path, http):
    module_value = os.environ.get("NEOTOMA_HTTPSIG_MODULE", "").strip()
    module = Path(module_value) if module_value else None
    if module is None or not (module / "package.json").is_file():
        pytest.skip("set NEOTOMA_HTTPSIG_MODULE to run the real verifier contract")
    rec = http()
    w = _writer(tmp_path, mode=ns.SigningMode.OFF)  # governance: signed regardless
    asyncio.run(
        w.acorrect("agent_policy", "ent_p", "status", "retired", idempotency_key="k")
    )
    [req] = rec.requests
    assert "authorization" not in req.headers
    wire = {
        "method": req.method,
        "authority": req.url.netloc.decode(),
        "path": req.url.path,
        "query": "",
        "headers": dict(req.headers),
        "body": req.content.decode(),
    }
    verified = _real_verify(module, wire)
    assert verified["verified"] is True, verified
    assert verified["sub"] == ANTHUS
    assert verified["thumbprint"] == w._load_signer().thumbprint
    assert (
        _real_verify(module, dict(wire, body=wire["body"] + " "))["verified"] is False
    )


# ── governance writes never fall back to the bearer ────────────────────────


@pytest.mark.parametrize("mode", list(ns.SigningMode))
@pytest.mark.parametrize("entity_type", sorted(ns.GOVERNANCE_ENTITY_TYPES))
def test_governance_store_without_a_key_is_refused_in_every_mode(
    tmp_path, http, mode, entity_type
):
    rec = http()
    w = _writer(tmp_path, mode=mode, with_key=False)
    with pytest.raises(ns.SignedWriteError):
        asyncio.run(
            w.astore([{"entity_type": entity_type, "x": 1}], idempotency_key="k")
        )
    assert rec.requests == [], "a governance write reached the server unsigned"


@pytest.mark.parametrize("mode", list(ns.SigningMode))
def test_governance_correct_without_a_key_is_refused_sync(tmp_path, http, mode):
    rec = http()
    w = _writer(tmp_path, mode=mode, with_key=False)
    with pytest.raises(ns.SignedWriteError):
        w.correct("agent_policy", "ent_p", "status", "active", idempotency_key="k")
    assert rec.requests == []


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("mode", list(ns.SigningMode))
def test_governance_write_refused_by_server_is_not_retried_with_bearer(
    tmp_path, http, mode, status
):
    rec = http(lambda r: httpx.Response(status, json={"error_code": "AAUTH_REQUIRED"}))
    w = _writer(tmp_path, mode=mode)
    with pytest.raises(ns.SignedWriteError):
        asyncio.run(w.apost("store", _policy_store_body()))
    assert len(rec.requests) == 1
    assert all("authorization" not in r.headers for r in rec.requests)


def _typed_entities(types: dict[str, str], default=_ok_store):
    """A responder whose ``GET /entities/{id}`` reports ``types[id]`` (404 if absent)."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/entities/"):
            eid = request.url.path.rsplit("/", 1)[-1]
            if eid in types:
                return httpx.Response(200, json={"id": eid, "entity_type": types[eid]})
            return httpx.Response(404, json={"error": "Entity not found"})
        return default(request)

    return respond


def _posts(rec) -> list[httpx.Request]:
    return [r for r in rec.requests if r.method == "POST"]


def test_governance_field_on_an_ordinary_type_must_be_signed(tmp_path, http):
    rec = http(_typed_entities({"ent_i": "issue"}))
    w = _writer(tmp_path, mode=ns.SigningMode.OFF, with_key=False)
    with pytest.raises(ns.SignedWriteError):
        w.correct("issue", "ent_i", "gate_status", "{}", idempotency_key="k")  # vocab-ok: retired name
    with pytest.raises(ns.SignedWriteError):
        w.store(
            [{"entity_type": "issue", "title": "t", "gate_status": "{}"}],  # vocab-ok: retired name
            idempotency_key="k",
        )
    assert rec.requests == []
    # ...while another field of the same type stays on the daemon's switch.
    w.correct("issue", "ent_i", "title", "t", idempotency_key="k")
    assert rec.requests[-1].headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize(
    "body",
    [
        {"entities": [{"message": "no type"}], "idempotency_key": "k"},
        {"entities": [], "idempotency_key": "k"},
        {"entity_id": "ent_x", "field": "status", "value": "v"},
        {"relationships": [{"relationship_type": "REFERS_TO"}]},
    ],
    ids=["entity-without-type", "no-entities", "correct-without-type", "unknown-shape"],
)
def test_a_write_whose_type_cannot_be_read_is_treated_as_governance(
    tmp_path, http, body
):
    rec = http()
    w = _writer(tmp_path, mode=ns.SigningMode.SHADOW, with_key=False)
    with pytest.raises(ns.SignedWriteError):
        w.post("store", body)
    assert rec.requests == []


def test_a_mixed_store_is_governance_if_any_entity_is(tmp_path, http):
    rec = http()
    w = _writer(tmp_path, mode=ns.SigningMode.SHADOW, with_key=False)
    body = {
        "entities": [{"entity_type": "daemon_report"}, {"entity_type": "agent_grant"}],
        "idempotency_key": "k",
    }
    with pytest.raises(ns.SignedWriteError):
        w.post("store", body)
    assert rec.requests == []


# ── non-governance writes follow the per-daemon switch ─────────────────────


def test_switch_unset_keeps_todays_bearer_write(tmp_path, http, monkeypatch):
    monkeypatch.delenv("ATELES_SIGNED_WRITES_ANTHUS", raising=False)
    rec = http()
    keys = tmp_path / "keys"
    _write_jwk(keys, "anthus")
    w = ns.NeotomaWriter("anthus", base_url=BASE, bearer="tok", keys_dir=keys)
    assert w.mode is ns.SigningMode.OFF
    result = w.post("store", _report_store_body())
    [req] = rec.requests
    assert req.headers["authorization"] == "Bearer tok"
    assert "signature" not in req.headers
    assert result.signed is False and result.sub is None


def test_shadow_falls_back_to_bearer_when_it_cannot_sign(tmp_path, http, caplog):
    rec = http()
    w = _writer(tmp_path, mode=ns.SigningMode.SHADOW, with_key=False)
    with caplog.at_level("ERROR"):
        result = w.post("store", _report_store_body())
    [req] = rec.requests
    assert req.headers["authorization"] == "Bearer tok"
    assert result.signed is False
    assert any("falling back to the bearer" in r.message for r in caplog.records)


def test_shadow_falls_back_when_the_server_refuses_the_signature(tmp_path, http):
    rec = http(
        lambda r: (
            httpx.Response(403, json={}) if "signature" in r.headers else _ok_store(r)
        )
    )
    w = _writer(tmp_path, mode=ns.SigningMode.SHADOW)
    result = asyncio.run(w.apost("store", _report_store_body()))
    assert [
        ("signature" in r.headers, "authorization" in r.headers) for r in rec.requests
    ] == [
        (True, False),
        (False, True),
    ]
    assert result.signed is False


def test_on_refuses_when_it_cannot_sign(tmp_path, http):
    rec = http()
    w = _writer(tmp_path, mode=ns.SigningMode.ON, with_key=False)
    with pytest.raises(ns.SignedWriteError):
        w.post("store", _report_store_body())
    assert rec.requests == []


def test_on_refuses_when_the_server_refuses_the_signature(tmp_path, http):
    rec = http(lambda r: httpx.Response(401, json={}))
    w = _writer(tmp_path, mode=ns.SigningMode.ON)
    with pytest.raises(ns.SignedWriteError):
        w.post("store", _report_store_body())
    assert len(rec.requests) == 1 and "authorization" not in rec.requests[0].headers


def test_key_whose_sub_is_another_agent_is_refused(tmp_path, http):
    rec = http()
    keys = tmp_path / "keys"
    _write_jwk(keys, "anthus", sub="apis@ateles-swarm")  # right file, wrong identity
    w = ns.NeotomaWriter(
        "anthus", mode=ns.SigningMode.ON, base_url=BASE, bearer="tok", keys_dir=keys
    )
    with pytest.raises(ns.SignedWriteError):
        w.post("store", _report_store_body())
    assert rec.requests == []


def test_ambient_aauth_sub_cannot_swap_the_identity(tmp_path, http, monkeypatch):
    monkeypatch.setenv("NEOTOMA_AAUTH_SUB", "apis@ateles-swarm")
    rec = http()
    w = _writer(tmp_path, mode=ns.SigningMode.ON)
    w.post("store", _report_store_body())
    assert rec.requests[0].headers["x-agent-label"] == ANTHUS
    assert _verify_locally(rec.requests[0])["sub"] == ANTHUS


@pytest.mark.parametrize(
    "raw, mode",
    [
        (None, ns.SigningMode.OFF),
        ("", ns.SigningMode.OFF),
        ("off", ns.SigningMode.OFF),
        ("shadow", ns.SigningMode.SHADOW),
        ("SHADOW", ns.SigningMode.SHADOW),
        ("on", ns.SigningMode.ON),
        ("1", ns.SigningMode.ON),
        ("onn", ns.SigningMode.ON),  # a typo takes the restrictive branch
    ],
)
def test_signing_mode_parsing(raw, mode):
    env = {} if raw is None else {"ATELES_SIGNED_WRITES_ANTHUS": raw}
    assert ns.signing_mode("anthus", env) is mode


def test_switch_name_per_daemon():
    assert ns.signed_writes_env_var("anthus") == "ATELES_SIGNED_WRITES_ANTHUS"
    assert (
        ns.signed_writes_env_var("neotoma-agent")
        == "ATELES_SIGNED_WRITES_NEOTOMA_AGENT"
    )


# ── the governance set covers the design's ──────────────────────────────────

# Design name → the name the live record still uses (docs/foundation/migration.md).
_LIVE_NAME = {
    "agent": "agent_definition",
    "workflow": "workflow_definition",  # vocab-ok: retired name the live record still uses
    "action_policy": "execution_policy",  # vocab-ok: retired name the live record still uses
}


def test_governance_set_covers_every_design_governance_type():
    sys.path.insert(0, str(REPO_ROOT / "execution" / "scripts"))
    try:
        import render_data_model
    finally:
        sys.path.pop(0)
    suite = (REPO_ROOT / "docs" / "foundation" / "conformance_suite.md").read_text(
        encoding="utf-8"
    )
    design = render_data_model.governance_types(suite)
    assert design, "the design's governance enumeration was not found"
    for t in design:
        assert t in ns.GOVERNANCE_ENTITY_TYPES, t
        if t in _LIVE_NAME:
            assert _LIVE_NAME[t] in ns.GOVERNANCE_ENTITY_TYPES, _LIVE_NAME[t]
    for t in ("task_policy", "checkpoint_brief"):  # vocab-ok: checkpoint_brief is retired, named by the ateles#1270 inventory
        assert t in ns.GOVERNANCE_ENTITY_TYPES


# ── read-back ───────────────────────────────────────────────────────────────


def _thumbprint_of(jwk: dict) -> str:
    return ahs.jwk_thumbprint(ahs.public_part_of(jwk))


def _obs_response(sub, tier, thumbprint=None, obs_id="obs_1"):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/observations/query"):
            prov = {"agent_sub": sub, "attribution_tier": tier}
            if thumbprint is not None:
                prov["agent_thumbprint"] = thumbprint
            return httpx.Response(
                200,
                json={
                    "observations": [
                        {"id": "obs_other", "provenance": {}},
                        {"id": obs_id, "provenance": prov},
                    ]
                },
            )
        return _ok_store(request)

    return respond


def test_confirm_attribution_passes_for_the_expected_signed_sub_and_thumbprint(tmp_path, http):
    jwk = _write_jwk(tmp_path / "keys", "anthus")
    rec = http(_obs_response(ANTHUS, "software", _thumbprint_of(jwk)))
    w = ns.NeotomaWriter("anthus", mode=ns.SigningMode.ON, base_url=BASE, bearer="tok", keys_dir=tmp_path / "keys")
    result = w.post("store", _report_store_body())
    check = w.confirm_attribution(result)
    assert check.ok, check.reason
    readback = rec.requests[-1]
    assert json.loads(readback.content)["entity_id"] == "ent_1"


@pytest.mark.parametrize(
    "sub, tier, use_thumbprint",
    [
        ("apis@ateles-swarm", "software", True),
        (ANTHUS, "unverified_client", True),
        (None, None, True),
    ],
    ids=["other-sub", "unverified-tier", "no-provenance"],
)
def test_confirm_attribution_fails_otherwise(tmp_path, http, sub, tier, use_thumbprint):
    keys = tmp_path / "keys"
    jwk = _write_jwk(keys, "anthus")
    thumbprint = _thumbprint_of(jwk) if use_thumbprint else None
    http(_obs_response(sub, tier, thumbprint))
    w = ns.NeotomaWriter("anthus", mode=ns.SigningMode.ON, base_url=BASE, bearer="tok", keys_dir=keys)
    result = w.post("store", _report_store_body())
    assert w.confirm_attribution(result).ok is False
    assert asyncio.run(w.aconfirm_attribution(result)).ok is False


def test_confirm_attribution_fails_when_sub_matches_but_thumbprint_does_not(tmp_path, http):
    """Falco PR #1274 round-3 finding: agent_sub is an unverified label; a signature by a
    DIFFERENT key whose token happens to carry the same sub must not pass."""
    keys = tmp_path / "keys"
    _write_jwk(keys, "anthus")
    other_jwk = _write_jwk(tmp_path / "other_keys", "someone_else", sub=ANTHUS)
    http(_obs_response(ANTHUS, "software", _thumbprint_of(other_jwk)))
    w = ns.NeotomaWriter("anthus", mode=ns.SigningMode.ON, base_url=BASE, bearer="tok", keys_dir=keys)
    result = w.post("store", _report_store_body())
    check = w.confirm_attribution(result)
    assert check.ok is False
    assert "agent_thumbprint" in check.reason


def test_confirm_attribution_fails_when_thumbprint_is_missing(tmp_path, http):
    keys = tmp_path / "keys"
    _write_jwk(keys, "anthus")
    http(_obs_response(ANTHUS, "software", thumbprint=None))
    w = ns.NeotomaWriter("anthus", mode=ns.SigningMode.ON, base_url=BASE, bearer="tok", keys_dir=keys)
    result = w.post("store", _report_store_body())
    check = w.confirm_attribution(result)
    assert check.ok is False
    assert "agent_thumbprint" in check.reason


def test_confirm_attribution_fails_when_the_observation_is_missing(tmp_path, http):
    jwk = _write_jwk(tmp_path / "keys", "anthus")
    http(_obs_response(ANTHUS, "software", _thumbprint_of(jwk), obs_id="obs_elsewhere"))
    w = ns.NeotomaWriter("anthus", mode=ns.SigningMode.ON, base_url=BASE, bearer="tok", keys_dir=tmp_path / "keys")
    result = w.post("store", _report_store_body())
    check = w.confirm_attribution(result)
    assert check.ok is False and "not found" in check.reason


def test_confirm_attribution_fails_for_a_bearer_write(tmp_path, http):
    jwk = _write_jwk(tmp_path / "keys", "anthus")
    http(_obs_response(ANTHUS, "software", _thumbprint_of(jwk)))
    w = ns.NeotomaWriter("anthus", mode=ns.SigningMode.OFF, base_url=BASE, bearer="tok", keys_dir=tmp_path / "keys")
    result = w.post("store", _report_store_body())
    assert result.signed is False
    assert w.confirm_attribution(result).ok is False


# Neotoma origin/main reports a grant capability denial as HTTP 500, observed
# live against the hosted instance on 2026-09-25 with Anthus's current grant.
_CAPABILITY_DENIAL = {
    "error_code": "DB_QUERY_FAILED",
    "message": 'Agent "anthus" is not permitted to store entity_type "strategy_revision_proposal".',
}


def test_capability_denial_reported_as_500_is_a_refusal_on(tmp_path, http):
    rec = http(lambda r: httpx.Response(500, json=_CAPABILITY_DENIAL))
    w = _writer(tmp_path, mode=ns.SigningMode.ON)
    with pytest.raises(ns.SignedWriteError):
        w.post("store", _report_store_body())
    assert len(rec.requests) == 1 and "authorization" not in rec.requests[0].headers


def test_capability_denial_reported_as_500_falls_back_in_shadow(tmp_path, http):
    rec = http(
        lambda r: (
            httpx.Response(500, json=_CAPABILITY_DENIAL)
            if "signature" in r.headers
            else _ok_store(r)
        )
    )
    w = _writer(tmp_path, mode=ns.SigningMode.SHADOW)
    result = w.post("store", _report_store_body())
    assert result.signed is False
    assert [("authorization" in r.headers) for r in rec.requests] == [False, True]


def test_an_ordinary_server_error_is_not_mistaken_for_a_refusal(tmp_path, http):
    rec = http(
        lambda r: httpx.Response(
            500, json={"error_code": "DB_QUERY_FAILED", "message": "timeout"}
        )
    )
    w = _writer(tmp_path, mode=ns.SigningMode.SHADOW)
    with pytest.raises(ns.NeotomaWriteError) as exc:
        w.post("store", _report_store_body())
    assert not isinstance(exc.value, ns.SignedWriteError)
    assert len(rec.requests) == 1, "an ordinary 500 must not be retried with the bearer"


# ── classification hardening (PR #1274 review round) ─────────────────────────

GOVERNANCE_VARIANTS = [
    "Agent_Policy",
    "AGENT_POLICY",
    "agent_policies",
    "agent_grants",
    "Agent_Grant",
    " agent-policy ",
    "agent policy",
    "agent_policy\u200b",
    "agentpolicy",
    "agent_p\u00f3licy",
    "Workflows",
    "swarm_rosters",
    "task_policies",
]


@pytest.mark.parametrize("variant", GOVERNANCE_VARIANTS)
def test_variant_spellings_of_a_governance_type_are_governance(variant):
    assert ns.is_governance_write(variant)


@pytest.mark.parametrize("variant", GOVERNANCE_VARIANTS)
def test_variant_spelling_never_reaches_the_bearer(tmp_path, http, variant):
    rec = http()
    w = _writer(tmp_path, mode=ns.SigningMode.OFF, with_key=False)
    with pytest.raises(ns.SignedWriteError):
        w.store([{"entity_type": variant, "rule": "x"}], idempotency_key="k")
    assert rec.requests == []


def test_variant_spelling_of_a_governance_field_is_governance():
    [(etype, fld)] = ns.GOVERNANCE_FIELDS
    assert ns.is_governance_write(etype.title(), fld)
    assert ns.is_governance_write(f"{etype}s", fld.replace("_", "-").title())
    assert not ns.is_governance_write(etype.title(), "title")


@pytest.mark.parametrize(
    "ordinary",
    [
        "task",
        "daemon_report",
        "strategy_revision_proposal",
        "strategy_drift_signal",
        "activity_log",
        "escalation",
        "issue",
        "agent_strategy",
        "agent_message",
    ],
)
def test_ordinary_types_stay_ordinary(ordinary):
    assert not ns.is_governance_write(ordinary)


def test_entity_types_cannot_declassify_a_governance_body(tmp_path, http):
    rec = http()
    w = _writer(tmp_path, mode=ns.SigningMode.OFF, with_key=False)
    with pytest.raises(ns.SignedWriteError):
        w.post("store", _policy_store_body(), entity_types=["task"])
    with pytest.raises(ns.SignedWriteError):
        asyncio.run(w.apost("store", _policy_store_body(), entity_types=["task"]))
    with pytest.raises(ns.SignedWriteError):
        w.post("store", _policy_store_body(), entity_types=[])
    assert rec.requests == []


def test_entity_types_can_add_governance_to_an_ordinary_body(tmp_path, http):
    rec = http()
    w = _writer(tmp_path, mode=ns.SigningMode.OFF, with_key=False)
    with pytest.raises(ns.SignedWriteError):
        w.post("store", _report_store_body(), entity_types=["agent_policy"])
    assert rec.requests == []
    w.post("store", _report_store_body(), entity_types=["task"])
    assert rec.requests[-1].headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
def test_correct_classifies_on_the_targets_real_type(tmp_path, http, sync):
    rec = http(_typed_entities({"ent_pol": "agent_policy", "ent_task": "task"}))
    w = _writer(tmp_path, mode=ns.SigningMode.OFF, with_key=False)

    def correct(eid):
        if sync:
            return w.correct("task", eid, "status", "done", idempotency_key="k")
        return asyncio.run(w.acorrect("task", eid, "status", "done", idempotency_key="k"))

    # Declared `task`, but the target is an agent_policy: signed or nothing.
    with pytest.raises(ns.SignedWriteError):
        correct("ent_pol")
    assert _posts(rec) == []
    # A failed lookup is governance too.
    with pytest.raises(ns.SignedWriteError):
        correct("ent_unknown")
    assert _posts(rec) == []
    # A real task stays on the switch (bearer, with it off).
    correct("ent_task")
    [post] = _posts(rec)
    assert post.headers["authorization"] == "Bearer tok"


def test_type_lookup_skipped_when_the_body_is_already_governance(tmp_path, http):
    rec = http()
    w = _writer(tmp_path, mode=ns.SigningMode.OFF, with_key=False)
    with pytest.raises(ns.SignedWriteError):
        w.correct("agent_policy", "ent_pol", "status", "x", idempotency_key="k")
    assert rec.requests == []


def test_relationship_onto_a_governance_entity_is_governance(tmp_path, http):
    rec = http(_typed_entities({"ent_pol": "agent_policy", "ent_task": "task"}))
    w = _writer(tmp_path, mode=ns.SigningMode.OFF, with_key=False)
    ent = [{"entity_type": "daemon_report", "message": "m"}]
    with pytest.raises(ns.SignedWriteError):
        w.store(
            ent,
            idempotency_key="k",
            relationships=[
                {"relationship_type": "SUPERSEDES", "source_index": 0, "target_entity_id": "ent_pol"}
            ],
        )
    with pytest.raises(ns.SignedWriteError):  # unresolvable endpoint
        w.store(
            ent,
            idempotency_key="k",
            relationships=[
                {"relationship_type": "REFERS_TO", "source_entity_id": "ent_gone", "target_index": 0}
            ],
        )
    assert _posts(rec) == []
    w.store(
        ent,
        idempotency_key="k",
        relationships=[
            {"relationship_type": "REFERS_TO", "source_index": 0, "target_entity_id": "ent_task"}
        ],
    )
    [post] = _posts(rec)
    assert post.headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize(
    "rels",
    [
        [{"relationship_type": "REFERS_TO", "source_index": 0, "target_index": 5}],
        [{"relationship_type": "REFERS_TO", "source_index": 0}],
        ["not-a-mapping"],
        {"not": "a list"},
    ],
    ids=["index-out-of-range", "missing-endpoint", "non-mapping", "non-list"],
)
def test_malformed_relationships_are_governance(rels):
    body = {"entities": [{"entity_type": "task", "title": "t"}], "relationships": rels}
    assert ns.body_touches_governance(body)


def test_signing_issuer_is_pinned_not_ambient(tmp_path, monkeypatch):
    monkeypatch.setenv("NEOTOMA_AAUTH_ISS", "https://ambient.example")
    w = _writer(tmp_path, mode=ns.SigningMode.ON)
    from aauth_httpsig import DEFAULT_AAUTH_ISSUER

    assert w._load_signer().iss == DEFAULT_AAUTH_ISSUER

    keys = tmp_path / "keys2"
    jwk = _write_jwk(keys, "anthus")
    jwk["iss"] = "https://issuer-in-key.example"
    (keys / "anthus.jwk.json").write_text(json.dumps(jwk))
    w2 = ns.NeotomaWriter("anthus", mode=ns.SigningMode.ON, base_url=BASE, bearer="tok", keys_dir=keys)
    assert w2._load_signer().iss == "https://issuer-in-key.example"


# ── store target_id, and endpoint-scoped classification (security round 2) ──


def _extend(entity_type: str, target_id: str, **fields) -> dict:
    return {
        "entities": [{"entity_type": entity_type, "target_id": target_id, **fields}],
        "idempotency_key": "k",
    }


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
def test_store_target_id_onto_an_agent_policy_is_governance(tmp_path, http, sync):
    """A store declared `task` that extends an agent_policy by target_id is signed or refused."""
    rec = http(_typed_entities({"ent_pol": "agent_policy", "ent_task": "task"}))
    w = _writer(tmp_path, mode=ns.SigningMode.OFF, with_key=False)

    def store(body):
        if sync:
            return w.post("store", body)
        return asyncio.run(w.apost("store", body))

    with pytest.raises(ns.SignedWriteError):
        store(_extend("task", "ent_pol", rule="planted"))
    assert _posts(rec) == []
    # The real type was looked up, once.
    gets = [r for r in rec.requests if r.method == "GET"]
    assert [r.url.path for r in gets] == ["/entities/ent_pol"]
    # A target that cannot be resolved is governance too.
    with pytest.raises(ns.SignedWriteError):
        store(_extend("task", "ent_unknown", title="t"))
    assert _posts(rec) == []
    # Extending a real task stays on the switch (bearer, with it off).
    store(_extend("task", "ent_task", title="t"))
    [post] = _posts(rec)
    assert post.headers["authorization"] == "Bearer tok"


def test_store_target_id_onto_an_agent_policy_is_signed_when_a_key_exists(tmp_path, http):
    rec = http(_typed_entities({"ent_pol": "agent_policy"}))
    w = _writer(tmp_path, mode=ns.SigningMode.OFF)
    result = w.post("store", _extend("task", "ent_pol", rule="r"))
    assert result.signed
    [post] = _posts(rec)
    assert "authorization" not in post.headers
    assert post.headers["x-agent-label"] == ANTHUS


def test_store_target_id_onto_an_issue_gate_status_is_governance(tmp_path, http):
    """gate_status reached through target_id is judged against the target's real type."""
    rec = http(_typed_entities({"ent_issue": "issue"}))
    w = _writer(tmp_path, mode=ns.SigningMode.SHADOW, with_key=False)
    with pytest.raises(ns.SignedWriteError):
        w.post("store", _extend("task", "ent_issue", gate_status='{"pm":"clear"}'))
    assert _posts(rec) == []
    # Another field onto the same issue is ordinary (shadow, no key: bearer fallback).
    w.post("store", _extend("task", "ent_issue", title="t"))
    [post] = _posts(rec)
    assert post.headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize("bad", ["", None, 7, ["ent_pol"]], ids=["empty", "null", "int", "list"])
def test_store_with_an_unreadable_target_id_is_governance(bad):
    assert ns.write_touches_governance("store", _extend("task", bad))


def test_correct_resolves_entity_id_even_with_a_stray_entities_list(tmp_path, http):
    rec = http(_typed_entities({"ent_pol": "agent_policy", "ent_task": "task"}))
    w = _writer(tmp_path, mode=ns.SigningMode.OFF, with_key=False)
    body = {
        "entity_type": "task",
        "entity_id": "ent_pol",
        "field": "status",
        "value": "x",
        "idempotency_key": "k",
        "entities": [{"entity_type": "task", "title": "decoy"}],
    }
    with pytest.raises(ns.SignedWriteError):
        w.post("correct", body)
    assert _posts(rec) == []
    w.post("correct", {**body, "entity_id": "ent_task"})
    [post] = _posts(rec)
    assert post.headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
def test_create_relationship_endpoints_are_resolved(tmp_path, http, sync):
    rec = http(_typed_entities({"ent_pol": "agent_policy", "ent_a": "task", "ent_b": "task"}))
    w = _writer(tmp_path, mode=ns.SigningMode.OFF, with_key=False)

    def post(path, body):
        if sync:
            return w.post(path, body)
        return asyncio.run(w.apost(path, body))

    edge = {"relationship_type": "REFERS_TO", "source_entity_id": "ent_a"}
    with pytest.raises(ns.SignedWriteError):
        post("create_relationship", {**edge, "target_entity_id": "ent_pol"})
    with pytest.raises(ns.SignedWriteError):
        post("create_relationships", {"relationships": [
            {**edge, "target_entity_id": "ent_b"}, {**edge, "target_entity_id": "ent_pol"},
        ]})
    with pytest.raises(ns.SignedWriteError):  # missing endpoint
        post("create_relationship", edge)
    assert _posts(rec) == []
    post("create_relationship", {**edge, "target_entity_id": "ent_b"})
    [p] = _posts(rec)
    assert p.headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize(
    "path", ["entities/merge", "entities/split", "delete_entity", "observations/create", "store?x=1", ""]
)
def test_unsupported_endpoints_are_refused_before_anything_is_sent(tmp_path, http, path):
    rec = http()
    w = _writer(tmp_path, mode=ns.SigningMode.ON)
    body = {"entity_type": "task", "from_entity_id": "ent_a", "to_entity_id": "ent_b"}
    with pytest.raises(ns.NeotomaWriteError, match="not a write endpoint"):
        w.post(path, body)
    with pytest.raises(ns.NeotomaWriteError, match="not a write endpoint"):
        asyncio.run(w.apost(path, body))
    assert rec.requests == []


def test_supported_endpoints_are_the_ones_the_classifier_reads():
    assert ns.SUPPORTED_WRITE_PATHS == {
        "store", "correct", "create_relationship", "create_relationships"
    }
