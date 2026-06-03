#!/usr/bin/env python3
"""
Revert an autonomously-generalized agent_policy in one command.

The generalizer auto-applies agent-local policies in `provisional` status and
matures them to `active` by exposure. This is the manual escape hatch: retire a
specific policy by entity ID, or sweep every auto-generated policy for an agent
back to `retired` (e.g. if a generalization went sideways).

    NEOTOMA_BEARER_TOKEN=... python3 scripts/revert_auto_policy.py --id ent_abc123
    NEOTOMA_BEARER_TOKEN=... python3 scripts/revert_auto_policy.py --agent pavo --all
    NEOTOMA_BEARER_TOKEN=... python3 scripts/revert_auto_policy.py --agent pavo --all --dry-run

Only ever touches policies flagged auto_generated in their notes — never an
operator- or Columba-authored policy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

import httpx

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")


def _headers(bearer: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}


def _is_auto(body: str) -> bool:
    """Maturation metadata lives in the agent_policy `body` field as JSON."""
    try:
        return bool(json.loads(body or "{}").get("auto_generated"))
    except (ValueError, TypeError):
        return False


def _retire(client: httpx.Client, entity_id: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] would retire {entity_id}")
        return
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    resp = client.post(
        f"{NEOTOMA_BASE_URL}/correct",
        json={
            "entity_id": entity_id,
            "entity_type": "agent_policy",
            "field": "status",
            "value": "retired",
            "idempotency_key": f"revert-{entity_id}-{stamp}",
        },
    )
    ok = resp.status_code < 400
    print(f"{'retired' if ok else 'FAILED'} {entity_id} (HTTP {resp.status_code})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Revert auto-generated agent policies.")
    ap.add_argument("--id", help="Retire a single policy by entity ID.")
    ap.add_argument("--agent", help="Agent short name or sub (with --all).")
    ap.add_argument("--all", action="store_true", help="Retire all auto policies for --agent.")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change.")
    args = ap.parse_args()

    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN")
    if not bearer:
        print("NEOTOMA_BEARER_TOKEN not set.", file=sys.stderr)
        return 2
    if not args.id and not (args.agent and args.all):
        ap.error("provide --id, or --agent NAME --all")

    with httpx.Client(headers=_headers(bearer), timeout=20) as client:
        if args.id:
            _retire(client, args.id, args.dry_run)
            return 0

        agent_sub = args.agent if "@" in args.agent else f"{args.agent}@ateles-swarm"
        resp = client.post(
            f"{NEOTOMA_BASE_URL}/retrieve_entities",
            json={"entity_type": "agent_policy", "limit": 500, "include_snapshots": True},
        )
        resp.raise_for_status()
        targets = [
            e["entity_id"]
            for e in resp.json().get("entities", [])
            if (snap := e.get("snapshot") or {}).get("agent_sub") == agent_sub
            and snap.get("status") != "retired"
            and _is_auto(snap.get("body", ""))
        ]
        if not targets:
            print(f"No auto-generated policies to retire for {agent_sub}.")
            return 0
        print(f"{len(targets)} auto policy(ies) for {agent_sub}:")
        for eid in targets:
            _retire(client, eid, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
