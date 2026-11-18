#!/usr/bin/env python3
"""Inspect or run one bounded recent gate-hold checkpoint recovery page."""

from __future__ import annotations

import argparse
import json

from apis import sweep_missing_gate_checkpoints


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    candidates: list[dict[str, str]] = []
    counts, next_offset = sweep_missing_gate_checkpoints(
        apply=not args.dry_run, candidates=candidates
    )
    for candidate in candidates:
        print(json.dumps(candidate, sort_keys=True))
    print(json.dumps({"counts": counts, "next_offset": next_offset}, sort_keys=True))
    return 1 if counts["unreconstructable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
