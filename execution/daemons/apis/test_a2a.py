"""
Unit tests for the Apis A2A inbound gateway.

Run with:   .venv/bin/python execution/daemons/apis/test_a2a.py
Or pytest:  .venv/bin/python -m pytest execution/daemons/apis/test_a2a.py -v

All tests exercise pure logic + the SDK-agnostic bridge with an injected
transport — no network calls, and `a2a-sdk` need not be installed.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_DAEMON_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import a2a_executor as ax  # noqa: E402
import a2a_gateway as gw  # noqa: E402
import routing  # noqa: E402

# Obvious non-secret placeholder for the Neotoma bearer arg in tests. Written
# this way (dotted, self-describing) so secret scanners don't flag it.
FAKE_BEARER = "fake.not-a-real-token.for-tests-only"


# ── Fixtures / helpers ──────────────────────────────────────────────────────


def _capture_transport():
    """A fake POST /api/store transport that records the request and returns a
    fabricated success response. Returns (transport_fn, captured_dict)."""
    captured: dict = {}

    def _transport(url, data, headers):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(data)
        return {"entities": [{"entity_id": "ent_test123", "entity_type": "task"}]}

    return _transport, captured


def _store_fn_with(transport):
    def _store(title, body, tags, *, caller="", idempotency_key=None):
        return ax.create_neotoma_task(
            title, body, tags,
            caller=caller, idempotency_key=idempotency_key,
            bearer_token=FAKE_BEARER, transport=transport,
        )
    return _store


# ── routing.py ──────────────────────────────────────────────────────────────


def test_routing_infer_and_resolve():
    tags = routing.infer_tags_from_text("Fix the deploy pipeline bug")
    assert "ops" in tags and "engineering" in tags, tags
    assert routing.resolve_skill(tags) == "gryllus"
    assert routing.resolve_skill(routing.infer_tags_from_text("pay rent invoice")) == "monedula"
    assert routing.resolve_skill([]) is None
    # card domains track the routing table
    assert routing.SUPPORTED_DOMAINS == list(routing.DOMAIN_ROUTES.keys())
    print("✓ routing infer/resolve")


# ── a2a_executor: message parsing ───────────────────────────────────────────


def test_parse_message_text():
    assert ax.parse_message_text("hello") == "hello"
    parts = [{"kind": "text", "text": "line1"}, {"kind": "text", "text": "line2"}]
    assert ax.parse_message_text(parts) == "line1\nline2"
    # legacy {"type": "text"} and non-text parts tolerated
    mixed = [{"type": "text", "text": "a"}, {"kind": "file", "file": {}}]
    assert ax.parse_message_text(mixed) == "a"
    print("✓ parse_message_text")


def test_split_title_body():
    title, body = ax.split_title_body("Fix CI\nThe docker step fails.")
    assert title == "Fix CI"
    assert body == "The docker step fails."
    title2, body2 = ax.split_title_body("")
    assert title2 == "(untitled A2A task)" and body2 == ""
    print("✓ split_title_body")


# ── a2a_executor: bridge → Neotoma task ─────────────────────────────────────


def test_bridge_submit_creates_task():
    transport, captured = _capture_transport()
    bridge = ax.ApisTaskBridge(store_fn=_store_fn_with(transport))

    result = bridge.submit(
        "Fix the deploy pipeline crash\nThe CI build fails on the docker step.",
        caller="external-agent@example.com",
    )
    assert result.ok, result
    assert result.neotoma_entity_id == "ent_test123"
    assert result.skill == "gryllus"
    assert "ops" in result.tags

    # the store request hit /api/store with a Bearer header and a task entity
    assert captured["url"].endswith("/api/store")
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    ent = captured["body"]["entities"][0]
    assert ent["entity_type"] == "task"
    assert ent["source"] == "a2a"
    assert ent["visibility"] == "private"
    # caller provenance is recorded in the task body
    assert "external-agent@example.com" in ent["description"]
    # inferred tags persisted on the entity
    assert "ops" in ent["tags"]
    print("✓ bridge.submit creates attributed Neotoma task")


def test_bridge_submit_idempotent():
    transport, _ = _capture_transport()
    bridge = ax.ApisTaskBridge(store_fn=_store_fn_with(transport))
    msg = "Draft a newsletter\nAnnounce the new feature."
    r1 = bridge.submit(msg, caller="a")
    r2 = bridge.submit(msg, caller="b")
    assert r1.a2a_task_id == r2.a2a_task_id
    assert bridge.get(r1.a2a_task_id).title == "Draft a newsletter"
    assert "comms" in r1.tags  # comms domain → routed to the gryllus skill
    assert r1.skill == "gryllus"
    print("✓ bridge.submit idempotent on identical content")


def test_bridge_submit_store_failure():
    def _failing_store(title, body, tags, *, caller="", idempotency_key=None):
        return None  # simulate Neotoma store failure

    bridge = ax.ApisTaskBridge(store_fn=_failing_store)
    result = bridge.submit("something", caller="x")
    assert not result.ok
    assert result.status == "failed"
    assert result.error == "neotoma_store_failed"
    print("✓ bridge.submit surfaces store failure")


def test_create_task_skips_without_token():
    transport, _ = _capture_transport()
    # No bearer token → returns None, never calls transport.
    eid = ax.create_neotoma_task(
        "t", "b", ["ops"], bearer_token="", transport=transport
    )
    assert eid is None
    print("✓ create_neotoma_task skips when unconfigured")


# ── a2a_gateway: Agent Card ─────────────────────────────────────────────────


def test_build_agent_card():
    card = gw.build_agent_card("https://apis.example.com")
    assert card["name"] == "Ateles Apis"
    assert card["url"] == "https://apis.example.com/"
    assert card["protocolVersion"]
    skill = card["skills"][0]
    assert skill["id"] == "delegate-task"
    # every routed domain is advertised as a tag
    for domain in routing.SUPPORTED_DOMAINS:
        assert domain in skill["tags"], domain
    # security scheme declared
    assert "bearer" in card["securitySchemes"]
    print("✓ build_agent_card shape + domain tags")


def test_sign_agent_card_verifies():
    """A signed card's JWS must verify against the signer's public key."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
    except ImportError:
        print("• skip sign test (cryptography not installed)")
        return

    priv = ec.generate_private_key(ec.SECP256R1())

    class _Signer:
        _private_key = priv
        key_id = "test-kid"

    card = gw.build_agent_card("https://apis.example.com")
    signed = gw.sign_agent_card(card, signer=_Signer())
    assert "signatures" in signed, "card not signed"
    sig = signed["signatures"][0]

    # reconstruct signing input over the original (signature-free) payload
    payload = {k: v for k, v in signed.items() if k != "signatures"}
    payload_b64 = (
        base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    signing_input = f"{sig['protected']}.{payload_b64}".encode()

    def _b64url_dec(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * ((4 - len(s) % 4) % 4))

    raw = _b64url_dec(sig["signature"])
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    der = utils.encode_dss_signature(r, s)
    priv.public_key().verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
    print("✓ sign_agent_card JWS verifies")


def test_sign_agent_card_stub_unsigned():
    class _Stub:
        _private_key = None
        key_id = ""

    card = gw.build_agent_card()
    out = gw.sign_agent_card(card, signer=_Stub())
    assert "signatures" not in out  # graceful: unsigned but valid
    print("✓ sign_agent_card stub → unsigned card (no crash)")


# ── a2a_gateway: authorization ──────────────────────────────────────────────


def test_authorize_caller():
    # auth disabled → always allowed
    ok, reason = gw.authorize_caller("", require_auth=False)
    assert ok and reason == "auth_not_required"

    # missing identity when required → rejected
    ok, reason = gw.authorize_caller("", require_auth=True)
    assert not ok and reason == "missing_caller_identity"

    class _GrantOK:
        is_active = True

        def has_capability(self, cap):
            return cap == gw.A2A_TASK_CAPABILITY

    ok, reason = gw.authorize_caller(
        "ext@a", require_auth=True, grant_checker_factory=lambda s: _GrantOK()
    )
    assert ok and reason == "ok"

    class _GrantNoCap:
        is_active = True

        def has_capability(self, cap):
            return False

    ok, reason = gw.authorize_caller(
        "ext@a", require_auth=True, grant_checker_factory=lambda s: _GrantNoCap()
    )
    assert not ok and reason == "missing_capability"

    class _GrantInactive:
        is_active = False

        def has_capability(self, cap):
            return True

    ok, reason = gw.authorize_caller(
        "ext@a", require_auth=True, grant_checker_factory=lambda s: _GrantInactive()
    )
    assert not ok and reason == "grant_not_active"

    # checker raises → advisory allow (current phase)
    def _boom(_sub):
        raise RuntimeError("neotoma down")

    ok, reason = gw.authorize_caller(
        "ext@a", require_auth=True, grant_checker_factory=_boom
    )
    assert ok and reason == "grant_check_unavailable_advisory"
    print("✓ authorize_caller all branches")


# ── Runner ──────────────────────────────────────────────────────────────────

_TESTS = [
    test_routing_infer_and_resolve,
    test_parse_message_text,
    test_split_title_body,
    test_bridge_submit_creates_task,
    test_bridge_submit_idempotent,
    test_bridge_submit_store_failure,
    test_create_task_skips_without_token,
    test_build_agent_card,
    test_sign_agent_card_verifies,
    test_sign_agent_card_stub_unsigned,
    test_authorize_caller,
]


def main() -> int:
    failures = 0
    for t in _TESTS:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"✗ {t.__name__}: {exc}")
    total = len(_TESTS)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
