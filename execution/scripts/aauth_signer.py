"""AAuth signer for outbound MCP proxy requests.

Python port of `services/agent-site/netlify/lib/aauth_signer.ts` from the
sibling Neotoma repo, adapted for the local Cursor proxy. Produces the
RFC 9421 HTTP Message Signature plus the AAuth `Signature-Key` header
format that Neotoma's `aauthVerify` middleware expects.

Wire format reference (`@hellocoop/httpsig`, used by Neotoma's verifier):

    Signature-Key:   aasig=jwt;jwt="<aa-agent+jwt>"
    Signature-Input: aasig=("@method" "@authority" "@path" "content-type"
                            "content-digest" "signature-key");created=<unix>;keyid="<jkt>";alg="ecdsa-p256-sha256"
    Signature:       aasig=:<base64>:
    Content-Digest:  sha-256=:<base64-of-sha256(body)>:

The label `aasig` is shared by the three headers so the verifier can
correlate them; this matches the constant the Netlify forwarder uses.

Public verification keys are discovered out-of-band via the JWKS hosted
at `<iss>/.well-known/jwks.json`. The `Signature-Key` JWT carries the
public JWK inline under `cnf.jwk` so verifiers can also operate offline.

Inputs:

  Required env (loaded by `load_signer_config_from_env()`):
    NEOTOMA_AAUTH_PRIVATE_JWK_PATH   absolute path to the agent's private JWK
                                     (defaults to .creds/aauth_agent_cursor.private.jwk
                                     resolved against the repo root).
    NEOTOMA_AAUTH_SUB                AAuth subject, e.g. cursor@markmhendrickson.com
    NEOTOMA_AAUTH_ISS                AAuth issuer, e.g. https://markmhendrickson.com

  Optional:
    NEOTOMA_AAUTH_KID                Key id (default: jwk["kid"])
    NEOTOMA_AAUTH_TOKEN_TTL_SEC      JWT lifetime, default 300s
    NEOTOMA_AAUTH_AUTHORITY_OVERRIDE Override @authority canonicalization
                                     (defaults to the URL's host[:port]).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import http_sfv
import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from http_message_signatures import (
    HTTPMessageSigner,
    HTTPSignatureKeyResolver,
    algorithms,
)

SIGNATURE_LABEL = "aasig"


class SignerConfigError(RuntimeError):
    """Raised when the signer is missing required config or has an invalid JWK."""


@dataclass
class SignerConfig:
    private_jwk: dict[str, Any]
    sub: str
    iss: str
    kid: str
    token_ttl_sec: int = 300
    authority_override: str | None = None

    @property
    def public_jwk(self) -> dict[str, Any]:
        return _public_part_of(self.private_jwk)

    @property
    def jkt(self) -> str:
        return _jwk_thumbprint(self.public_jwk)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_to_int(value: str) -> int:
    pad = "=" * (-len(value) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(value + pad), "big")


def _public_part_of(jwk: dict[str, Any]) -> dict[str, Any]:
    redacted = {
        k: v for k, v in jwk.items() if k not in {"d", "p", "q", "dp", "dq", "qi"}
    }
    return redacted


def _jwk_thumbprint(public_jwk: dict[str, Any]) -> str:
    """RFC 7638 JWK thumbprint, EC P-256 only (matches the AAuth profile)."""
    if public_jwk.get("kty") != "EC":
        raise SignerConfigError(
            f"Unsupported JWK kty for AAuth signing: {public_jwk.get('kty')!r} (expected EC)."
        )
    canonical = json.dumps(
        {
            "crv": public_jwk["crv"],
            "kty": public_jwk["kty"],
            "x": public_jwk["x"],
            "y": public_jwk["y"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return _b64url(hashlib.sha256(canonical).digest())


def _resolve_alg(jwk: dict[str, Any]) -> str:
    if jwk.get("kty") == "EC" and jwk.get("crv") == "P-256":
        return "ES256"
    raise SignerConfigError(
        f"Unsupported JWK for AAuth signing: kty={jwk.get('kty')!r} "
        f"crv={jwk.get('crv')!r} alg={jwk.get('alg')!r} "
        "(only ES256 / P-256 is wired in this proxy today)."
    )


def _jwk_to_private_key(jwk: dict[str, Any]) -> ec.EllipticCurvePrivateKey:
    """Reconstruct a cryptography EC private key from a JWK."""
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise SignerConfigError("AAuth signer expects an EC P-256 private JWK.")
    if "d" not in jwk:
        raise SignerConfigError("Private JWK missing 'd' component.")
    d = _b64url_to_int(jwk["d"])
    private_numbers = ec.EllipticCurvePrivateNumbers(
        private_value=d,
        public_numbers=ec.EllipticCurvePublicNumbers(
            x=_b64url_to_int(jwk["x"]),
            y=_b64url_to_int(jwk["y"]),
            curve=ec.SECP256R1(),
        ),
    )
    return private_numbers.private_key()


def _resolve_private_jwk_path() -> Path:
    explicit = os.environ.get("NEOTOMA_AAUTH_PRIVATE_JWK_PATH")
    if explicit:
        return Path(explicit).expanduser()
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / ".creds" / "aauth_agent_cursor.private.jwk"


def load_signer_config_from_env() -> SignerConfig:
    """Build a `SignerConfig` from environment variables; raise if incomplete."""
    private_path = _resolve_private_jwk_path()
    if not private_path.is_file():
        raise SignerConfigError(
            f"Private JWK not found at {private_path}. Run "
            "execution/scripts/aauth_provision_identity.py first or set "
            "NEOTOMA_AAUTH_PRIVATE_JWK_PATH."
        )
    try:
        private_jwk = json.loads(private_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SignerConfigError(
            f"Private JWK at {private_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(private_jwk, dict):
        raise SignerConfigError(
            f"Private JWK at {private_path} did not parse to a JSON object."
        )

    sub = os.environ.get("NEOTOMA_AAUTH_SUB")
    iss = os.environ.get("NEOTOMA_AAUTH_ISS")
    if not sub:
        raise SignerConfigError("NEOTOMA_AAUTH_SUB is required for AAuth signing.")
    if not iss:
        raise SignerConfigError("NEOTOMA_AAUTH_ISS is required for AAuth signing.")

    kid_env = os.environ.get("NEOTOMA_AAUTH_KID")
    kid_jwk = (
        private_jwk.get("kid") if isinstance(private_jwk.get("kid"), str) else None
    )
    kid = kid_env or kid_jwk
    if not kid:
        raise SignerConfigError(
            "Could not determine kid: set NEOTOMA_AAUTH_KID or include 'kid' in the JWK."
        )

    ttl_raw = os.environ.get("NEOTOMA_AAUTH_TOKEN_TTL_SEC", "300")
    try:
        ttl = max(30, int(ttl_raw))
    except ValueError as exc:
        raise SignerConfigError(
            f"NEOTOMA_AAUTH_TOKEN_TTL_SEC must be an integer; got {ttl_raw!r}."
        ) from exc

    return SignerConfig(
        private_jwk=private_jwk,
        sub=sub,
        iss=iss,
        kid=kid,
        token_ttl_sec=ttl,
        authority_override=os.environ.get("NEOTOMA_AAUTH_AUTHORITY_OVERRIDE") or None,
    )


def mint_agent_token_jwt(config: SignerConfig) -> str:
    """Mint a fresh `aa-agent+jwt` for the configured subject."""
    alg = _resolve_alg(config.private_jwk)
    private_key = _jwk_to_private_key(config.private_jwk)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    now = int(time.time())
    claims = {
        "iss": config.iss,
        "sub": config.sub,
        "iat": now,
        "exp": now + config.token_ttl_sec,
        "jkt": config.jkt,
        "cnf": {"jwk": config.public_jwk},
    }
    headers = {"typ": "aa-agent+jwt", "kid": config.kid}
    return jwt.encode(claims, private_pem, algorithm=alg, headers=headers)


def _signature_key_header_value(token: str) -> str:
    """Build `Signature-Key: aasig=jwt;jwt="<token>"` per RFC 8941."""
    item = http_sfv.Item(http_sfv.Token("jwt"))
    item.params["jwt"] = token
    dictionary = http_sfv.Dictionary()
    dictionary[SIGNATURE_LABEL] = item
    return str(dictionary)


def _content_digest_header_value(body: bytes) -> str:
    digest = hashlib.sha256(body).digest()
    dictionary = http_sfv.Dictionary()
    dictionary["sha-256"] = http_sfv.Item(digest)
    return str(dictionary)


class _StaticKeyResolver(HTTPSignatureKeyResolver):
    def __init__(self, key_id: str, private_pem: bytes, public_pem: bytes) -> None:
        self._key_id = key_id
        self._private_pem = private_pem
        self._public_pem = public_pem

    def resolve_private_key(self, key_id: str):  # type: ignore[override]
        if key_id != self._key_id:
            raise KeyError(f"Unknown key_id {key_id!r}")
        return self._private_pem

    def resolve_public_key(self, key_id: str):  # type: ignore[override]
        if key_id != self._key_id:
            raise KeyError(f"Unknown key_id {key_id!r}")
        return self._public_pem


def build_signed_headers(
    *,
    method: str,
    url: str,
    body: bytes,
    base_headers: dict[str, str] | None,
    config: SignerConfig,
) -> dict[str, str]:
    """Return a fresh dict of HTTP headers with AAuth signature attached.

    The proxy passes the result straight to its aiohttp POST. `base_headers`
    is copied first so caller-provided values (Authorization, Mcp-Session-Id,
    Content-Type, X-Connection-Id, etc.) survive.
    """
    headers: dict[str, str] = dict(base_headers or {})
    headers.setdefault("Content-Type", "application/json")
    headers["Content-Digest"] = _content_digest_header_value(body)

    token = mint_agent_token_jwt(config)
    headers["Signature-Key"] = _signature_key_header_value(token)

    private_key = _jwk_to_private_key(config.private_jwk)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    parsed = urlparse(url)
    authority = config.authority_override or parsed.netloc
    if not authority:
        raise SignerConfigError(f"Cannot derive @authority from URL {url!r}.")
    request_for_sign = requests.Request(
        method=method.upper(),
        url=url,
        headers=headers,
        data=body,
    )
    prepared = request_for_sign.prepare()
    prepared.headers["Host"] = authority

    resolver = _StaticKeyResolver(config.jkt, private_pem, public_pem)
    signer = HTTPMessageSigner(
        signature_algorithm=algorithms.ECDSA_P256_SHA256,
        key_resolver=resolver,
    )
    signer.sign(
        prepared,
        key_id=config.jkt,
        label=SIGNATURE_LABEL,
        covered_component_ids=(
            "@method",
            "@authority",
            "@path",
            "content-type",
            "content-digest",
            "signature-key",
        ),
        include_alg=True,
    )

    for header_name in ("Signature", "Signature-Input"):
        value = prepared.headers.get(header_name)
        if value is None:
            raise SignerConfigError(
                f"http-message-signatures did not produce {header_name}; "
                "cannot continue."
            )
        headers[header_name] = value

    return headers


__all__ = [
    "SIGNATURE_LABEL",
    "SignerConfig",
    "SignerConfigError",
    "build_signed_headers",
    "load_signer_config_from_env",
    "mint_agent_token_jwt",
]
