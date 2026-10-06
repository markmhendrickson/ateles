#!/usr/bin/env python3
"""Re-issue signed, resolvable checkpoints for tasks still held at the gate.

Dry run by default: prints what would happen per task and writes nothing.
``--apply`` files one fresh signed checkpoint per still-held task that lacks a
resolvable one, then retires that task's unresolvable pending briefs.  It never
releases, approves, or dispatches a task, and never touches a task that is not
at ``awaiting_approval`` right now.

One JSON object per task on stdout, then a summary object.  Exit status is
non-zero when any task failed, so an operator can re-run (the operation is
idempotent) until it is zero.
"""

from __future__ import annotations

import argparse
import collections
import json

from apis import iter_held_task_ids, reissue_held_task_checkpoint


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write checkpoints (default: dry run, no writes)",
    )
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        metavar="TASK_ID",
        help="restrict to these task ids (repeatable); default: every held task",
    )
    parser.add_argument("--limit", type=int, default=None, help="stop after N tasks")
    args = parser.parse_args()

    task_ids = (
        args.task[: args.limit] if args.task else iter_held_task_ids(limit=args.limit)
    )
    counts: collections.Counter[str] = collections.Counter()
    for task_id in task_ids:
        try:
            result = reissue_held_task_checkpoint(task_id, apply=args.apply)
            record = {
                "task_id": result.task_id,
                "outcome": result.outcome,
                "detail": result.detail,
                "checkpoint_id": result.checkpoint_id,
                "superseded": result.superseded,
            }
        except Exception as exc:  # noqa: BLE001 — one bad task must not stop the run
            record = {
                "task_id": task_id,
                "outcome": "failed",
                "detail": f"{type(exc).__name__}: {exc}"[:200],
            }
        counts[record["outcome"]] += 1
        print(json.dumps(record, sort_keys=True))
    print(json.dumps({"summary": dict(counts), "applied": args.apply}, sort_keys=True))
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
