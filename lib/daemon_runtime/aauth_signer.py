"""
lib/daemon_runtime/aauth_signer.py — AAuth request signing for Ateles daemons.

Each daemon has a per-role AAuth keypair stored in ateles-private/keys/.
This module loads the keypair and produces signed request headers for
Neotoma API calls, establishing per-daemon attribution on all observations.

Phase 1 status: keypairs are not yet minted. AAuthSigner.from_key_file()
returns a stub signer that logs a warning. Once keypairs are minted and
placed in ateles-private/keys/<name>.json, the stub upgrades automatically.

Supported key file formats (read from ateles-private/keys/<name>.jwk.json
or legacy ateles-private/keys/<name>.json):

  Canonical JWK format (preferred — produced by execution/scripts/mint_daemon_keypair.py):
    {
        "sub": "monedula@ateles-swarm",
        "kid": "<kid>",
        "kty": "EC", "crv": "P-256",
        "x": "<base64url>", "y": "<base64url>", "d": "<base64url private scalar>"
    }

  Legacy PEM format (still supported for backward compatibility):
    {
        "sub": "monedula@ateles-swarm",
        "key_id": "<kid>",
        "private_key_pem": "-----BEGIN EC PRIVATE KEY-----\\n..."
    }

Files must have mode 0600. See docs/aauth/keys.md for layout and rotation guide.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # imported as part of the daemon_runtime package
    from .aauth_identifier import normalize_for_wire as _normalize_sub
    from .aauth_httpsig import _sign_for_alg, alg_for_jwk
    from .aauth_httpsig import (
        load_private_key_from_jwk as _load_private_key_from_jwk,
    )
except ImportError:  # imported flat, with lib/daemon_runtime on sys.path
    # Daemon scripts (e.g. store_release_result.py) and their tests
    # sys.path-insert this directory and `import aauth_signer` as a bare
    # top-level module rather than via the `lib.daemon_runtime` package, so
    # the relative import above has no parent package to resolve against.
    # Fall back to the same bare import those callers already rely on.
    from aauth_identifier import normalize_for_wire as _normalize_sub  # type: ignore[no-redef]
    from aauth_httpsig import _sign_for_alg, alg_for_jwk  # type: ignore[no-redef]
    from aauth_httpsig import (  # type: ignore[no-redef]
        load_private_key_from_jwk as _load_private_key_from_jwk,
    )

log = logging.getLogger(__name__)

# Default location: ateles-private repo, checked out alongside ateles
_DEFAULT_KEYS_DIR = Path(
    os.environ.get(
        "ATELES_PRIVATE_KEYS_DIR",
        str(Path(__file__).parent.parent.parent.parent / "ateles-private" / "keys"),
    )
)


@dataclass
class AAuthSigner:
    """
    Signs outbound Neotoma requests with the daemon's AAuth keypair.

    If the keypair file doesn't exist (Phase 1 pre-mint), the signer is a
    stub: headers() returns an empty dict and a warning is logged once.
    """

    sub: str = ""
    key_id: str = ""
    _private_key: Any = None
    _alg: str = "ES256"
    _warned: bool = False

    @classmethod
    def from_key_file(
        cls, agent_name: str, keys_dir: Path | None = None
    ) -> AAuthSigner:
        """
        Load keypair from ateles-private/keys/<agent_name>.jwk.json (canonical)
        or ateles-private/keys/<agent_name>.json (legacy PEM format).
        Returns a stub signer if neither file exists.
        """
        keys_dir = keys_dir or _DEFAULT_KEYS_DIR
        name = agent_name.lower()

        # Probe canonical JWK path first, then legacy path.
        jwk_path = keys_dir / f"{name}.jwk.json"
        legacy_path = keys_dir / f"{name}.json"
        key_path = jwk_path if jwk_path.exists() else (
            legacy_path if legacy_path.exists() else None
        )

        if key_path is None:
            log.warning(
                f"[{agent_name}] AAuth key not found at {jwk_path} or {legacy_path} — "
                "stub signer in use (run: python execution/scripts/mint_daemon_keypair.py "
                f"--name {name})"
            )
            return cls(sub=_normalize_sub(f"{name}@ateles-swarm"))

        try:
            data = json.loads(key_path.read_text())
            sub = _normalize_sub(data.get("sub", f"{name}@ateles-swarm"))

            # Canonical JWK format: has "kty" and "d" fields.
            if "kty" in data and "d" in data:
                kid = data.get("kid", "")
                alg = alg_for_jwk(data)
                private_key = _load_private_key_jwk(data, alg)
            else:
                # Legacy PEM format — PEM keys minted here have always been
                # EC/P-256; Ed25519 is only produced in the canonical JWK
                # format above.
                kid = data.get("key_id", data.get("kid", ""))
                alg = "ES256"
                private_key = _load_private_key(data.get("private_key_pem", ""))

            signer = cls(sub=sub, key_id=kid, _alg=alg)
            signer._private_key = private_key
            log.info(
                f"[{agent_name}] AAuth keypair loaded (sub={sub} kid={kid} alg={alg} "
                f"format={'jwk' if 'kty' in data else 'pem'})"
            )
            return signer
        except Exception as exc:
            log.warning(
                f"[{agent_name}] Failed to load AAuth key from {key_path}: {exc} — "
                "stub signer in use"
            )
            return cls(sub=_normalize_sub(f"{name}@ateles-swarm"))

    @classmethod
    def stub(cls, agent_name: str) -> AAuthSigner:
        """Return a no-op stub signer for testing or pre-Phase-1 use."""
        return cls(sub=_normalize_sub(f"{agent_name.lower()}@ateles-swarm"))

    def headers(self, method: str = "POST", path: str = "/store") -> dict[str, str]:
        """
        Return AAuth Authorization headers for an outbound request.

        If the keypair is not loaded (stub mode), returns an empty dict.
        Bearer token auth (NEOTOMA_BEARER_TOKEN) still applies separately.
        """
        if not self._private_key:
            if not self._warned:
                log.debug(
                    f"[{self.sub}] Stub AAuth signer — no per-agent headers. "
                    "Observations attributed to operator token until keypair is minted."
                )
                self._warned = True
            return {}

        try:
            token = _sign_jwt(
                self.sub, self.key_id, self._private_key, method, path, self._alg
            )
            return {"X-AAuth-Token": token}
        except Exception as exc:
            log.warning(f"[{self.sub}] AAuth signing failed: {exc}")
            return {}

    @property
    def is_stub(self) -> bool:
        return self._private_key is None


# ── Private helpers ────────────────────────────────────────────────────────


def _load_private_key(pem: str) -> Any:
    """Load an EC private key from PEM string. Returns None if unavailable."""
    if not pem:
        return None
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        return load_pem_private_key(pem.encode(), password=None)
    except ImportError:
        log.warning("[aauth] cryptography not installed — AAuth signing unavailable")
        return None
    except Exception as exc:
        log.warning(f"[aauth] Could not load private key: {exc}")
        return None


def _load_private_key_jwk(data: dict, alg: str = "ES256") -> Any:
    """Load a private key from a JWK dict (EC/P-256 or OKP/Ed25519).

    Delegates to :func:`aauth_httpsig.load_private_key_from_jwk`, which
    already handles both algorithm families draft-10 §12.8.1 admits — this
    module used to carry its own EC-only copy of the same logic, which is
    exactly why it silently couldn't load the Ed25519 keys
    ``mint_daemon_keypair.py`` produces by default.
    """
    try:
        return _load_private_key_from_jwk(data)
    except Exception as exc:
        # aauth_httpsig raises AAuthSigningError (a plain Exception) for both
        # a missing `cryptography` package and a malformed/unsupported key —
        # one catch-all keeps this the same fail-safe-to-stub behavior as
        # every other loader in this module.
        log.warning(f"[aauth] Could not load JWK private key (alg={alg}): {exc}")
        return None


def _sign_jwt(
    sub: str, kid: str, private_key: Any, method: str, path: str, alg: str = "ES256"
) -> str:
    """
    Produce a minimal AAuth JWT, signed with the key's own algorithm.

    ES256 goes through PyJWT as before. Ed25519 is assembled and signed
    manually: PyJWT 2.13 only registers Ed25519 as the polymorphic "EdDSA"
    identifier that RFC 9864 deprecated and draft-10 §12.8.1 forbids on the
    wire, and the protected header must already say "Ed25519" before signing
    since it's part of the JWS signing input — mirroring the same constraint
    `aauth_httpsig._encode_jws_manually` handles for the richer agent token.
    """
    now = int(time.time())
    payload = {
        "sub": sub,
        "iat": now,
        "exp": now + 300,  # 5 min expiry
        "method": method.upper(),
        "path": path,
    }
    headers = {"alg": alg, "typ": "JWT"}
    if kid:
        headers["kid"] = kid

    if alg == "Ed25519":
        try:
            segments = [
                _b64url_nopad(json.dumps(part, separators=(",", ":")).encode("utf-8"))
                for part in (headers, payload)
            ]
            signing_input = ".".join(segments).encode("ascii")
            signature = _sign_for_alg(private_key, signing_input, alg)
            segments.append(_b64url_nopad(signature))
            return ".".join(segments)
        except ImportError:
            log.warning("[aauth] cryptography not installed — returning empty token")
            return ""
        except Exception as exc:
            log.warning(f"[aauth] JWT signing failed: {exc}")
            return ""

    try:
        import jwt  # PyJWT

        return jwt.encode(payload, private_key, algorithm=alg, headers=headers)
    except ImportError:
        log.warning("[aauth] PyJWT not installed — returning empty token")
        return ""
    except Exception as exc:
        log.warning(f"[aauth] JWT signing failed: {exc}")
        return ""


def _b64url_nopad(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
