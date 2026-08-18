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
terminating with ``"@signature-params": (...);created=<unix>``.

Two algorithms are supported, per draft-hardt-oauth-aauth-protocol-10 §12.8.1,
which makes Ed25519 a MUST and ES256 a SHOULD:

* ``Ed25519`` (OKP) — the signature is RFC 8032's native 64-byte form.
* ``ES256`` (EC/P-256) — ECDSA/SHA-256 emitted as the raw IEEE-P1363 r||s pair
  (NOT DER), matching WebCrypto's ``crypto.subtle.sign`` output.

The ``aa-agent+jwt`` carries ``cnf.jwk`` (the public key the verifier uses) and
``sub``/``iss`` for attribution; it is trusted transitively because
``signature-key`` is a covered component of the HTTP signature. Its claim set
follows draft-10 §5.2.2 (``iss``, ``dwk``, ``sub``, ``jti``, ``cnf``, ``iat``,
``exp``), plus the legacy ``jkt`` that Neotoma's deployed verifier still reads.

Stdlib + ``cryptography`` + ``PyJWT`` only — no new runtime dependency.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

SIGNATURE_LABEL = "aasig"
JWT_TYP = "aa-agent+jwt"
DEFAULT_TOKEN_TTL_SEC = 300

# Well-known metadata document for agent-provider key discovery; the value of
# the REQUIRED ``dwk`` claim (draft-10 §5.2.2).
AGENT_METADATA_DOCUMENT = "aauth-agent.json"

# Fully-specified JOSE algorithm identifiers, keyed by (kty, crv).
# draft-10 §12.8.1: Ed25519 MUST be supported, ES256 SHOULD be. The
# polymorphic "EdDSA" identifier MUST NOT be used — RFC 9864 deprecated it in
# favour of "Ed25519", and a verifier reading only `alg` cannot otherwise tell
# Ed25519 from Ed448.
_ALG_BY_KEY_TYPE: dict[tuple[str, str], str] = {
    ("EC", "P-256"): "ES256",
    ("OKP", "Ed25519"): "Ed25519",
}

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


def _new_jti() -> str:
    """A fresh 128-bit token identifier (draft-10 §5.2.2 REQUIRES ``jti``)."""
    return _b64url_nopad(secrets.token_bytes(16))


# ── algorithm determination (draft-10 §12.8.1) ───────────────────────────────


def alg_for_jwk(jwk: Mapping[str, Any]) -> str:
    """Return the fully-specified JOSE ``alg`` for a JWK.

    Prefers an explicit ``alg`` member, but verifies it agrees with ``kty`` /
    ``crv`` — draft-10 requires a verifier to reject a key whose ``kty`` or
    ``crv`` disagrees with its ``alg``, so we refuse to sign such a key rather
    than emit a token that every conformant verifier will reject.
    """
    kty = jwk.get("kty")
    crv = jwk.get("crv")
    expected = _ALG_BY_KEY_TYPE.get((str(kty), str(crv)))
    if expected is None:
        raise AAuthSigningError(
            f"Unsupported AAuth key: kty={kty} crv={crv} "
            "(expected EC/P-256 for ES256 or OKP/Ed25519 for Ed25519)"
        )
    declared = jwk.get("alg")
    if declared is None:
        return expected
    if declared == "EdDSA":
        raise AAuthSigningError(
            "The polymorphic 'EdDSA' alg MUST NOT be used (draft-10 §12.8.1); "
            "use the fully-specified 'Ed25519'"
        )
    if declared != expected:
        raise AAuthSigningError(
            f"JWK alg={declared!r} disagrees with kty={kty} crv={crv} "
            f"(expected {expected!r})"
        )
    return expected


# JWK members that may appear in `cnf.jwk`. Our key files also carry
# bookkeeping like `sub`, which is a JWT claim rather than a JWK member and has
# no business inside the confirmation key.
_CNF_JWK_MEMBERS = frozenset({"kty", "crv", "x", "y", "n", "e", "alg", "kid", "use"})


def _cnf_jwk(public_jwk: Mapping[str, Any]) -> dict[str, Any]:
    """The public JWK for ``cnf``, guaranteed to carry a fully-specified ``alg``.

    draft-10 §12.8.1: a verifier MUST reject a key whose ``alg`` is absent, so
    we fill it in from the key type rather than forwarding a bare JWK.
    """
    out = {k: v for k, v in public_jwk.items() if k in _CNF_JWK_MEMBERS}
    out["alg"] = alg_for_jwk(public_jwk)
    return out


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


def _b64url_to_bytes(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def load_private_key_from_jwk(jwk: Mapping[str, Any]) -> Any:
    """Load an AAuth signing key from a JWK.

    Supports EC/P-256 (ES256) and OKP/Ed25519 — the two algorithm families
    draft-10 §12.8.1 admits.
    """
    if "d" not in jwk:
        raise AAuthSigningError("JWK is missing the private 'd' component")
    # Validates the (kty, crv) pair and any declared alg.
    alg = alg_for_jwk(jwk)

    if alg == "Ed25519":
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
        except Exception as exc:  # noqa: BLE001
            raise AAuthSigningError(
                "cryptography is required for AAuth signing but is unavailable"
            ) from exc
        return Ed25519PrivateKey.from_private_bytes(_b64url_to_bytes(jwk["d"]))

    ec = _require_cryptography()
    d = _b64url_to_int(jwk["d"])
    x = _b64url_to_int(jwk["x"])
    y = _b64url_to_int(jwk["y"])
    pub = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1())
    return ec.EllipticCurvePrivateNumbers(d, pub).private_key()


def load_ec_private_key_from_jwk(jwk: Mapping[str, Any]) -> Any:
    """Deprecated alias for :func:`load_private_key_from_jwk`.

    Retained because callers outside this module import it by name.
    """
    return load_private_key_from_jwk(jwk)


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
    kty = public_jwk.get("kty")
    if kty == "EC":
        canonical = {
            "crv": public_jwk["crv"],
            "kty": "EC",
            "x": public_jwk["x"],
            "y": public_jwk["y"],
        }
    elif kty == "OKP":
        # RFC 8037 §2: the canonical OKP members are crv, kty, x.
        canonical = {
            "crv": public_jwk["crv"],
            "kty": "OKP",
            "x": public_jwk["x"],
        }
    else:
        raise AAuthSigningError(
            f"thumbprint not implemented for kty={kty!r} (expected EC or OKP)"
        )
    serialized = json.dumps(canonical, separators=(",", ":"), sort_keys=True)
    return _b64url_nopad(hashlib.sha256(serialized.encode("utf-8")).digest())


# ── raw signatures (ES256: IEEE P1363 r||s, NOT DER; Ed25519: native) ────────


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


def _sign_for_alg(private_key: Any, message: bytes, alg: str) -> bytes:
    """Produce the HTTP Message Signature bytes for the given algorithm.

    Ed25519 (RFC 8032) signatures are already the fixed 64-byte form the
    verifier expects, so they are emitted as-is; ES256 needs the DER → r||s
    conversion above.
    """
    if alg == "Ed25519":
        return private_key.sign(message)
    if alg == "ES256":
        return _ecdsa_sign_raw(private_key, message)
    raise AAuthSigningError(f"unsupported signing algorithm: {alg!r}")


# ── aa-agent+jwt minting (PyJWT) ─────────────────────────────────────────────

# Algorithms PyJWT cannot emit under their fully-specified JOSE name. PyJWT
# 2.13 exposes Ed25519 only as the polymorphic "EdDSA" that RFC 9864
# deprecated and draft-10 §12.8.1 forbids on the wire, so those tokens are
# assembled and signed directly instead.
_PYJWT_CANNOT_NAME = {"Ed25519"}


def _encode_jws_manually(
    *,
    private_key: Any,
    payload: Mapping[str, Any],
    headers: Mapping[str, Any],
    alg: str,
) -> str:
    """Build and sign a compact JWS, controlling the protected header exactly.

    The header is part of the JWS signing input, so it must already carry the
    fully-specified ``alg`` when the signature is computed — rewriting it
    afterwards would invalidate the token.
    """
    segments = []
    for part in (dict(headers), dict(payload)):
        raw = json.dumps(part, separators=(",", ":")).encode("utf-8")
        segments.append(_b64url_nopad(raw))
    signing_input = ".".join(segments).encode("ascii")
    signature = _sign_for_alg(private_key, signing_input, alg)
    segments.append(_b64url_nopad(signature))
    return ".".join(segments)


def mint_agent_token_jwt(
    *,
    private_key: Any,
    public_jwk: Mapping[str, Any],
    sub: str,
    iss: str,
    kid: str | None,
    ttl_sec: int,
    now: int,
    jti: str | None = None,
    include_jkt: bool = True,
) -> str:
    """Mint an ``aa-agent+jwt`` agent token.

    Claims follow draft-hardt-oauth-aauth-protocol-10 §5.2.2, which REQUIRES
    ``iss``, ``dwk``, ``sub``, ``jti``, ``cnf``, ``iat``, and ``exp``.

    ``jti`` defaults to a fresh 128-bit random value; callers pass one only to
    make a token reproducible in tests. ``include_jkt`` retains the legacy
    ``jkt`` claim, which predates the current draft and is not part of it —
    Neotoma's deployed verifier still reads it, so it stays on by default and
    can be dropped once that verifier no longer needs it.
    """
    import jwt as pyjwt

    ttl = max(30, int(ttl_sec))
    headers: dict[str, Any] = {"typ": JWT_TYP}
    if kid:
        headers["kid"] = kid
    payload: dict[str, Any] = {
        "iss": iss,
        # Key-discovery pointer: verifiers resolve {iss}/.well-known/{dwk}.
        "dwk": AGENT_METADATA_DOCUMENT,
        "sub": sub,
        "jti": jti or _new_jti(),
        "cnf": {"jwk": _cnf_jwk(public_jwk)},
        "iat": now,
        "exp": now + ttl,
    }
    if include_jkt:
        payload["jkt"] = jwk_thumbprint(public_jwk)

    alg = alg_for_jwk(public_jwk)
    headers["alg"] = alg
    if alg in _PYJWT_CANNOT_NAME:
        return _encode_jws_manually(
            private_key=private_key, payload=payload, headers=headers, alg=alg
        )
    return pyjwt.encode(payload, private_key, algorithm=alg, headers=headers)


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
        self._private_key = load_private_key_from_jwk(self.private_jwk)
        self._public_jwk = public_part_of(self.private_jwk)
        self._alg = alg_for_jwk(self._public_jwk)

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

        raw_sig = _sign_for_alg(self._private_key, signature_base, self._alg)
        out["signature"] = f"{SIGNATURE_LABEL}=:{_std_b64(raw_sig)}:"

        # Surface content-type so callers that omit it still send a value the
        # signature covers.
        if has_body and content_type:
            out.setdefault("content-type", content_type)
        return out
