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


def _httpsig():
    """Import `lib/daemon_runtime/aauth_httpsig` lazily.

    The RFC 7638 thumbprint has ONE implementation, `aauth_httpsig.jwk_thumbprint`
    (also what `gate_waive._lens_key_thumbprint` uses to compare an observation's
    `agent_thumbprint`), so the value printed here cannot drift from the value the
    dispatcher computes. The import is lazy so a missing `cryptography` still
    produces `mint()`'s friendly message rather than an import traceback at load.
    """
    lib = str(Path(__file__).resolve().parent.parent.parent / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    from daemon_runtime import aauth_httpsig

    return aauth_httpsig


def key_file_thumbprint(path: Path) -> str:
    """Thumbprint of the public half of a key file: a thin wrapper, no algorithm here.

    The file contains the private scalar `d`; only its public members reach the
    hash (`public_part_of` drops `d`), and only the resulting public thumbprint
    is returned.
    """
    h = _httpsig()
    jwk = json.loads(Path(path).read_text())
    return h.jwk_thumbprint(h.public_part_of(jwk))


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


def _print_thumbprint(name: str, keys_dir: Path) -> None:
    """Read-only: print the public thumbprint of an existing key and nothing else.

    Writes nothing and creates nothing (not even the keys directory). The file
    contains the private scalar `d`; only the thumbprint of its public members is
    printed, and that value is public.
    """
    try:
        path = keys_dir / f"{validate_name(name)}.jwk.json"
        if not _check_target(path):
            sys.exit(f"ERROR: no key at {path}")
        print(key_file_thumbprint(path))
    except (ValueError, OSError) as exc:
        sys.exit(f"ERROR: {exc}")


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
    parser.add_argument(
        "--print-thumbprint",
        action="store_true",
        help="Read-only: print the RFC 7638 thumbprint (public) of an existing key for --name and exit. Writes nothing.",
    )
    args = parser.parse_args()

    try:
        _httpsig()  # fail before any key is written if the helper cannot load
    except ImportError as exc:
        sys.exit(
            f"ERROR: {exc.name or 'a dependency'} not installed. "
            "Run: pip install cryptography pyjwt"
        )

    if args.print_thumbprint:
        if args.force:
            sys.exit("ERROR: --print-thumbprint is read-only and cannot be combined with --force")
        _print_thumbprint(args.name, args.keys_dir)
        return

    try:
        # Message-only: a --force over an existing key is a rotation, and must
        # not read like a first mint. (mint() itself does its own lstat checks.)
        rotated = args.force and os.path.lexists(
            args.keys_dir / f"{validate_name(args.name)}.jwk.json"
        )
        out_path = mint(args.name, args.keys_dir, force=args.force)
    except (ValueError, OSError) as exc:
        sys.exit(f"ERROR: {exc}")

    name = validate_name(args.name)
    sub = f"{name}@ateles-swarm"
    # Non-secret metadata only — never the private scalar (d) or the public
    # coordinates (x/y). The RFC 7638 thumbprint is a hash of the public key: it
    # is what a grant pins, and it is public.
    thumbprint = key_file_thumbprint(out_path)
    if rotated:
        print(f"Keypair ROTATED at: {out_path}")
        print("  the previous private key has been destroyed and cannot be recovered")
    else:
        print(f"Keypair written to: {out_path}")
    print(f"  sub: {sub}")
    print(f"  format: canonical JWK (ES256 P-256)")
    print(f"  mode: {stat.S_IMODE(out_path.stat().st_mode):04o}")
    print(f"  thumbprint (RFC 7638, public): {thumbprint}")
    print()
    verify_cmd = (
        "python3 execution/scripts/verify_aauth_signer.py "
        f"--jwk {out_path} --live <neotoma-base-url>"
    )
    check_cmd = (
        "neotoma request --operation listAgentGrants "
        f"--query '{{\"q\": \"{thumbprint}\", \"status\": \"active\"}}'"
    )
    create_cmd = (
        "neotoma request --operation createAgentGrant --body "
        f"'{{\"label\": \"{name}\", \"match_sub\": \"{sub}\", "
        f"\"match_thumbprint\": \"{thumbprint}\", "
        "\"capabilities\": [<the ops and entity types this role needs>]}'"
    )
    iss_note = (
        "match_iss is optional: use the issuer your deployment signs with "
        "(NEOTOMA_AAUTH_ISS), or leave it out; the thumbprint is what admits."
    )
    shape_note = (
        "(Command shapes are read from Neotoma's CLI source and openapi.yaml; "
        "they have not been run against a live instance.)"
    )
    if rotated:
        # Admission is key-bound: the old grant pins the OLD key and does NOT
        # admit the new one. The dispatcher's gate write-back reads the key file on
        # every call, so it signs with the new key immediately and is refused until
        # the new key is pinned, while a long-running daemon keeps signing with the
        # old key it loaded until restart. So: pin the new key with a SECOND grant
        # (updating the old grant in place would refuse the still-running old key),
        # restart, then revoke the old grant.
        print("Rotation: the old grant pins the OLD key and does not admit this one.")
        print(
            f"Signed writes as {sub} that read the key file per call (the dispatcher's "
            "gate write-back) are refused until the new key is pinned; a running daemon "
            "keeps signing with the old key until it restarts."
        )
        print('In this order (exact update/revoke commands: docs/aauth/keys.md, "Rotation"):')
        print(f"  1. Create a SECOND grant pinning the new key: {create_cmd}")
        print(f"     {iss_note}")
        print(f"  2. Check the pin exists: {check_cmd}")
        print("  3. Restart the daemon so it signs with the new key, then re-verify:")
        print(f"       {verify_cmd}")
        print("  4. Revoke the OLD grant (find its grant_id with listAgentGrants).")
        print(f"     {shape_note}")
    else:
        # Same order as docs/aauth.md "Identity provisioning": mint (done),
        # register the grant PINNING this key, check it, verify, restart.
        print('Step 1 (mint) is done. Next, in this order (docs/aauth.md, "Identity provisioning"):')
        print(f"  2. Register the agent_grant for {sub}, pinning this key (operator): {create_cmd}")
        print(f"     {iss_note}")
        print("     Without match_thumbprint the grant admits nothing on current Neotoma.")
        print(f"     {shape_note}")
        print(f"  3. Check the pin exists: {check_cmd}")
        print(f"  4. Verify the signer: {verify_cmd}")
        print("  5. Restart the daemon so it picks up the new keypair.")
    print(
        "Note: the dispatcher reads ATELES_AAUTH_KEYS_DIR; it must point at the "
        "same directory as this key."
    )


if __name__ == "__main__":
    main()
