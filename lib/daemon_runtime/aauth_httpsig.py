"""RFC 9421 HTTP Message Signature signer for Ateles daemons (AAuth).

Produces the exact wire format Neotoma's ``aauthVerify`` middleware expects
(it verifies via ``@hellocoop/httpsig``). This is a Python port of the
TypeScript reference signer ``neotoma-rc-src/src/proxy/aauth_client_signer.ts``
plus the library's own signature-base construction
(``@hellocoop/httpsig/dist/{fetch,utils/signature,utils/base64}.js``).

Wire format (label ``aasig``)::

    Content-Digest:  sha-256=:<std-b64(sha256(body))>:
    Signature-Key:   aasig=jwt;jwt="<aa-agent+jwt>"
    Signature-Input: aasig=("@method" "@authority" "@path" ...);created=<unix>
    Signature:       aasig=:<std-b64(raw r||s)>:

Covered components:
    with body:    @method @authority @path content-type content-digest signature-key
    without body: @method @authority @path signature-key

Signature base (one line per component, ``"name": value`` joined by ``\\n``),
terminating with ``"@signature-params": (...);created=<unix>``. Signed with
ECDSA P-256 / SHA-256 and emitted as the raw IEEE-P1363 r||s pair (NOT DER) —
matching WebCrypto's ``crypto.subtle.sign`` output, which the verifier expects.

The ``aa-agent+jwt`` carries ``cnf.jwk`` (the public key the verifier uses) and
``sub``/``iss`` for attribution; it is trusted transitively because
``signature-key`` is a covered component of the HTTP signature.

Stdlib + ``cryptography`` + ``PyJWT`` only — no new runtime dependency.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

SIGNATURE_LABEL = "aasig"
JWT_TYP = "aa-agent+jwt"
DEFAULT_TOKEN_TTL_SEC = 300

COMPONENTS_WITH_BODY: tuple[str, ...] = (
    "@method",
    "@authority",
    "@path",
    "content-type",
    "content-digest",
    "signature-key",
)
COMPONENTS_WITHOUT_BODY: tuple[str, ...] = (
    "@method",
    "@authority",
    "@path",
    "signature-key",
)


class AAuthSigningError(Exception):
    """Raised when a request cannot be signed (bad key, missing crypto, etc.)."""


# ── base64 / digest (match base64Encode: STANDARD base64 with padding) ───────


def _std_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def content_digest(body: bytes) -> str:
    """``sha-256=:<std-b64(sha256(body))>:`` — RFC 9530 single-value digest."""
    return f"sha-256=:{_std_b64(hashlib.sha256(body).digest())}:"


# ── JWK → cryptography key + thumbprint (RFC 7638) ───────────────────────────


def _require_cryptography() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric import ec  # noqa: F401

        return ec
    except Exception as exc:  # noqa: BLE001
        raise AAuthSigningError(
            "cryptography is required for AAuth signing but is unavailable"
        ) from exc


def _b64url_to_int(s: str) -> int:
    pad = "=" * (-len(s) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(s + pad), "big")


def load_ec_private_key_from_jwk(jwk: Mapping[str, Any]) -> Any:
    """Load an EC P-256 private key (the ``d`` component) from a JWK."""
    ec = _require_cryptography()
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise AAuthSigningError(
            f"Unsupported JWK for AAuth: kty={jwk.get('kty')} crv={jwk.get('crv')} "
            "(expected EC / P-256)"
        )
    if "d" not in jwk:
        raise AAuthSigningError("JWK is missing the private 'd' component")
    d = _b64url_to_int(jwk["d"])
    x = _b64url_to_int(jwk["x"])
    y = _b64url_to_int(jwk["y"])
    pub = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1())
    return ec.EllipticCurvePrivateNumbers(d, pub).private_key()


def public_part_of(jwk: Mapping[str, Any]) -> dict[str, Any]:
    """Strip private components, leaving the public JWK."""
    drop = {"d", "p", "q", "dp", "dq", "qi"}
    return {k: v for k, v in jwk.items() if k not in drop}


def jwk_thumbprint(public_jwk: Mapping[str, Any]) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, base64url, no padding).

    For EC keys the canonical members are crv, kty, x, y in lexicographic
    order with compact separators — exactly what jose's
    ``calculateJwkThumbprint`` produces.
    """
    if public_jwk.get("kty") != "EC":
        raise AAuthSigningError("thumbprint only implemented for EC keys")
    canonical = {
        "crv": public_jwk["crv"],
        "kty": "EC",
        "x": public_jwk["x"],
        "y": public_jwk["y"],
    }
    serialized = json.dumps(canonical, separators=(",", ":"), sort_keys=True)
    return _b64url_nopad(hashlib.sha256(serialized.encode("utf-8")).digest())


# ── ES256 raw-signature (IEEE P1363 r||s, NOT DER) ───────────────────────────


def _ecdsa_sign_raw(private_key: Any, message: bytes) -> bytes:
    """Sign with ECDSA P-256/SHA-256, returning raw 64-byte r||s.

    cryptography emits DER; WebCrypto (and thus the verifier) expects the
    fixed-width r||s concatenation. Convert DER → raw.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils

    der = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


# ── aa-agent+jwt minting (PyJWT, ES256) ──────────────────────────────────────


def mint_agent_token_jwt(
    *,
    private_key: Any,
    public_jwk: Mapping[str, Any],
    sub: str,
    iss: str,
    kid: str | None,
    ttl_sec: int,
    now: int,
) -> str:
    import jwt as pyjwt

    jkt = jwk_thumbprint(public_jwk)
    ttl = max(30, int(ttl_sec))
    headers: dict[str, Any] = {"typ": JWT_TYP}
    if kid:
        headers["kid"] = kid
    payload = {
        "jkt": jkt,
        "cnf": {"jwk": dict(public_jwk)},
        "sub": sub,
        "iss": iss,
        "iat": now,
        "exp": now + ttl,
    }
    return pyjwt.encode(payload, private_key, algorithm="ES256", headers=headers)


# ── signer ───────────────────────────────────────────────────────────────────


@dataclass
class HttpSigSigner:
    """Signs outbound HTTP requests with an RFC 9421 AAuth signature."""

    private_jwk: Mapping[str, Any]
    sub: str
    iss: str
    kid: str | None = None
    ttl_sec: int = DEFAULT_TOKEN_TTL_SEC

    def __post_init__(self) -> None:
        self._private_key = load_ec_private_key_from_jwk(self.private_jwk)
        self._public_jwk = public_part_of(self.private_jwk)

    def sign_headers(
        self,
        *,
        method: str,
        url: str,
        body: bytes | str | None,
        content_type: str | None = "application/json",
        now: int | None = None,
    ) -> dict[str, str]:
        """Return the AAuth headers (Signature-Key/-Input/Signature, +digest).

        These are MERGED onto the request's existing headers by the caller.
        """
        created = int(now if now is not None else time.time())
        body_bytes: bytes
        has_body = body is not None and body != "" and body != b""
        if body is None:
            body_bytes = b""
        elif isinstance(body, str):
            body_bytes = body.encode("utf-8")
        else:
            body_bytes = body

        split = urlsplit(url)
        authority = split.netloc
        path = split.path or "/"

        agent_jwt = mint_agent_token_jwt(
            private_key=self._private_key,
            public_jwk=self._public_jwk,
            sub=self.sub,
            iss=self.iss,
            kid=self.kid,
            ttl_sec=self.ttl_sec,
            now=created,
        )
        sig_key_header = f'{SIGNATURE_LABEL}=jwt;jwt="{agent_jwt}"'

        out: dict[str, str] = {"signature-key": sig_key_header}
        components: Sequence[str]
        if has_body:
            components = COMPONENTS_WITH_BODY
            out["content-digest"] = content_digest(body_bytes)
        else:
            components = COMPONENTS_WITHOUT_BODY

        # Build component → value map.
        values: dict[str, str] = {}
        for c in components:
            if c == "@method":
                values[c] = method.upper()
            elif c == "@authority":
                values[c] = authority
            elif c == "@path":
                values[c] = path
            elif c == "content-type":
                if not content_type:
                    raise AAuthSigningError(
                        "content-type is a covered component but no value was provided"
                    )
                values[c] = content_type
            elif c == "content-digest":
                values[c] = out["content-digest"]
            elif c == "signature-key":
                values[c] = sig_key_header
            else:  # pragma: no cover - components are a fixed allowlist
                raise AAuthSigningError(f"unsupported covered component: {c}")

        component_list = " ".join(f'"{c}"' for c in components)
        sig_params = f"({component_list});created={created}"
        out["signature-input"] = f"{SIGNATURE_LABEL}={sig_params}"

        # Signature base: "name": value\n ... ending with @signature-params.
        lines = [f'"{c}": {values[c]}' for c in components]
        lines.append(f'"@signature-params": {sig_params}')
        signature_base = "\n".join(lines).encode("utf-8")

        raw_sig = _ecdsa_sign_raw(self._private_key, signature_base)
        out["signature"] = f"{SIGNATURE_LABEL}=:{_std_b64(raw_sig)}:"

        # Surface content-type so callers that omit it still send a value the
        # signature covers.
        if has_body and content_type:
            out.setdefault("content-type", content_type)
        return out
