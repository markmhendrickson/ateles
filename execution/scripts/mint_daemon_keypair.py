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

Creation is race- and symlink-safe:
  * no --force: the final path is opened O_CREAT|O_EXCL|O_NOFOLLOW at 0600, so
    the existence check and the create are one atomic step (no check-then-act
    window) and a symlink at the target is never followed, dangling or not;
  * --force (rotation): the new key is written to a temp file in the same
    directory opened O_CREAT|O_EXCL|O_NOFOLLOW at 0600, fsync'd, then moved
    onto the target with os.replace. The replacement is a new inode, so an
    existing 0644 file cannot leak its old mode into the new key, and a failed
    rotation leaves the OLD key intact rather than truncated in place;
  * a symlink (or any non-regular file) at the target is refused outright;
  * the keys directory is created 0700.

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
import re
import stat
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


_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class UnsafeKeyTargetError(OSError):
    """The target path is a symlink or not a regular file; refuse to touch it."""


def validate_name(name: str) -> str:
    """Normalize and validate a daemon/role name for use as a filename component.

    Strips and lowercases, then requires ``^[a-z][a-z0-9_-]{0,63}$`` — an
    allowlist rather than a denylist, so `/`, `\\`, `..`, NUL, whitespace, a
    leading dot or dash, and anything else that could smuggle a path component
    or truncate a path at the C-library level are all rejected by construction.
    Raises ValueError (never `SystemExit`) so callers and tests can catch it
    uniformly.
    """
    normalized = name.strip().lower()
    if not _NAME_RE.fullmatch(normalized):
        raise ValueError(
            f"invalid daemon name: {name!r} "
            "(must match ^[a-z][a-z0-9_-]{0,63}$ after lowercasing)"
        )
    return normalized


_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


def _write_new_file(path: Path, payload: str) -> None:
    """Create *path* exclusively at 0600, write, fsync. Removes it on failure."""
    fd = os.open(str(path), _CREATE_FLAGS, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _fsync_dir(directory: Path) -> None:
    """Best-effort durability for the rename; a failure here is not a key loss."""
    try:
        dfd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _check_target(out_path: Path) -> bool:
    """True if a regular file exists at *out_path*; refuse symlinks and non-files.

    Uses lstat, never stat/exists: those follow a symlink, so a dangling link
    would read as "absent" and a live one as the file it points at.
    """
    try:
        st = os.lstat(out_path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        raise UnsafeKeyTargetError(
            f"{out_path} is a symlink; refusing to write a private key through it"
        )
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeKeyTargetError(f"{out_path} exists and is not a regular file")
    return True


def mint(name: str, keys_dir: Path, *, force: bool = False) -> Path:
    """Generate (or, with ``force``, rotate) *name*'s AAuth private JWK.

    Raises ValueError for an invalid name (see `validate_name`),
    FileExistsError when the target already exists and `force` is False, and
    UnsafeKeyTargetError (an OSError) when the target is a symlink or not a
    regular file — all normal exceptions, not `sys.exit`, so this function
    stays testable without capturing `SystemExit`. The `cryptography`-missing
    case remains a `sys.exit` since it is an environment problem `main()`
    should report and stop for, not a condition a caller would want to catch
    and retry.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        sys.exit("ERROR: cryptography package not installed. Run: pip install cryptography")

    name = validate_name(name)

    keys_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    out_path = keys_dir / f"{name}.jwk.json"

    exists = _check_target(out_path)
    if exists and not force:
        raise FileExistsError(
            f"{out_path} already exists. Deleting it OR passing --force destroys "
            "the previous private key irrecoverably (and invalidates it for "
            "anyone who cached it); pass --force only if you intend to rotate."
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
    payload = json.dumps(jwk, indent=2) + "\n"

    if not force:
        # One atomic create-or-fail: O_EXCL makes "does it exist" and "create
        # it" a single step, so there is no check-then-act race, and O_NOFOLLOW
        # (with O_EXCL failing on any existing entry, dangling symlinks
        # included) means a link planted at the path is never written through.
        try:
            _write_new_file(out_path, payload)
        except FileExistsError:
            raise FileExistsError(
                f"{out_path} appeared while minting; refusing to overwrite it "
                "(pass --force only if you intend to rotate)."
            ) from None
        return out_path

    # Rotation: write the replacement beside the target, then swap it in.
    tmp_path = keys_dir / f".{name}.jwk.json.{os.urandom(8).hex()}.tmp"
    try:
        _write_new_file(tmp_path, payload)
        os.replace(tmp_path, out_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _fsync_dir(keys_dir)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint an AAuth keypair for a daemon.")
    parser.add_argument("--name", required=True, help="Role name (e.g. monedula); the key's sub becomes <name>@ateles-swarm")
    parser.add_argument(
        "--keys-dir",
        type=Path,
        default=_DEFAULT_KEYS_DIR,
        help=f"Directory to write keypair into (default: {_DEFAULT_KEYS_DIR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rotate: replace an existing key. This destroys the old private key irrecoverably; without the flag an existing key is left untouched.",
    )
    args = parser.parse_args()

    try:
        out_path = mint(args.name, args.keys_dir, force=args.force)
    except (ValueError, OSError) as exc:
        sys.exit(f"ERROR: {exc}")

    name = validate_name(args.name)
    # Non-secret metadata only — never the private scalar (d) or the public
    # coordinates (x/y).
    print(f"Keypair written to: {out_path}")
    print(f"  sub: {name}@ateles-swarm")
    print(f"  format: canonical JWK (ES256 P-256)")
    print(f"  mode: {stat.S_IMODE(out_path.stat().st_mode):04o}")
    print()
    print("Next: restart the daemon so it picks up the new keypair.")


if __name__ == "__main__":
    main()
