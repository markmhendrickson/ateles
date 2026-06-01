#!/usr/bin/env python3
"""
execution/scripts/manage_grants.py — Manage agent_grant lifecycle.

Suspend, restore, revoke, or list agent grants stored in Neotoma.

Usage:
    python execution/scripts/manage_grants.py list [--sub formica@ateles-swarm]
    python execution/scripts/manage_grants.py suspend <grant_id> [--reason "…"]
    python execution/scripts/manage_grants.py restore <grant_id>
    python execution/scripts/manage_grants.py revoke  <grant_id> [--reason "…"]

Reads NEOTOMA_BEARER_TOKEN and NEOTOMA_BASE_URL from environment (or
~/.config/neotoma/.env if present).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# ── env bootstrap ──────────────────────────────────────────────────────────────
_ENV_PATH = Path.home() / ".config" / "neotoma" / ".env"
if _ENV_PATH.exists():
    for line in _ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.grant_checker import (  # noqa: E402
    GrantChecker,
    restore_grant,
    revoke_grant,
    suspend_grant,
)


def cmd_list(args: argparse.Namespace) -> None:
    sub = args.sub or ""
    checker = GrantChecker(sub).load() if sub else _list_all_grants()
    if isinstance(checker, list):
        grants = checker
    else:
        grants = checker.grants
    if not grants:
        print("No grants found.")
        return
    for g in grants:
        status_str = {
            "active": "✓ active",
            "suspended": "⏸ suspended",
            "revoked": "✗ revoked",
        }.get(g.status, g.status)
        print(f"{g.entity_id}  {g.aauth_sub:<32}  {status_str}")
        if g.capabilities:
            print(f"    capabilities: {', '.join(g.capabilities)}")
        if g.suspended_reason:
            print(f"    suspended: {g.suspended_at}  reason: {g.suspended_reason}")
        if g.revoked_reason:
            print(f"    revoked:   {g.revoked_at}  reason: {g.revoked_reason}")


def _list_all_grants() -> list:
    import httpx
    base = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not token:
        sys.exit("ERROR: NEOTOMA_BEARER_TOKEN not set")
    try:
        resp = httpx.get(
            f"{base}/entities",
            params={"entity_type": "agent_grant", "include_snapshots": "true", "limit": 200},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        entities = resp.json().get("entities", [])
        return [GrantChecker._parse(e) for e in entities]
    except Exception as exc:
        sys.exit(f"ERROR: {exc}")


def cmd_suspend(args: argparse.Namespace) -> None:
    ok = suspend_grant(args.grant_id, args.reason or "")
    if ok:
        print(f"Grant {args.grant_id} suspended.")
    else:
        sys.exit("Failed — check logs.")


def cmd_restore(args: argparse.Namespace) -> None:
    ok = restore_grant(args.grant_id)
    if ok:
        print(f"Grant {args.grant_id} restored to active.")
    else:
        sys.exit("Failed — check logs.")


def cmd_revoke(args: argparse.Namespace) -> None:
    if not args.reason:
        confirm = input(f"Revoke grant {args.grant_id} with no reason? Requires re-consent to restore. [y/N] ")
        if confirm.lower() != "y":
            print("Aborted.")
            return
    ok = revoke_grant(args.grant_id, args.reason or "")
    if ok:
        print(f"Grant {args.grant_id} revoked.")
    else:
        sys.exit("Failed — check logs.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Ateles agent_grant lifecycle.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List all grants (or filter by sub)")
    p_list.add_argument("--sub", help="Filter by aauth_sub (e.g. formica@ateles-swarm)")

    p_suspend = sub.add_parser("suspend", help="Suspend a grant")
    p_suspend.add_argument("grant_id", help="Neotoma entity ID of the agent_grant")
    p_suspend.add_argument("--reason", default="", help="Reason for suspension")

    p_restore = sub.add_parser("restore", help="Restore a suspended grant to active")
    p_restore.add_argument("grant_id", help="Neotoma entity ID of the agent_grant")

    p_revoke = sub.add_parser("revoke", help="Revoke a grant (requires re-consent to restore)")
    p_revoke.add_argument("grant_id", help="Neotoma entity ID of the agent_grant")
    p_revoke.add_argument("--reason", default="", help="Reason for revocation")

    args = parser.parse_args()
    {"list": cmd_list, "suspend": cmd_suspend, "restore": cmd_restore, "revoke": cmd_revoke}[
        args.command
    ](args)


if __name__ == "__main__":
    main()
