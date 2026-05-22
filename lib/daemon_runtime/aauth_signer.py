"""
lib/daemon_runtime/aauth_signer.py — AAuth request signing for Ateles daemons.

Each daemon has a per-role AAuth keypair stored in ateles-private/keys/.
This module loads the keypair and produces signed request headers for
Neotoma API calls, establishing per-daemon attribution on all observations.

Phase 1 status: keypairs are not yet minted. AAuthSigner.from_key_file()
returns a stub signer that logs a warning. Once keypairs are minted and
placed in ateles-private/keys/<name>.json, the stub upgrades automatically.

Key file format (ateles-private/keys/<name>.json):
    {
        "sub": "monedula@ateles-swarm",
        "key_id": "<kid>",
        "private_key_pem": "-----BEGIN EC PRIVATE KEY-----\\n..."
    }

See docs/architecture.md for AAuth identity design.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    _warned: bool = False

    @classmethod
    def from_key_file(
        cls, agent_name: str, keys_dir: Path | None = None
    ) -> AAuthSigner:
        """
        Load keypair from ateles-private/keys/<agent_name>.json.
        Returns a stub signer if the file doesn't exist.
        """
        keys_dir = keys_dir or _DEFAULT_KEYS_DIR
        key_path = keys_dir / f"{agent_name.lower()}.json"

        if not key_path.exists():
            log.warning(
                f"[{agent_name}] AAuth key not found at {key_path} — "
                "stub signer in use (Phase 1: mint keypair to enable attribution)"
            )
            return cls(sub=f"{agent_name.lower()}@ateles-swarm")

        try:
            data = json.loads(key_path.read_text())
            signer = cls(
                sub=data.get("sub", f"{agent_name.lower()}@ateles-swarm"),
                key_id=data.get("key_id", ""),
            )
            signer._private_key = _load_private_key(data.get("private_key_pem", ""))
            log.info(f"[{agent_name}] AAuth keypair loaded (sub={signer.sub})")
            return signer
        except Exception as exc:
            log.warning(
                f"[{agent_name}] Failed to load AAuth key: {exc} — stub signer in use"
            )
            return cls(sub=f"{agent_name.lower()}@ateles-swarm")

    @classmethod
    def stub(cls, agent_name: str) -> AAuthSigner:
        """Return a no-op stub signer for testing or pre-Phase-1 use."""
        return cls(sub=f"{agent_name.lower()}@ateles-swarm")

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
            token = _sign_jwt(self.sub, self.key_id, self._private_key, method, path)
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


def _sign_jwt(sub: str, kid: str, private_key: Any, method: str, path: str) -> str:
    """
    Produce a minimal AAuth JWT.
    Requires: cryptography, PyJWT or manual jose encoding.
    """
    try:
        import jwt  # PyJWT

        now = int(time.time())
        payload = {
            "sub": sub,
            "iat": now,
            "exp": now + 300,  # 5 min expiry
            "method": method.upper(),
            "path": path,
        }
        headers = {"kid": kid} if kid else {}
        return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)
    except ImportError:
        log.warning("[aauth] PyJWT not installed — returning empty token")
        return ""
    except Exception as exc:
        log.warning(f"[aauth] JWT signing failed: {exc}")
        return ""
