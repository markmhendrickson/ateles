"""Tests for the isolated caller-side checkpoint resolver signer."""

from __future__ import annotations

import base64
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from daemon_runtime.aauth_httpsig import (
    AAuthSigningError,
    content_digest,
    jwk_thumbprint,
    public_part_of,
)
from daemon_runtime.checkpoint_protocol import (
    canonical_json_bytes,
    checkpoint_resolution_body,
)
from daemon_runtime.checkpoint_resolution import (
    sign_checkpoint_resolution,
)


def _resolver_jwk() -> dict:
    private = ec.generate_private_key(ec.SECP256R1()).private_numbers()

    def b64u(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()

    return {
        "kty": "EC",
        "crv": "P-256",
        "d": b64u(private.private_value),
        "x": b64u(private.public_numbers.x),
        "y": b64u(private.public_numbers.y),
        "sub": "resolver@ateles-swarm",
        "kid": "existing-resolver-key",
    }


def _key_material():
    jwk = _resolver_jwk()
    return jwk, jwk_thumbprint(public_part_of(jwk))


def test_sign_checkpoint_resolution_uses_exact_canonical_body() -> None:
    jwk, jkt = _key_material()

    headers = sign_checkpoint_resolution(
        "ent_cp1",
        "approve",
        private_jwk=jwk,
        expected_sub="resolver@ateles-swarm",
        expected_jkt=jkt,
        issuer="https://issuer.example",
        neotoma_base_url="https://neotoma.example",
        now=1_700_000_000,
    )

    assert set(headers) == {
        "signature-key",
        "signature-input",
        "signature",
        "content-digest",
        "content-type",
    }
    body = canonical_json_bytes(checkpoint_resolution_body("ent_cp1", "approve"))
    assert headers["content-digest"] == content_digest(body)


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"expected_sub": "other@ateles-swarm"}, "subject"),
        ({"expected_jkt": "B" * 43}, "thumbprint"),
        ({"ttl_seconds": 301}, "lifetime"),
    ],
)
def test_sign_checkpoint_resolution_rejects_wrong_identity_or_lifetime(
    override, match
) -> None:
    jwk, jkt = _key_material()
    kwargs = {
        "private_jwk": jwk,
        "expected_sub": "resolver@ateles-swarm",
        "expected_jkt": jkt,
        "issuer": "https://issuer.example",
        "neotoma_base_url": "https://neotoma.example",
    }
    kwargs.update(override)

    with pytest.raises(AAuthSigningError, match=match):
        sign_checkpoint_resolution("ent_cp1", "approve", **kwargs)
