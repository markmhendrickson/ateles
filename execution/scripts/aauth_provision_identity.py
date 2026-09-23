#!/usr/bin/env python3
"""Provision (or rotate) a T3/T4 agent's AAuth signing identity.

Generates an ES256 P-256 keypair for one agent role and writes the PRIVATE
JWK to ``ateles-private/keys/<role>.jwk.json`` (mode 0600) — the exact path
and format ``lib/daemon_runtime/aauth_httpsig.load_http_sig_signer`` already
consumes for every daemon that signs its own Neotoma requests. It never
prints, logs, or returns key material; only non-secret metadata (thumbprint,
sub, iss, and the output path) reaches stdout.

Replaces the same-named script that existed only on unmerged migration
branches (5c37ebe9, 434bb7bd, 4a937923 — never on ``main``). That version
generated a JWK for the CURSOR-PROXY identity flavor (``.creds/*.jwk``,
consumed by the RFC 9421 ``execution/scripts/aauth_signer.py`` /
``mcp_identity_proxy.py`` path) and updated the website's published
``jwks.json``. This script targets the OTHER flavor instead — the
``ateles-private/keys/<role>.jwk.json`` PEM^H^H JWK files T3/T4 daemons and
dispatched agents load via ``aauth_httpsig`` — because that is the flavor
``docs/aauth.md`` actually points gate-owning roles at, and the one most
roles already have a key for.

WHAT THIS DOES NOT DO, AND WHY
-------------------------------
This script does not mint a "Neotoma bearer token" for ``<ROLE>_NEOTOMA_TOKEN``
in the sense the env var's name suggests (an opaque string presented as
``Authorization: Bearer <token>``). Reading Neotoma's server source
(read-only, ``origin/main``) turned up no mechanism that maps such a string
to an agent principal:

  - ``agent_grant`` admission (``src/services/aauth_admission.ts``,
    ``admitFromAAuthContext``) matches ONLY a verified AAuth signature's
    ``(sub, iss, thumbprint)`` — never a bearer token.
  - Every bearer/OAuth token Neotoma issues resolves, via
    ``mcp_oauth_connections`` (``src/services/mcp_auth.ts``,
    ``validateSessionToken``), to a human ``user_id`` — never an agent
    ``sub``. There is no per-agent service-account or scoped-API-key
    concept in the schema.
  - ``docs/subsystems/aauth.md`` states this explicitly: "Bearer tokens,
    OAuth, and MCP `connection_id` continue to resolve the human `user_id`.
    AAuth never bypasses user-scope resolution."

So a role's OWN Neotoma identity is carried by an AAuth-signed request, not
by a distinct bearer string. This script produces exactly that: a keypair
whose signature ``lib/daemon_runtime/aauth_httpsig.HttpSigSigner`` can
produce per request. The env var ``<ROLE>_NEOTOMA_TOKEN`` mentioned in
older docs and in ``execution/daemons/apis/skill_runner.py`` remains a
SEPARATE, narrower fallback — see that module's docstring — and does not
change meaning here.

Usage
-----
    python3 execution/scripts/aauth_provision_identity.py --role accipiter

    # Rotate an existing key (overwrites the file):
    python3 execution/scripts/aauth_provision_identity.py --role accipiter --force

    # Non-default issuer (rare — matches NEOTOMA_AAUTH_ISS elsewhere):
    python3 execution/scripts/aauth_provision_identity.py --role accipiter \\
        --iss https://markmhendrickson.com

The private JWK never leaves the local ``ateles-private`` checkout. To also
admit the key server-side, the OPERATOR (not this script, not an unattended
agent — this uses the operator's own authenticated Neotoma session) runs the
generic contract-driven CLI verb against the `createAgentGrant` operation:

    neotoma request --operation createAgentGrant --body '{
      "label": "accipiter",
      "match_sub": "accipiter@ateles-swarm",
      "match_iss": "https://markmhendrickson.com",
      "capabilities": [
        {"op": "retrieve", "entity_types": ["issue"]},
        {"op": "correct", "entity_types": ["issue"]}
      ]
    }'

(or the Inspector's Agents -> Grants UI, or `neotoma auth keygen --register`
if provisioning and registration happen in the same step on a machine that
also runs the signer). See ``docs/aauth.md`` for the full per-agent
activation checklist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "lib"))

from daemon_runtime.aauth_httpsig import (  # noqa: E402
    DEFAULT_AAUTH_ISSUER,
    jwk_thumbprint,
    public_part_of,
)


def _default_keys_dir() -> Path:
    """``ateles-private/keys/`` — override via ``ATELES_PRIVATE_KEYS_DIR`` for tests."""
    override = os.environ.get("ATELES_PRIVATE_KEYS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "repos" / "ateles-private" / "keys"


def _generate_p256_jwk() -> dict[str, str]:
    """Generate a fresh EC P-256 keypair and return it as a private JWK dict."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "the `cryptography` package is required to generate an AAuth "
            f"keypair but is unavailable: {exc}"
        ) from exc

    import base64

    def _b64url(n: int, length: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode("ascii")

    private_key = ec.generate_private_key(ec.SECP256R1())
    numbers = private_key.private_numbers()
    public_numbers = numbers.public_numbers
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(public_numbers.x, 32),
        "y": _b64url(public_numbers.y, 32),
        "d": _b64url(numbers.private_value, 32),
    }


def provision(
    role: str,
    *,
    iss: str = DEFAULT_AAUTH_ISSUER,
    force: bool = False,
    keys_dir: Path | None = None,
) -> dict[str, str]:
    """Generate (or, with ``force``, rotate) *role*'s AAuth private JWK.

    Returns non-secret metadata only: ``{"sub", "iss", "kid", "thumbprint",
    "path"}``. Never returns or prints the private key material.
    """
    role = role.strip().lower()
    if not role or "/" in role or ".." in role:
        raise ValueError(f"invalid role name: {role!r}")

    directory = keys_dir or _default_keys_dir()
    out_path = directory / f"{role}.jwk.json"

    if out_path.exists() and not force:
        raise FileExistsError(
            f"{out_path} already exists. Pass --force to rotate it (this "
            "invalidates the previous key for anyone who cached it — the "
            "matching agent_grant's match_thumbprint must be updated too)."
        )

    directory.mkdir(parents=True, exist_ok=True)

    sub = f"{role}@ateles-swarm"
    private_jwk = _generate_p256_jwk()
    kid = f"{role}-{jwk_thumbprint(public_part_of(private_jwk))[:8]}"
    full_jwk = {**private_jwk, "sub": sub, "iss": iss, "kid": kid}

    # Write with restrictive permissions from the start (avoid a window where
    # the file is world/group-readable between create and chmod).
    fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(full_jwk, f, indent=2)
            f.write("\n")
    finally:
        os.chmod(out_path, 0o600)

    thumbprint = jwk_thumbprint(public_part_of(full_jwk))
    return {"sub": sub, "iss": iss, "kid": kid, "thumbprint": thumbprint, "path": str(out_path)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--role", required=True, help="Agent role name, e.g. accipiter")
    parser.add_argument("--iss", default=DEFAULT_AAUTH_ISSUER, help="AAuth issuer (default: %(default)s)")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing key (rotation)")
    args = parser.parse_args(argv)

    try:
        result = provision(args.role, iss=args.iss, force=args.force)
    except FileExistsError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    # Non-secret metadata only — never the private JWK itself.
    print(f"provisioned AAuth identity for '{args.role}'")
    print(f"  sub:        {result['sub']}")
    print(f"  iss:        {result['iss']}")
    print(f"  kid:        {result['kid']}")
    print(f"  thumbprint: {result['thumbprint']}")
    print(f"  path:       {result['path']}  (mode 0600, private — never printed)")
    print()
    print(
        "Next: the OPERATOR files an agent_grant matching this (sub, iss) "
        "with the capabilities this role needs — via `neotoma request "
        "--operation createAgentGrant --body '...'` or the Inspector's "
        "Agents -> Grants UI, using the operator's own authenticated "
        "session. Not from this script or an unattended agent."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
