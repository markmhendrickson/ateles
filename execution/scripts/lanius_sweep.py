#!/usr/bin/env python3
"""
lanius_sweep.py — Lanius stale Neotoma issue sweeper.

Lanius genus: shrikes. GHA-first Phase 3 implementation.

Finds open Neotoma issues (audience=agent) that have not been updated in
LANIUS_STALE_DAYS days and marks them status=stale via Neotoma corrections.

Promote to a T3 daemon (Lanius) if Neotoma attribution or event-driven
triggering becomes important.

Usage:
  python lanius_sweep.py [--dry-run] [--stale-days N]

Environment variables:
  NEOTOMA_BEARER_TOKEN    Neotoma API bearer token (required)
  NEOTOMA_BASE_URL        Neotoma API base URL (default: https://neotoma.markmhendrickson.com)
  LANIUS_DRY_RUN          Set to "true" or "1" to print without modifying (default: false)
  LANIUS_STALE_DAYS       Days without update before stale (default: 30)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime, timedelta

import httpx

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("lanius")

# ── Config ────────────────────────────────────────────────────────────────────
NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()

DRY_RUN_ENV = os.environ.get("LANIUS_DRY_RUN", "false").lower() in ("true", "1", "yes")
STALE_DAYS_ENV = int(os.environ.get("LANIUS_STALE_DAYS", "30"))

# Issue audiences to sweep (system/agent issues only — not human maintainer issues)
SWEEP_AUDIENCES = {"agent", "both"}

# Statuses that are considered open / actionable
OPEN_STATUSES = {"open", "in_progress", "pending"}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
        "Content-Type": "application/json",
    }


def fetch_open_issues(client: httpx.Client, limit: int = 500) -> list[dict]:
    """Retrieve open issue entities from Neotoma."""
    params = {
        "entity_type": "issue",
        "limit": str(limit),
        "include_snapshots": "true",
    }
    resp = client.get(f"{NEOTOMA_BASE_URL}/entities", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("entities", [])


def is_stale(entity: dict, stale_cutoff: datetime) -> bool:
    """Return True if the issue is open, agent-audience, and not updated since cutoff."""
    snap = entity.get("snapshot") or {}

    # Skip non-open statuses
    status = snap.get("status", "")
    if status not in OPEN_STATUSES:
        return False

    # Skip human-only issues
    audience = snap.get("audience", "agent")
    if audience not in SWEEP_AUDIENCES:
        return False

    # Check last update time
    updated_at_raw = entity.get("updated_at") or entity.get("last_observation_at") or ""
    if not updated_at_raw:
        return False

    try:
        # Parse ISO 8601 — may have trailing Z or +00:00
        updated_at_raw = updated_at_raw.replace("Z", "+00:00")
        updated_at = datetime.fromisoformat(updated_at_raw)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
    except ValueError:
        log.warning(
            f"[lanius] Could not parse updated_at={updated_at_raw!r} for {entity.get('entity_id')}"
        )
        return False

    return updated_at < stale_cutoff


def mark_stale(client: httpx.Client, entity_id: str, dry_run: bool) -> bool:
    """
    Mark issue as stale via Neotoma correction.

    Uses POST /corrections (field-level correction pattern).
    Returns True on success.
    """
    if dry_run:
        log.info(f"[lanius] [DRY RUN] Would mark stale: {entity_id}")
        return True

    payload = {
        "entity_id": entity_id,
        "field_name": "status",
        "corrected_value": "stale",
        "correction_note": "Lanius: no update in over 30 days — marking stale",
    }
    try:
        resp = client.post(f"{NEOTOMA_BASE_URL}/corrections", json=payload, timeout=15)
        resp.raise_for_status()
        log.info(f"[lanius] Marked stale: {entity_id}")
        return True
    except httpx.HTTPStatusError as exc:
        log.error(
            f"[lanius] Failed to mark {entity_id} stale: HTTP {exc.response.status_code}"
        )
        return False
    except Exception as exc:
        log.error(f"[lanius] Failed to mark {entity_id} stale: {exc}")
        return False


def run(dry_run: bool, stale_days: int) -> None:
    """Main sweep logic."""
    if not NEOTOMA_BEARER_TOKEN:
        log.error("[lanius] NEOTOMA_BEARER_TOKEN not set — cannot run sweep")
        sys.exit(1)

    stale_cutoff = datetime.now(tz=UTC) - timedelta(days=stale_days)
    log.info(
        f"[lanius] Sweep config: dry_run={dry_run} stale_days={stale_days} "
        f"cutoff={stale_cutoff.date().isoformat()}"
    )

    with httpx.Client(headers=_headers()) as client:
        log.info("[lanius] Fetching open issues from Neotoma...")
        try:
            issues = fetch_open_issues(client)
        except httpx.HTTPStatusError as exc:
            log.error(
                f"[lanius] Failed to fetch issues: HTTP {exc.response.status_code}"
            )
            sys.exit(0)  # non-fatal: sweeper should not fail CI
        except Exception as exc:
            log.error(f"[lanius] Failed to fetch issues: {exc}")
            sys.exit(0)

        log.info(f"[lanius] Fetched {len(issues)} issue entities")

        stale = [e for e in issues if is_stale(e, stale_cutoff)]
        log.info(f"[lanius] Found {len(stale)} stale issues (of {len(issues)} total)")

        marked = 0
        for entity in stale:
            eid = entity.get("entity_id", "")
            snap = entity.get("snapshot") or {}
            title = snap.get("title", "(untitled)")[:80]
            audience = snap.get("audience", "?")
            log.info(f"[lanius] Stale: {eid} audience={audience} title={title!r}")
            if mark_stale(client, eid, dry_run):
                marked += 1

    mode = "[DRY RUN] " if dry_run else ""
    log.info(
        f"[lanius] {mode}Sweep complete: {marked}/{len(stale)} issues marked stale "
        f"(out of {len(issues)} total fetched)"
    )
    print(
        f"lanius: {mode}{marked} of {len(stale)} stale issues processed ({len(issues)} total)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Lanius — Neotoma stale issue sweeper")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print without modifying"
    )
    parser.add_argument(
        "--stale-days", type=int, default=None, help="Staleness threshold (days)"
    )
    args = parser.parse_args()

    dry_run = args.dry_run or DRY_RUN_ENV
    stale_days = args.stale_days if args.stale_days is not None else STALE_DAYS_ENV

    run(dry_run=dry_run, stale_days=stale_days)


if __name__ == "__main__":
    main()
