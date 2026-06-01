#!/usr/bin/env python3
"""
execution/scripts/mint_daemon_keypair.py — Generate an ES256 P-256 keypair for a daemon.

Writes ateles-private/keys/<name>.jwk.json in canonical JWK format.
The file is mode 0600 and contains both the private scalar and public
coordinates so aauth_signer.py can load it without a separate public-key file.

Usage:
    python execution/scripts/mint_daemon_keypair.py --name monedula
    python execution/scripts/mint_daemon_keypair.py --name gryllus --keys-dir /path/to/keys
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

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


def mint(name: str, keys_dir: Path) -> Path:
    try:
        from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        sys.exit("ERROR: cryptography package not installed. Run: pip install cryptography")

    keys_dir.mkdir(parents=True, exist_ok=True)
    out_path = keys_dir / f"{name}.jwk.json"

    if out_path.exists():
        sys.exit(
            f"ERROR: {out_path} already exists. Delete it first if you intend to rotate."
        )

    private_key = generate_private_key(SECP256R1(), default_backend())
    pub = private_key.public_key().public_numbers()
    priv = private_key.private_numbers()

    kid = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()

    jwk = {
        "sub": f"{name}@ateles-swarm",
        "kid": kid,
        "kty": "EC",
        "crv": "P-256",
        "x": _int_to_b64url(pub.x),
        "y": _int_to_b64url(pub.y),
        "d": _int_to_b64url(priv.private_value),
    }

    out_path.write_text(json.dumps(jwk, indent=2) + "\n")
    out_path.chmod(0o600)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint an AAuth keypair for a daemon.")
    parser.add_argument("--name", required=True, help="Daemon name (e.g. monedula)")
    parser.add_argument(
        "--keys-dir",
        type=Path,
        default=_DEFAULT_KEYS_DIR,
        help=f"Directory to write keypair into (default: {_DEFAULT_KEYS_DIR})",
    )
    args = parser.parse_args()

    name = args.name.lower()
    out_path = mint(name, args.keys_dir)
    print(f"Keypair written to: {out_path}")
    print(f"  sub: {name}@ateles-swarm")
    print(f"  format: canonical JWK (ES256 P-256)")
    print(f"  mode: 0600")
    print()
    print("Next: restart the daemon so it picks up the new keypair.")


if __name__ == "__main__":
    main()
