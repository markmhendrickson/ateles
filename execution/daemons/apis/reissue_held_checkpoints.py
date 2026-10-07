#!/usr/bin/env python3
"""Re-issue signed, resolvable checkpoints for tasks still held at the gate.

Dry run by default: prints what would happen per task and writes nothing.
``--apply`` files one fresh signed checkpoint per still-held task that lacks a
current one (one that release would still accept for the task and policy as they
are now), then retires that task's obsolete pending briefs.  Re-run it after
repairing the policy: the checkpoints it filed under the old policy are replaced.  It never
releases, approves, or dispatches a task, and never touches a task that is not
at ``awaiting_approval`` right now.

One JSON object per task on stdout, then a summary object.  Exit status is
non-zero when any task ``failed`` (a read or the replacement write did not
succeed) or is ``incomplete`` (a stale brief could not be retired), so an
operator can re-run (the operation is idempotent) until it is zero.
"""

from __future__ import annotations

import argparse
import collections
import itertools
import json

from apis import iter_held_task_ids, reissue_held_task_checkpoint


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer (1 or more)")
    return number


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
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        metavar="N",
        help=(
            "examine at most N tasks, whether they are named with --task or "
            "enumerated; must be a positive integer"
        ),
    )
    args = parser.parse_args()

    # One meaning for --limit in both modes: the first N tasks, in order.
    task_ids = itertools.islice(
        args.task if args.task else iter_held_task_ids(), args.limit
    )
    counts: collections.Counter[str] = collections.Counter()
    enumeration_error: str | None = None
    iterator = iter(task_ids)
    while True:
        try:
            task_id = next(iterator)
        except StopIteration:
            break
        except RuntimeError as exc:
            # The held-task list itself could not be read: report it as a
            # failure with its own record, never as an empty or complete run.
            enumeration_error = str(exc)
            counts["failed"] += 1
            print(
                json.dumps(
                    {
                        "outcome": "failed",
                        "detail": f"enumeration: {enumeration_error}"[:200],
                    },
                    sort_keys=True,
                )
            )
            break
        try:
            result = reissue_held_task_checkpoint(task_id, apply=args.apply)
            record = {
                "task_id": result.task_id,
                "outcome": result.outcome,
                "detail": result.detail,
                "checkpoint_id": result.checkpoint_id,
                "superseded": result.superseded,
                "remaining": result.remaining,
            }
        except Exception as exc:  # noqa: BLE001 - one bad task must not stop the run
            record = {
                "task_id": task_id,
                "outcome": "failed",
                "detail": f"{type(exc).__name__}: {exc}"[:200],
            }
        counts[record["outcome"]] += 1
        print(json.dumps(record, sort_keys=True))
    print(
        json.dumps(
            {
                "summary": dict(counts),
                "examined": sum(counts.values()),
                "applied": args.apply,
            },
            sort_keys=True,
        )
    )
    # A read or write failure, or a partial retirement, means recovery is not
    # complete: exit non-zero so "rerun until zero" is a reliable contract.
    return 1 if counts.get("failed") or counts.get("incomplete") else 0


if __name__ == "__main__":
    raise SystemExit(main())
