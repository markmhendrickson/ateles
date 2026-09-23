"""Caller-side checkpoint resolution signing.

The MCP server is verification-only and must never load the resolver's private
key. A trusted caller imports this module in its own process, signs the exact
canonical ``POST /correct`` body, and passes only the returned RFC 9421 headers
to ``resolve_checkpoint``.
"""

from __future__ import annotations

from pathlib import Path

from .aauth_httpsig import AAuthSigningError, load_http_sig_signer
from .checkpoint_protocol import canonical_json_bytes, checkpoint_resolution_body

MAX_RESOLUTION_PROOF_TTL_SECONDS = 300


def sign_checkpoint_resolution(
    checkpoint_id: str,
    action: str,
    *,
    private_jwk_path: Path,
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
    signer = load_http_sig_signer(
        Path(private_jwk_path),
        expected_sub=str(expected_sub or "").strip(),
        issuer=str(issuer or "").strip(),
    )
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
