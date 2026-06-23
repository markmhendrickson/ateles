"""Tests for the RFC 9421 AAuth signer.

The authoritative interop check (Python signs → the real @hellocoop/httpsig
verifies) lives in the verify harness exercised during development; here we lock
down the wire-format invariants that must not drift:

- signature base canonicalization (component lines + @signature-params)
- standard-base64 Content-Digest
- thumbprint determinism
- raw (IEEE-P1363) ECDSA output, not DER
- self-verification of the produced signature against the covered base
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from daemon_runtime.aauth_httpsig import (
    COMPONENTS_WITH_BODY,
    COMPONENTS_WITHOUT_BODY,
    HttpSigSigner,
    content_digest,
    jwk_thumbprint,
    load_ec_private_key_from_jwk,
    public_part_of,
)

# A throwaway P-256 JWK generated for tests (NOT a real agent key).
_TEST_JWK = {
    "kty": "EC",
    "crv": "P-256",
    "d": "870MYoMHWMHBnnIfYRH0Y5xfsCNYHwf3Uq3F8KK3qSk",
    "x": "fJ8mZ8m9wQ3xhJ1jH2nT6Vd0mY9oQ1k3rJ8aF7bN2cE",
    "y": "Wm0kq5n7vX2pLrA1sT9oU4cZ6dE3fG8hJ0kL2mN4pQ6",
    "sub": "tester@ateles-swarm",
    "kid": "test-kid",
}


def _valid_test_jwk() -> dict:
    """Derive a usable P-256 JWK from a fresh key (the static x/y above are
    illustrative; generate a real one so signing actually works)."""
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    nums = priv.private_numbers()
    pub = nums.public_numbers

    def b64u(i: int) -> str:
        return base64.urlsafe_b64encode(i.to_bytes(32, "big")).rstrip(b"=").decode()

    return {
        "kty": "EC",
        "crv": "P-256",
        "d": b64u(nums.private_value),
        "x": b64u(pub.x),
        "y": b64u(pub.y),
        "sub": "tester@ateles-swarm",
        "kid": "test-kid",
    }


def test_content_digest_is_standard_base64() -> None:
    body = b'{"a":1}'
    expected = base64.b64encode(hashlib.sha256(body).digest()).decode()
    assert content_digest(body) == f"sha-256=:{expected}:"


def test_thumbprint_is_deterministic_and_rfc7638() -> None:
    jwk = _valid_test_jwk()
    pub = public_part_of(jwk)
    tp1 = jwk_thumbprint(pub)
    tp2 = jwk_thumbprint(pub)
    assert tp1 == tp2
    # RFC 7638: SHA-256 over {crv,kty,x,y} compact+sorted, base64url no pad.
    canonical = json.dumps(
        {"crv": pub["crv"], "kty": "EC", "x": pub["x"], "y": pub["y"]},
        separators=(",", ":"),
        sort_keys=True,
    )
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert tp1 == expected


def test_with_body_covers_digest_and_content_type() -> None:
    signer = HttpSigSigner(private_jwk=_valid_test_jwk(), sub="s@x", iss="s@x")
    h = signer.sign_headers(
        method="POST",
        url="http://localhost:9180/entities/query",
        body='{"entity_type":"task"}',
        now=1_700_000_000,
    )
    assert "content-digest" in h
    for comp in COMPONENTS_WITH_BODY:
        token = f'"{comp}"'
        assert token in h["signature-input"]
    assert h["signature-input"].endswith(";created=1700000000")
    assert h["signature-key"].startswith('aasig=jwt;jwt="')
    assert h["signature"].startswith("aasig=:") and h["signature"].endswith(":")


def test_without_body_omits_digest() -> None:
    signer = HttpSigSigner(private_jwk=_valid_test_jwk(), sub="s@x", iss="s@x")
    h = signer.sign_headers(
        method="GET", url="http://localhost:9180/session", body=None, content_type=None
    )
    assert "content-digest" not in h
    assert '"content-digest"' not in h["signature-input"]
    for comp in COMPONENTS_WITHOUT_BODY:
        assert f'"{comp}"' in h["signature-input"]


def test_signature_is_raw_p1363_not_der() -> None:
    """Raw ECDSA output is exactly 64 bytes; DER would be ~70-72 and variable."""
    signer = HttpSigSigner(private_jwk=_valid_test_jwk(), sub="s@x", iss="s@x")
    h = signer.sign_headers(
        method="GET", url="http://localhost:9180/session", body=None, content_type=None
    )
    raw_b64 = h["signature"][len("aasig=:") : -1]
    raw = base64.b64decode(raw_b64)
    assert len(raw) == 64


def test_self_verifies_against_reconstructed_base() -> None:
    """Sign, then verify the raw r||s against the public key over a base we
    reconstruct exactly as the signer did — catches base-construction drift."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils

    jwk = _valid_test_jwk()
    signer = HttpSigSigner(private_jwk=jwk, sub="s@x", iss="s@x")
    method, url, body = "POST", "http://localhost:9180/entities/query", '{"q":1}'
    h = signer.sign_headers(method=method, url=url, body=body, now=1_700_000_001)

    created = "1700000001"
    sig_params = (
        '("@method" "@authority" "@path" "content-type" "content-digest" '
        f'"signature-key");created={created}'
    )
    lines = [
        '"@method": POST',
        '"@authority": localhost:9180',
        '"@path": /entities/query',
        '"content-type": application/json',
        f'"content-digest": {h["content-digest"]}',
        f'"signature-key": {h["signature-key"]}',
        f'"@signature-params": {sig_params}',
    ]
    base = "\n".join(lines).encode()

    raw = base64.b64decode(h["signature"][len("aasig=:") : -1])
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    der = asym_utils.encode_dss_signature(r, s)

    pub_key = load_ec_private_key_from_jwk(jwk).public_key()
    pub_key.verify(der, base, ec.ECDSA(hashes.SHA256()))  # raises on mismatch


def test_rejects_non_ec_jwk() -> None:
    from daemon_runtime.aauth_httpsig import AAuthSigningError

    with pytest.raises(AAuthSigningError):
        HttpSigSigner(private_jwk={"kty": "RSA", "n": "x", "e": "AQAB"}, sub="s", iss="s")
