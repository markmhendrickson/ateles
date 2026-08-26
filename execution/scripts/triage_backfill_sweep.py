#!/usr/bin/env python3
"""Dispatch Lanius triage for issue entities that exist but were never triaged.

## The gap this closes

Triage fires on a GitHub ``issue.opened`` webhook and nowhere else — one-shot,
no sweep, no retry. An ``issue`` entity written through ``/store`` (CLI, MCP,
``sync_issues``, a swarm agent) therefore exists immediately but never receives
a ``gate_status``. It is not visible as broken: the entity is present and fully
populated, so every existence check passes while the gate pipeline has no
record of it. No owner, no pending gate, nothing to advance, and no error.

A 2026-08-26 audit found 176 such issues (174 neotoma, 2 ateles), median age 21
and 54 days respectively.

Re-storing does NOT fix them. The issue canonical key is composite
``[github_number, repo]``, so a re-store coalesces onto the existing entity —
and even a fresh ``created`` event would not help, because the SSE subscribers
route ``issue`` events to Cicada, not Lanius.

## Why correct(), never store()

``gate_status`` is a ``last_write`` field. Writing it with ``store()`` clobbers
concurrent sign-offs — the documented neotoma#2033 failure. This sweep never
writes gate state itself; it dispatches triage through ``ensure_issue_entity``,
which owns the correct write path and re-reads to verify.

Dry-run by default: it reports what it would dispatch and changes nothing.

    python3 execution/scripts/triage_backfill_sweep.py --repo markmhendrickson/ateles
    python3 execution/scripts/triage_backfill_sweep.py --repo ... --apply --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "daemons" / "apis"))

import httpx  # noqa: E402

DEFAULT_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)


def _token() -> str:
    for var in ("NEOTOMA_BEARER_TOKEN_PROD", "NEOTOMA_BEARER_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    return ""


def find_untriaged(base_url: str, token: str, repo: str) -> list[int]:
    """Return issue numbers whose entity exists but carries no gate_status.

    Pages the full ``issue`` corpus. Only entities resolving to the composite
    ``issue:<number>|<repo>`` canonical form are considered — a title-keyed or
    ``local_issue_id``-keyed entity has no reliable number to dispatch on and is
    a separate defect (see the canonical-name migration plan).
    """
    import re

    seen: dict[int, bool] = {}
    cursor = ""
    with httpx.Client(timeout=90) as client:
        while True:
            url = f"{base_url}/entities?entity_type=issue&limit=200"
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            data = resp.json()
            entities = data.get("entities", [])
            for entity in entities:
                match = re.match(
                    r"^issue:(\d+)\|(.+)$", entity.get("canonical_name") or ""
                )
                if not match or match.group(2) != repo:
                    continue
                number = int(match.group(1))
                snap = entity.get("snapshot") or {}
                inner = snap.get("snapshot")
                if isinstance(inner, dict):
                    snap = inner
                has_gates = bool(snap.get("gate_status"))
                # Several entities can share a number; the issue counts as
                # triaged if ANY of them carries gate state.
                seen[number] = seen.get(number, False) or has_gates
            cursor = data.get("next_cursor") or ""
            if not cursor or not entities:
                break
    return sorted(n for n, triaged in seen.items() if not triaged)


async def dispatch(repo: str, numbers: list[int], apply: bool) -> int:
    if not apply:
        for number in numbers:
            print(f"  DRY-RUN would dispatch triage: {repo}#{number}")
        return 0

    import swarm_dispatch as sd

    dispatcher = sd.SwarmDispatcher(sd.DispatcherConfig.from_env())
    ok = 0
    for number in numbers:
        try:
            if await dispatcher.ensure_issue_entity(repo, number):
                print(f"  triaged: {repo}#{number}")
                ok += 1
            else:
                print(f"  FAILED:  {repo}#{number}")
        except Exception as exc:  # noqa: BLE001 - report, continue the sweep
            print(f"  ERROR:   {repo}#{number}: {exc}")
        # Each dispatch is an LLM turn; pace them so a large sweep does not
        # stampede the runner.
        await asyncio.sleep(2)
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill Lanius triage for un-triaged issue entities"
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually dispatch triage (default: dry-run)",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="cap dispatches (0 = no cap)"
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()

    token = _token()
    if not token:
        parser.error("NEOTOMA_BEARER_TOKEN_PROD (or _TOKEN) must be set")

    numbers = find_untriaged(args.base_url, token, args.repo)
    if args.limit:
        numbers = numbers[: args.limit]
    print(f"{args.repo}: {len(numbers)} issue entities with no gate_status")
    if not numbers:
        return
    done = asyncio.run(dispatch(args.repo, numbers, args.apply))
    if args.apply:
        print(f"triaged {done}/{len(numbers)}")
    else:
        print("dry-run — pass --apply to dispatch")


if __name__ == "__main__":
    main()
