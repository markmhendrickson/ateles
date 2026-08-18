#!/usr/bin/env python3
"""
execution/scripts/mint_daemon_keypair.py — Generate an AAuth keypair for a daemon.

Defaults to Ed25519, which draft-hardt-oauth-aauth-protocol-10 §12.8.1 makes a
MUST for agents and resources; ES256 (EC/P-256) remains available via
--alg ES256 for verifiers that do not yet accept Ed25519.

Writes ateles-private/keys/<name>.jwk.json in canonical JWK format.
The file is mode 0600 and contains both the private scalar and public
coordinates so aauth_signer.py can load it without a separate public-key file.

Usage:
    python execution/scripts/mint_daemon_keypair.py --name monedula
    python execution/scripts/mint_daemon_keypair.py --name cicada --keys-dir /path/to/keys
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from daemon_runtime.aauth_identifier import (  # noqa: E402
    LEGACY_SUFFIX,
    build_agent_identifier,
)

# Default keys directory: ateles-private repo alongside ateles.
_DEFAULT_KEYS_DIR = Path(
    os.environ.get(
        "ATELES_PRIVATE_KEYS_DIR",
        str(Path(__file__).parent.parent.parent.parent / "ateles-private" / "keys"),
    )
)


def _int_to_b64url(n: int, byte_length: int = 32) -> str:
    raw = n.to_bytes(byte_length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _bytes_to_b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _ed25519_jwk() -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    raw_priv = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_pub = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "Ed25519",
        "x": _bytes_to_b64url(raw_pub),
        "d": _bytes_to_b64url(raw_priv),
    }


def _es256_jwk() -> dict:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric.ec import (
        SECP256R1,
        generate_private_key,
    )

    private_key = generate_private_key(SECP256R1(), default_backend())
    pub = private_key.public_key().public_numbers()
    priv = private_key.private_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "alg": "ES256",
        "x": _int_to_b64url(pub.x),
        "y": _int_to_b64url(pub.y),
        "d": _int_to_b64url(priv.private_value),
    }


def mint(name: str, keys_dir: Path, alg: str = "Ed25519") -> Path:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        sys.exit("ERROR: cryptography package not installed. Run: pip install cryptography")

    keys_dir.mkdir(parents=True, exist_ok=True)
    out_path = keys_dir / f"{name}.jwk.json"

    if out_path.exists():
        sys.exit(
            f"ERROR: {out_path} already exists. Delete it first if you intend to rotate."
        )

    key_material = _ed25519_jwk() if alg == "Ed25519" else _es256_jwk()
    kid = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()

    # draft-10 §5.1 defines the subject as an aauth: URI whose domain is the
    # agent provider's — build_agent_identifier(name) below is that form.
    # But the *stored* sub stays the legacy `<name>@ateles-swarm` form until
    # ATELES_AAUTH_SPEC_IDENTIFIERS is flipped: normalize_for_wire() passes a
    # stored sub through unchanged when the flag is off, on the assumption
    # that what's stored is the legacy form the ~25 live agent_grant entities
    # still match on. Storing the spec form here would present it on the
    # wire regardless of the flag and fail admission for every re-minted key
    # — the same flag-day outage the gate exists to prevent, just triggered
    # by minting instead of the flag. `alg` is included per §12.8.1 — a
    # verifier MUST reject a key whose alg is absent.
    jwk = {
        "sub": f"{name}{LEGACY_SUFFIX}",
        "kid": kid,
        **key_material,
    }

    out_path.write_text(json.dumps(jwk, indent=2) + "\n")
    out_path.chmod(0o600)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint an AAuth keypair for a daemon.")
    parser.add_argument("--name", required=True, help="Daemon name (e.g. monedula)")
    parser.add_argument(
        "--alg",
        choices=["Ed25519", "ES256"],
        default="Ed25519",
        help="Signing algorithm (default: Ed25519, a MUST in draft-10 §12.8.1)",
    )
    parser.add_argument(
        "--keys-dir",
        type=Path,
        default=_DEFAULT_KEYS_DIR,
        help=f"Directory to write keypair into (default: {_DEFAULT_KEYS_DIR})",
    )
    args = parser.parse_args()

    name = args.name.lower()
    out_path = mint(name, args.keys_dir, args.alg)
    print(f"Keypair written to: {out_path}")
    print(f"  sub (on the wire today): {name}{LEGACY_SUFFIX}")
    print(f"  sub (draft-10, once ATELES_AAUTH_SPEC_IDENTIFIERS=1): {build_agent_identifier(name)}")
    print(f"  format: canonical JWK ({args.alg})")
    print("  mode: 0600")
    print()
    print("Next: restart the daemon so it picks up the new keypair.")


if __name__ == "__main__":
    main()
