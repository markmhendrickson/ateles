#!/usr/bin/env python3
"""
Deduplicate financial_account entities by registry_id using Neotoma merge.

Finds all financial_account entities, groups by registry_id, and merges
duplicates (keeping the highest-quality entity as the merge target).

Usage:
  python3 execution/scripts/finances/dedup_financial_accounts_by_registry.py [--dry-run] [--api-only]

Flags:
  --dry-run     Print merge plan without executing
  --api-only    Pass --api-only to neotoma CLI
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from typing import Any


def snap_field(entity: dict[str, Any], key: str) -> Any:
    snap = entity.get("snapshot") or {}
    return snap.get(key)


def quality_score(entity: dict[str, Any]) -> int:
    score = 0
    inst = str(snap_field(entity, "institution") or "").strip()
    if inst and not re.match(r"^[\s\u2014\u2013-]+$", inst):
        score += 5
    if snap_field(entity, "account_name"):
        score += 3
    cn = str(
        snap_field(entity, "canonical_name") or entity.get("canonical_name") or ""
    ).strip()
    if cn and not re.match(r"^[\s\u2014\u2013-]+", cn):
        score += 2
    obs_count = entity.get("observation_count", 0)
    if isinstance(obs_count, int):
        score += min(obs_count, 10)
    return score


def fetch_all_accounts(api_only: bool) -> list[dict[str, Any]]:
    cmd = [
        "neotoma",
        "entities",
        "list",
        "--type",
        "financial_account",
        "--limit",
        "500",
    ]
    if api_only:
        cmd.append("--api-only")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error fetching entities: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(result.stdout or "{}")
    return data.get("entities", data.get("data", []))


def merge_entities(
    from_id: str, to_id: str, reason: str, api_only: bool, dry_run: bool
) -> bool:
    if dry_run:
        print(f"  MERGE {from_id} -> {to_id} ({reason})")
        return True
    body = json.dumps(
        {
            "from_entity_id": from_id,
            "to_entity_id": to_id,
            "merge_reason": reason,
        }
    )
    cmd = [
        "neotoma",
        "request",
        "--operation",
        "mergeEntities",
        "--body",
        body,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  MERGE FAILED {from_id} -> {to_id}: {result.stderr}", file=sys.stderr)
        return False
    print(f"  MERGED {from_id} -> {to_id}")
    return True


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    api_only = "--api-only" in sys.argv

    print("Fetching all financial_account entities...", file=sys.stderr)
    accounts = fetch_all_accounts(api_only)
    print(f"Found {len(accounts)} entities", file=sys.stderr)

    by_registry: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entity in accounts:
        rid = snap_field(entity, "registry_id")
        if rid:
            by_registry[str(rid)].append(entity)

    duplicates = {k: v for k, v in by_registry.items() if len(v) > 1}
    if not duplicates:
        print("No duplicates found", file=sys.stderr)
        return 0

    print(f"Found {len(duplicates)} registry_ids with duplicates:", file=sys.stderr)
    total_merges = 0
    for rid, entities in sorted(duplicates.items()):
        print(f"\n{rid}: {len(entities)} entities")
        ranked = sorted(entities, key=quality_score, reverse=True)
        target = ranked[0]
        target_id = target.get("entity_id")
        target_name = target.get("canonical_name") or "?"
        print(
            f"  Target (highest quality): {target_id} ({target_name}, score={quality_score(target)})"
        )

        for source in ranked[1:]:
            source_id = source.get("entity_id")
            source_name = source.get("canonical_name") or "?"
            reason = f"Dedup by registry_id={rid}: merge lower-quality entity (score={quality_score(source)}) into target (score={quality_score(target)})"
            print(
                f"  Source: {source_id} ({source_name}, score={quality_score(source)})"
            )
            if merge_entities(source_id, target_id, reason, api_only, dry_run):
                total_merges += 1

    suffix = " (dry run)" if dry_run else ""
    print(f"\nTotal merges: {total_merges}{suffix}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
