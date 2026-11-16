"""Caller-side checkpoint resolution signing.

The MCP server is verification-only and must never load the resolver's private
key. A trusted caller imports this module in its own process, signs the exact
canonical ``POST /correct`` body, and passes only the returned RFC 9421 headers
to ``resolve_checkpoint``.
"""

from __future__ import annotations

from typing import Any, Mapping

from .aauth_httpsig import AAuthSigningError, HttpSigSigner
from .checkpoint_protocol import canonical_json_bytes, checkpoint_resolution_body

MAX_RESOLUTION_PROOF_TTL_SECONDS = 300


def sign_checkpoint_resolution(
    checkpoint_id: str,
    action: str,
    *,
    private_jwk: Mapping[str, Any],
    expected_sub: str,
    expected_jkt: str,
    issuer: str,
    neotoma_base_url: str,
    ttl_seconds: int = MAX_RESOLUTION_PROOF_TTL_SECONDS,
    now: int | None = None,
) -> dict[str, str]:
    """Return the five body-bound headers accepted by ``resolve_checkpoint``.

    ``expected_jkt`` is compared with the RFC 7638 thumbprint derived from the
    existing JWK's public members. A different key, even one claiming the same
    subject, is refused before any proof is produced.
    """
    ttl = int(ttl_seconds)
    if ttl < 1 or ttl > MAX_RESOLUTION_PROOF_TTL_SECONDS:
        raise AAuthSigningError("checkpoint resolution proof lifetime is invalid")
    required_sub = str(expected_sub or "").strip()
    key_sub = str(private_jwk.get("sub") or "").strip()
    if not key_sub or (required_sub and key_sub != required_sub):
        raise AAuthSigningError("resolver JWK subject is missing or mismatched")
    resolved_issuer = str(issuer or "").strip()
    if not resolved_issuer:
        raise AAuthSigningError("resolver JWK issuer is missing")
    try:
        signer = HttpSigSigner(
            private_jwk=private_jwk,
            sub=key_sub,
            iss=resolved_issuer,
            kid=str(private_jwk.get("kid") or "").strip() or None,
        )
    except Exception:  # noqa: BLE001 — normalize without exposing key material
        raise AAuthSigningError("could not load resolver JWK") from None
    if signer.thumbprint != str(expected_jkt or "").strip():
        raise AAuthSigningError("resolver JWK thumbprint does not match authority")
    signer.ttl_sec = ttl
    body = canonical_json_bytes(checkpoint_resolution_body(checkpoint_id, action))
    return signer.sign_headers(
        method="POST",
        url=f"{str(neotoma_base_url).rstrip('/')}/correct",
        body=body,
        content_type="application/json",
        now=now,
    )
