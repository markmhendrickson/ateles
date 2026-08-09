#!/usr/bin/env python3
"""
Store a `release_result` entity as phoenicurus, with an AAuth signature.

## Why this exists

`release_result` has no configured access policy, so it falls to the default
`closed` — "no guest access" (`src/services/access_policy.ts`). The prepare
agent reaches Neotoma over plain bearer-token auth, which resolves as a guest,
so its `POST /store` was denied. The agent reported this as its identity
(`pavo@ateles-swarm`) lacking a grant; that was a red herring twice over —
`pavo` is a swarm-roster persona rather than a credential, and a
`phoenicurus@ateles-swarm` grant with exactly the right `release_result`
capabilities has existed since 2026-06-23. The grant never applied because
nothing was signing (ateles#402).

The daemon already holds the keypair (`ateles-private/keys/phoenicurus.jwk.json`)
whose `sub` matches that grant. This script is the missing wire: it signs the
write so the existing grant admits it, instead of the release record being
written anonymously — or, as it has been, not at all.

Signing rather than widening the policy is the deliberate choice. `release_result`
is the audit trail for what shipped; opening it to `submit_only` would let
anything holding a bearer token write that record.

## Usage

    python3 store_release_result.py \
      --version v0.21.5 \
      --status pending_approval \
      --rc-branch release/v0.21.5 \
      --rc-pr-url https://github.com/owner/repo/pull/123

Exits 0 on success, 1 on failure. Prints the entity id on success so the caller
has a resolvable locator rather than an inferred one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

# Cloudflare 1010-blocks urllib's default UA on the hosted instance.
USER_AGENT = "phoenicurus-release/1.0"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "lib" / "daemon_runtime"))

# Bootstrap the same env file prepare.py reads. This script is invoked by the
# spawned agent, which does not inherit the daemon's in-process os.environ —
# without this, NEOTOMA_BASE_URL and NEOTOMA_BEARER_TOKEN are simply absent and
# the script exits before it ever tries to sign anything.
_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Store a signed release_result entity")
    ap.add_argument("--version", required=True, help="release tag, e.g. v0.21.5")
    ap.add_argument("--status", required=True, help="prepared|pending_approval|approved|publishing|published|failed")
    ap.add_argument("--rc-branch", default="", help="release branch name")
    ap.add_argument("--rc-pr-url", default="", help="RC pull-request URL")
    ap.add_argument("--notes", default="", help="free-text notes")
    ap.add_argument("--base-url", default=os.environ.get("NEOTOMA_BASE_URL", ""))
    ap.add_argument("--timeout", type=int, default=300,
                    help="seconds; generous by default because the hosted instance "
                         "can take 30s+ per DB-backed request (neotoma#2141)")
    args = ap.parse_args()

    base = (args.base_url or "").rstrip("/")
    if not base:
        print("NEOTOMA_BASE_URL is unset and --base-url was not given", file=sys.stderr)
        return 1

    try:
        from aauth_signer import AAuthSigner  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"could not load AAuth signer: {exc}", file=sys.stderr)
        return 1

    signer = AAuthSigner.from_key_file("phoenicurus")
    if signer.is_stub:
        # Fail loudly rather than falling back to an unsigned write. An
        # unsigned write is denied by the closed policy anyway, so "continue
        # without signing" would just reproduce the silent failure this script
        # exists to remove.
        print(
            "AAuth keypair for phoenicurus not found — refusing to attempt an "
            "unsigned write, which the closed access policy on release_result "
            "would deny. Mint it with:\n"
            "  python execution/scripts/mint_daemon_keypair.py --name phoenicurus",
            file=sys.stderr,
        )
        return 1

    entity: dict[str, object] = {
        "entity_type": "release_result",
        "version": args.version,
        "status": args.status,
    }
    # publish.py reads the `rc_*` names; the plain names are kept for continuity,
    # so write both and either reader resolves.
    if args.rc_branch:
        entity["rc_branch"] = args.rc_branch
        entity["branch"] = args.rc_branch
    if args.rc_pr_url:
        entity["rc_pr_url"] = args.rc_pr_url
        entity["release_url"] = args.rc_pr_url
    if args.notes:
        entity["notes"] = args.notes

    body = {
        "entities": [entity],
        "idempotency_key": f"release-{args.version}-{args.status}-{date.today().isoformat()}",
    }

    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    headers.update(signer.headers("POST", "/store"))

    req = urllib.request.Request(
        f"{base}/store", data=json.dumps(body).encode(), headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode(errors="replace")
        print(f"store failed: HTTP {exc.code} {detail}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        # A timeout here is genuinely ambiguous on this instance: the write
        # often lands and only the response is lost (neotoma#2141). Say so
        # rather than asserting failure, so the caller re-reads instead of
        # blindly retrying into a duplicate.
        print(
            f"store did not return: {type(exc).__name__}: {exc}\n"
            "NOTE: on the hosted instance a timeout does NOT mean the write "
            "failed — verify by querying release_result for this version before "
            "retrying.",
            file=sys.stderr,
        )
        return 1

    ents = out.get("entities") or []
    if not ents:
        print(f"store returned no entities: {json.dumps(out)[:300]}", file=sys.stderr)
        return 1
    first = ents[0]
    print(f"{first.get('entity_id')} ({first.get('action')}) as {signer.sub}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
