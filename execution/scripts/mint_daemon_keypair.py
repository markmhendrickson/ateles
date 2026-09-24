#!/usr/bin/env python3
"""
execution/scripts/mint_daemon_keypair.py — Generate an ES256 P-256 keypair for a daemon.

Writes ateles-private/keys/<name>.jwk.json in canonical JWK format.
The file is mode 0600 from the moment it is created — no window where a
default-umask file exists before permissions are tightened — and contains
both the private scalar and public coordinates so aauth_signer.py can load
it without a separate public-key file. Never prints the private scalar (`d`)
or the public coordinates (`x`/`y`); only non-secret metadata (`sub`, `kid`,
path, mode) reaches stdout.

This is the CANONICAL keypair-minting script for T3/T4 agent roles — see
docs/aauth/keys.md. Do not add a second script that writes the same
ateles-private/keys/<name>.jwk.json format; extend this one instead
(docs/foundation/principles.md "extend the mechanism that already
generalizes; do not build a parallel one" — a prior PR round added exactly
such a parallel script, `aauth_provision_identity.py`, before this rule was
applied and it was deleted in favour of hardening this file instead).

Usage:
    python execution/scripts/mint_daemon_keypair.py --name monedula
    python execution/scripts/mint_daemon_keypair.py --name cicada --keys-dir /path/to/keys
    python execution/scripts/mint_daemon_keypair.py --name monedula --force  # rotate
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


def validate_name(name: str) -> str:
    """Normalize and validate a daemon/role name for use as a filename component.

    Rejects anything that could smuggle a path component past the intended
    `<keys_dir>/<name>.jwk.json` target: `/` and `..` (POSIX traversal),
    `\\` (a path separator on Windows, and a way to smuggle a component past
    POSIX-only `/`/`..` checks), and a NUL byte (can truncate a path at the
    C-library level, silently discarding whatever follows it, e.g. the
    `.jwk.json` extension). Raises ValueError on any of these; never raises
    on a bare `SystemExit`, so callers (and tests) can catch it uniformly.
    """
    normalized = name.strip().lower()
    if (
        not normalized
        or "/" in normalized
        or "\\" in normalized
        or ".." in normalized
        or "\x00" in normalized
    ):
        raise ValueError(f"invalid daemon name: {name!r}")
    return normalized


def mint(name: str, keys_dir: Path, *, force: bool = False) -> Path:
    """Generate (or, with ``force``, rotate) *name*'s AAuth private JWK.

    Raises ValueError for an invalid name (see `validate_name`) and
    FileExistsError when the target already exists and `force` is False —
    both normal exceptions, not `sys.exit`, so this function stays testable
    without capturing `SystemExit`. The `cryptography`-missing case remains
    a `sys.exit` since it is an environment problem `main()` should report
    and stop for, not a condition a caller would want to catch and retry.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        sys.exit("ERROR: cryptography package not installed. Run: pip install cryptography")

    name = validate_name(name)

    keys_dir.mkdir(parents=True, exist_ok=True)
    out_path = keys_dir / f"{name}.jwk.json"

    if out_path.exists() and not force:
        raise FileExistsError(
            f"{out_path} already exists. Delete it first, or pass --force, if "
            "you intend to rotate (this invalidates the previous key for "
            "anyone who cached it)."
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

    # Write with 0600 from the moment the file is created — os.open + fdopen,
    # not write_text() + a later chmod(), which leaves a window where the
    # file exists at the process's default umask (commonly 0644, world- and
    # group-readable) before permissions are tightened.
    fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(jwk, indent=2) + "\n")
    finally:
        os.chmod(out_path, 0o600)
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing key (rotation). Without this flag, an existing key is left untouched.",
    )
    args = parser.parse_args()

    try:
        out_path = mint(args.name, args.keys_dir, force=args.force)
    except (ValueError, FileExistsError) as exc:
        sys.exit(f"ERROR: {exc}")

    name = validate_name(args.name)
    # Non-secret metadata only — never the private scalar (d) or the public
    # coordinates (x/y).
    print(f"Keypair written to: {out_path}")
    print(f"  sub: {name}@ateles-swarm")
    print(f"  format: canonical JWK (ES256 P-256)")
    print(f"  mode: 0600")
    print()
    print("Next: restart the daemon so it picks up the new keypair.")


if __name__ == "__main__":
    main()
