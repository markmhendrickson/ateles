#!/usr/bin/env python3
"""Interop proof for the Python AAuth signer.

Signs a sample request with lib/daemon_runtime/aauth_httpsig and (1) emits the
signed request as JSON for the Node-side `@hellocoop/httpsig` verifier, and
(2) optionally sends it to a live Neotoma to confirm the server reports
`signature_verified: true`.

This is the reproducible version of the manual ground-truth check used when the
signer was first written (Python signs → real library verifies → live server
upgrades the request to the `software` tier).

Usage:
    # 1. Emit a signed request to stdout / file:
    python execution/scripts/verify_aauth_signer.py \
        --jwk ~/repos/ateles-private/keys/apis.jwk.json --out /tmp/aauth_probe.json

    # 2. Verify with the real library (run from the neotoma checkout):
    node --input-type=module -e '
      import { verify } from "@hellocoop/httpsig";
      import { readFileSync } from "node:fs";
      const r = await verify(JSON.parse(readFileSync("/tmp/aauth_probe.json","utf8")), { strictAAuth: true });
      console.log("verified:", r.verified, "sub:", r.jwt?.payload?.sub);
    '

    # 3. Live server check (signed GET /session, expects software tier):
    python execution/scripts/verify_aauth_signer.py \
        --jwk ~/repos/ateles-private/keys/apis.jwk.json --live http://localhost:9180
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from daemon_runtime.aauth_httpsig import HttpSigSigner  # noqa: E402


def _signer(jwk_path: Path) -> HttpSigSigner:
    jwk = json.loads(jwk_path.read_text())
    sub = jwk.get("sub") or f"{jwk_path.stem}@ateles-swarm"
    return HttpSigSigner(private_jwk=jwk, sub=sub, iss=sub, kid=jwk.get("kid"))


def emit(jwk_path: Path, out: Path | None) -> None:
    signer = _signer(jwk_path)
    url = "http://localhost:9180/entities/query"
    body = json.dumps({"entity_type": "task", "limit": 1})
    headers = {"content-type": "application/json"}
    headers.update(signer.sign_headers(method="POST", url=url, body=body))
    payload = {
        "method": "POST",
        "authority": "localhost:9180",
        "path": "/entities/query",
        "query": "",
        "body": body,
        "headers": headers,
    }
    text = json.dumps(payload, indent=2)
    if out:
        out.write_text(text)
        print(f"wrote signed request → {out}")
    else:
        print(text)


def live(jwk_path: Path, base: str) -> int:
    import httpx

    signer = _signer(jwk_path)
    url = base.rstrip("/") + "/session"
    headers = signer.sign_headers(method="GET", url=url, body=None, content_type=None)
    r = httpx.get(url, headers=headers, timeout=10)
    decision = r.json().get("attribution", {}).get("decision", {})
    verified = bool(decision.get("signature_verified"))
    print(f"status={r.status_code} signature_present={decision.get('signature_present')} "
          f"signature_verified={verified} error={decision.get('signature_error_code')} "
          f"tier={r.json().get('attribution', {}).get('tier')}")
    return 0 if verified else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jwk", required=True, type=lambda p: Path(p).expanduser())
    ap.add_argument("--out", type=lambda p: Path(p).expanduser())
    ap.add_argument("--live", help="base URL of a running Neotoma to probe")
    args = ap.parse_args(argv)

    if args.live:
        return live(args.jwk, args.live)
    emit(args.jwk, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
