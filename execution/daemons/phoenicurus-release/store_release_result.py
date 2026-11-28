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
      --rc-head <full SHA of the release branch head> \
      --rc-pr-url https://github.com/owner/repo/pull/123

Exits 0 only when the write landed AND a read-back of the entity shows the
version and status just written; 1 otherwise. Prints the entity id on success
so the caller has a resolvable locator rather than an inferred one. A caller
must not send the approval request unless this exits 0.

## Idempotency

The key is bound to the candidate (`--rc-head`), not the calendar day: a
second preparation of the same version with different content is a new write
and is recorded, while a true retry of the same candidate deduplicates. The
old `version-status-date` key made the second same-day candidate fail
ERR_IDEMPOTENCY_MISMATCH, so it was never recorded as pending approval.
`--rc-head` is required for `pending_approval`; other statuses without it keep
the date-bound key.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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


_SHA_RE = re.compile(r"[0-9a-f]{40}")


def idempotency_key(version: str, status: str, rc_head: str) -> str:
    """Bind the key to the candidate head; fall back to the day without one."""
    if rc_head:
        return f"release-{version}-{status}-{rc_head}"
    return f"release-{version}-{status}-{date.today().isoformat()}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Store a signed release_result entity")
    ap.add_argument("--version", required=True, help="release tag, e.g. v0.21.5")
    ap.add_argument(
        "--status",
        required=True,
        help="prepared|pending_approval|approved|publishing|published|failed",
    )
    ap.add_argument("--rc-branch", default="", help="release branch name")
    ap.add_argument(
        "--rc-head",
        default="",
        help="full commit SHA of the release branch head (required for pending_approval)",
    )
    ap.add_argument("--rc-pr-url", default="", help="RC pull-request URL")
    ap.add_argument("--notes", default="", help="free-text notes")
    ap.add_argument("--base-url", default=os.environ.get("NEOTOMA_BASE_URL", ""))
    ap.add_argument("--timeout", type=int, default=300,
                    help="seconds; generous by default because the hosted instance "
                         "can take 30s+ per DB-backed request (neotoma#2141)")
    args = ap.parse_args()

    rc_head = args.rc_head.strip().lower()
    if rc_head and not _SHA_RE.fullmatch(rc_head):
        print(f"--rc-head must be a full 40-character commit SHA, got {args.rc_head!r}",
              file=sys.stderr)
        return 1
    if args.status == "pending_approval" and not rc_head:
        print(
            "--rc-head is required for pending_approval: the record must be bound "
            "to the candidate it asks the operator to approve",
            file=sys.stderr,
        )
        return 1

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
    if rc_head:
        entity["rc_head_sha"] = rc_head
    if args.rc_pr_url:
        entity["rc_pr_url"] = args.rc_pr_url
        entity["release_url"] = args.rc_pr_url
    if args.notes:
        entity["notes"] = args.notes

    body = {
        "entities": [entity],
        "idempotency_key": idempotency_key(args.version, args.status, rc_head),
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
    entity_id = str(first.get("entity_id") or "")
    if not entity_id:
        print(f"store returned no entity id: {json.dumps(first)[:300]}", file=sys.stderr)
        return 1

    problem = _read_back(base, entity_id, args, bearer, signer)
    if problem:
        print(
            f"store of {entity_id} not confirmed by read-back: {problem}\n"
            "Do NOT send the approval request: the release is not recorded.",
            file=sys.stderr,
        )
        return 1
    print(f"{entity_id} ({first.get('action')}) as {signer.sub}")
    return 0


def _read_back(base: str, entity_id: str, args, bearer: str, signer) -> str | None:
    """Fetch the stored entity; return a problem string, or None if it matches."""
    headers = {"User-Agent": USER_AGENT}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    path = f"/entities/{entity_id}"
    headers.update(signer.headers("GET", path))
    req = urllib.request.Request(f"{base}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            got = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} {exc.read()[:200].decode(errors='replace')}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    if not isinstance(got, dict):
        return f"unexpected response {json.dumps(got)[:200]}"
    fields = got.get("snapshot") or got.get("fields") or got
    for key, want in (("version", args.version), ("status", args.status)):
        if str(fields.get(key) or "") != want:
            return f"{key} is {fields.get(key)!r}, want {want!r}"
    if args.rc_branch:
        branch = str(fields.get("rc_branch") or fields.get("branch") or "")
        if branch != args.rc_branch:
            return f"rc_branch is {branch!r}, want {args.rc_branch!r}"
    return None


if __name__ == "__main__":
    sys.exit(main())
