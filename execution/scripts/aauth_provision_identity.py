#!/usr/bin/env python3
"""Provision AAuth agent identity for markmhendrickson.com.

Generates an ES256 (P-256 ECDSA) keypair for one agent identity (sub),
writes the public JWK *additively* into the website's `.well-known/jwks.json`
(any pre-existing key with the same `kid` is replaced), updates
`.well-known/aauth-agent.json` so its `subjects_supported` lists every
provisioned subject, and saves the private JWK to `.creds/` for the Python
proxy to load. Mints a sample `aa-agent+jwt` and verifies it parses.

The same `iss` (`https://markmhendrickson.com`) is shared by every agent
the user controls. Each *distinct agent product* gets its own `sub` and
its own keypair so:

  - Cursor IDE proxy   -> sub `cursor@markmhendrickson.com`,  kid `sw-cursor-1`
  - Public MCP server  -> sub `mcp@markmhendrickson.com`,     kid `sw-mcp-1`
  - YubiKey (later)    -> sub `cursor@markmhendrickson.com`,  kid `hw-cursor-1`

All keys live side-by-side in the published JWKS; the Resource Server
matches by (sub, iss, [optional thumbprint]) on each request, so the same
sub can be served by software OR hardware-backed keys interchangeably.

Outputs:
  - .creds/aauth_agent_<role>.private.jwk  (gitignored, read by the proxy)
  - execution/website/markmhendrickson/react-app/public/.well-known/jwks.json
  - execution/website/markmhendrickson/react-app/public/.well-known/aauth-agent.json

Examples:
  Provision the Cursor proxy identity (re-run with --force to rotate):
      .venv/bin/python execution/scripts/aauth_provision_identity.py \
          --sub cursor@markmhendrickson.com --kid sw-cursor-1 --force

  Add a second identity (no --force needed; this is additive):
      .venv/bin/python execution/scripts/aauth_provision_identity.py \
          --sub mcp@markmhendrickson.com --kid sw-mcp-1
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

REPO_ROOT = Path(__file__).resolve().parents[2]
CREDS_DIR = REPO_ROOT / ".creds"
WELL_KNOWN_DIR = (
    REPO_ROOT
    / "execution"
    / "website"
    / "markmhendrickson"
    / "react-app"
    / "public"
    / ".well-known"
)

JWKS_PATH = WELL_KNOWN_DIR / "jwks.json"
AAUTH_AGENT_METADATA_PATH = WELL_KNOWN_DIR / "aauth-agent.json"

ISS = "https://markmhendrickson.com"
ALG = "ES256"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _ec_public_jwk(
    public_key: ec.EllipticCurvePublicKey, *, kid: str
) -> dict[str, Any]:
    nums = public_key.public_numbers()
    x = nums.x.to_bytes(32, "big")
    y = nums.y.to_bytes(32, "big")
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(x),
        "y": _b64url(y),
        "alg": ALG,
        "use": "sig",
        "kid": kid,
    }


def _ec_private_jwk(
    private_key: ec.EllipticCurvePrivateKey, *, kid: str
) -> dict[str, Any]:
    public_jwk = _ec_public_jwk(private_key.public_key(), kid=kid)
    d = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_jwk["d"] = _b64url(d)
    return public_jwk


def _jwk_thumbprint(public_jwk: dict[str, Any]) -> str:
    """RFC 7638 JWK Thumbprint over the canonical EC member set."""
    canonical = json.dumps(
        {
            "crv": public_jwk["crv"],
            "kty": public_jwk["kty"],
            "x": public_jwk["x"],
            "y": public_jwk["y"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return _b64url(hashlib.sha256(canonical).digest())


def _role_from_sub(sub: str) -> str:
    """Derive a filesystem-safe role label from the subject local part."""
    local = sub.split("@", 1)[0]
    role = re.sub(r"[^A-Za-z0-9_-]+", "_", local).strip("_") or "agent"
    return role


def _private_jwk_path(sub: str) -> Path:
    return CREDS_DIR / f"aauth_agent_{_role_from_sub(sub)}.private.jwk"


def _write_json(path: Path, payload: Any, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_existing_jwks() -> list[dict[str, Any]]:
    payload = _read_json(JWKS_PATH)
    if not payload:
        return []
    keys = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(keys, list):
        return []
    return [k for k in keys if isinstance(k, dict)]


def _load_existing_metadata() -> dict[str, Any]:
    payload = _read_json(AAUTH_AGENT_METADATA_PATH)
    if isinstance(payload, dict):
        return payload
    return {}


def _private_key_pem(private_key: ec.EllipticCurvePrivateKey) -> str:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _mint_sample_jwt(
    private_key: ec.EllipticCurvePrivateKey,
    public_jwk: dict[str, Any],
    *,
    sub: str,
    kid: str,
    jkt: str,
) -> str:
    now = int(time.time())
    claims = {
        "iss": ISS,
        "sub": sub,
        "iat": now,
        "exp": now + 300,
        "jkt": jkt,
        "cnf": {"jwk": public_jwk},
    }
    headers = {"typ": "aa-agent+jwt", "kid": kid}
    return jwt.encode(
        claims, _private_key_pem(private_key), algorithm=ALG, headers=headers
    )


def _verify_sample_jwt(token: str, public_jwk: dict[str, Any]) -> dict[str, Any]:
    public_pem = jwt.algorithms.ECAlgorithm.from_jwk(
        json.dumps(public_jwk)
    ).public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return jwt.decode(
        token,
        public_pem,
        algorithms=[ALG],
        options={"require": ["iss", "sub", "iat", "exp", "jkt"]},
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sub",
        required=True,
        help="AAuth subject for this agent, e.g. 'cursor@markmhendrickson.com'.",
    )
    parser.add_argument(
        "--kid",
        help=(
            "Key id published in the JWKS. Defaults to 'sw-<role>-1' where "
            "<role> is the local part of --sub."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite the private JWK file and replace any JWKS key with the "
            "same kid. Without --force, refuses to overwrite an existing "
            "private JWK or kid."
        ),
    )
    args = parser.parse_args()

    sub: str = args.sub.strip()
    if not sub or "@" not in sub:
        raise SystemExit("--sub must be a non-empty value containing '@'.")
    role = _role_from_sub(sub)
    kid: str = (args.kid or f"sw-{role}-1").strip()
    if not kid:
        raise SystemExit("--kid must be a non-empty value.")

    private_path = _private_jwk_path(sub)
    if private_path.exists() and not args.force:
        raise SystemExit(
            f"Refusing to overwrite existing private JWK at {private_path.relative_to(REPO_ROOT)}. "
            "Pass --force to rotate this agent's key."
        )

    existing_keys = _load_existing_jwks()
    if any(k.get("kid") == kid for k in existing_keys) and not args.force:
        raise SystemExit(
            f"Refusing to replace existing JWKS entry kid='{kid}'. "
            "Pass --force to rotate this key, or pass a different --kid."
        )

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    public_jwk = _ec_public_jwk(public_key, kid=kid)
    private_jwk = _ec_private_jwk(private_key, kid=kid)
    jkt = _jwk_thumbprint(public_jwk)

    _write_json(private_path, private_jwk, mode=0o600)

    merged_keys = [k for k in existing_keys if k.get("kid") != kid]
    merged_keys.append(public_jwk)
    merged_keys.sort(key=lambda k: k.get("kid", ""))
    _write_json(JWKS_PATH, {"keys": merged_keys})

    metadata = _load_existing_metadata()
    metadata["iss"] = ISS
    metadata["jwks_uri"] = f"{ISS}/.well-known/jwks.json"
    metadata["agent_token_types_supported"] = sorted(
        set(metadata.get("agent_token_types_supported", []) + ["aa-agent+jwt"])
    )
    metadata["alg_values_supported"] = sorted(
        set(metadata.get("alg_values_supported", []) + [ALG])
    )
    subjects = set(metadata.get("subjects_supported", []))
    subjects.add(sub)
    metadata["subjects_supported"] = sorted(subjects)
    _write_json(AAUTH_AGENT_METADATA_PATH, metadata)

    sample_token = _mint_sample_jwt(private_key, public_jwk, sub=sub, kid=kid, jkt=jkt)
    decoded = _verify_sample_jwt(sample_token, public_jwk)

    print(f"AAuth identity provisioned for {sub}\n")
    print(f"  iss      = {ISS}")
    print(f"  sub      = {sub}")
    print(f"  alg      = {ALG}")
    print(f"  kid      = {kid}")
    print(f"  jkt      = {jkt}")
    print()
    print(f"  private  = {private_path.relative_to(REPO_ROOT)} (mode 600, gitignored)")
    print(
        f"  public   = {JWKS_PATH.relative_to(REPO_ROOT)}  ({len(merged_keys)} key(s) total)"
    )
    print(f"  metadata = {AAUTH_AGENT_METADATA_PATH.relative_to(REPO_ROOT)}")
    print(f"           subjects_supported = {metadata['subjects_supported']}")
    print()
    print("  sample aa-agent+jwt (300s TTL):")
    print(f"  {sample_token}")
    print()
    print("  decoded payload (verified against the public JWK):")
    print(json.dumps(decoded, indent=2, sort_keys=True))
    print()
    print("Next:")
    print("  1) Back up the private JWK to 1Password:")
    print(
        f"     op item create --category 'API Credential' --title 'AAuth: {sub}' "
        f"--vault Personal 'private_jwk[concealed]={private_path}'"
    )
    print("  2) Commit the public files to the website repo and push to deploy.")
    print("     (Both files live under react-app/public/.well-known/.)")
    print("  3) After deploy, verify endpoints:")
    print(f"     curl -fsS {ISS}/.well-known/jwks.json | jq")
    print(f"     curl -fsS {ISS}/.well-known/aauth-agent.json | jq")

    return 0


if __name__ == "__main__":
    sys.exit(main())
