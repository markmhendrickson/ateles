#!/usr/bin/env python3
"""Show or clear one Fly delivery receipt (the operator's reconcile step).

A delivery that launched on Fly leaves a durable receipt in the Fly ledger, and
while it exists every re-dispatch for that issue or task is refused and nothing
falls back to the local path. After inspecting the remote branch and the run
record and reconciling what happened, the operator clears the receipt here.
Nothing clears a receipt automatically.

    python3 execution/daemons/apis/fly_delivery_receipt.py list
    python3 execution/daemons/apis/fly_delivery_receipt.py clear OWNER/REPO#NUMBER
    python3 execution/daemons/apis/fly_delivery_receipt.py clear OWNER/REPO#NUMBER \\
        --confirm OWNER/REPO#NUMBER

Run it with the dispatcher's environment (it reads the same ledger file).
``clear`` is a dry run: it prints the receipt (key, status, run, branch, commit,
recorded time) and what would be removed, and writes nothing. The receipt is
removed only when ``--confirm`` repeats the exact key. The ledger's own lock and
atomic write are used, a malformed ledger is refused, and a receipt that changed
since it was shown is left alone. The audit comes first: under the ledger lock,
after the receipt has been compared with what was shown and before anything is
written, a ``removing`` event (who, when, the action and the full identity of
the receipt) is appended to ``<ledger>.audit.jsonl`` and synced; if that fails
nothing is removed. The removal is then verified by reading the ledger again
(an error raised during or after the write is treated as uncertain, never as
"not removed"), and a ``removed`` event (the receipt is gone) or a
``remove_failed`` event (it is still there) is appended. No credential is read
or written.

Exit status: 0 the receipt is verified gone and the ``removed`` event is written
(or a dry run, or ``list``); 1 refused (no such receipt, wrong ``--confirm``, or
the receipt changed); 2 nothing was removed (the ledger could not be read, or
the ``removing`` event could not be written); 3 the removal failed and the
receipt is still there; 4 the receipt WAS removed (verified) but the ``removed``
event could not be written; 5 the final state could not be verified.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_delivery  # noqa: E402
import fly_runner  # noqa: E402


def _iso(epoch: object) -> str:
    if isinstance(epoch, (int, float)) and not isinstance(epoch, bool):
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "not reported"


def describe(key: str, entry: dict) -> str:
    return (
        f"key:       {key}\n"
        f"status:    {entry.get('status') or 'not reported'}\n"
        f"run id:    {entry.get('run_id') or 'not reported'}\n"
        f"branch:    {entry.get('branch') or 'not reported'}\n"
        f"commit:    {entry.get('commit') or 'not reported'}\n"
        f"recorded:  {_iso(entry.get('recorded_at'))}"
    )


def audit_path(ledger: "fly_runner.FlyLedger") -> Path:
    return ledger.path.with_name(ledger.path.name + ".audit.jsonl")


def _append_audit(ledger: "fly_runner.FlyLedger", line: dict) -> None:
    """One JSON line, appended and synced. Raises OSError when it cannot be
    written, and the caller then removes nothing."""
    path = audit_path(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _find(receipts: dict, key: str) -> tuple[str, dict] | None:
    """The stored (key, receipt) for the canonical *key*, whatever spelling
    the ledger holds it under."""
    for stored, entry in receipts.items():
        if stored == key or build_delivery.canonical_key(stored) == key:
            return stored, entry
    return None


def main(argv: list[str] | None = None, *, ledger=None, out=None, err=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="print every delivery receipt")
    clear = sub.add_parser("clear", help="dry run; with --confirm, remove one receipt")
    clear.add_argument("key", help="the issue key, OWNER/REPO#NUMBER (or OWNER/REPO#task:ID)")
    clear.add_argument("--confirm", metavar="KEY", help="the same key, to actually remove it")
    args = parser.parse_args(argv)

    ledger = ledger or build_delivery._delivery_ledger()
    try:
        receipts = ledger.delivery_receipts()
    except (OSError, fly_runner.FlyLedgerError) as exc:
        print(f"refused: the Fly ledger could not be read ({exc}); nothing changed", file=err)
        return 2

    if args.command == "list":
        for stored in sorted(receipts):
            print(describe(stored, receipts[stored]) + "\n", file=out)
        print(f"{len(receipts)} delivery receipt(s)", file=out)
        return 0

    key = build_delivery.canonical_key(args.key)
    found = _find(receipts, key)
    if found is None:
        print(f"refused: no delivery receipt for {key}; nothing changed", file=err)
        return 1
    stored, entry = found
    print(describe(stored, entry), file=out)
    if args.confirm is None:
        print(f"\nDry run: would remove the receipt above. Nothing changed. To remove it, "
              f"repeat with --confirm {key}", file=out)
        return 0
    if build_delivery.canonical_key(args.confirm) != key:
        print("refused: --confirm must repeat the exact key; nothing changed", file=err)
        return 1
    when = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    identity = {
        "by": getpass.getuser(),
        "action": "clear_delivery_receipt",
        "key": stored,
        "status": entry.get("status"),
        "run_id": entry.get("run_id"),
        "branch": entry.get("branch"),
        "commit": entry.get("commit"),
        "recorded_at": entry.get("recorded_at"),
    }
    audit_file = audit_path(ledger)
    announced, attempted = [], []

    def announce() -> None:
        # Under the ledger lock, after the compare, before any write.
        attempted.append(True)
        _append_audit(ledger, {**identity, "stage": "removing", "at": when()})
        announced.append(True)

    problem = ""
    try:
        removed = ledger.clear_delivery(stored, entry, before=announce)
    except (OSError, fly_runner.FlyLedgerError) as exc:
        removed, problem = None, f"{type(exc).__name__}: {exc}"
    if removed is False:
        print("refused: the receipt changed since it was shown; nothing removed. "
              "Run this again.", file=err)
        return 1
    if not announced:
        why = "the audit record could not be written" if attempted else "the ledger refused"
        print(f"refused: {why} ({problem}); nothing was removed", file=err)
        return 2
    # An error during or after the write is not an answer: read the ledger again.
    try:
        gone = stored not in ledger.delivery_receipts()
    except (OSError, fly_runner.FlyLedgerError) as exc:
        print(f"UNVERIFIED: the ledger could not be read back ({exc}). The 'removing' record "
              f"is in {audit_file}. Check the ledger before doing anything else.", file=err)
        return 5
    detail = {"error": problem} if problem else {}
    try:
        _append_audit(ledger, {**identity, **detail, "at": when(),
                               "stage": "removed" if gone else "remove_failed"})  # fmt: skip
    except OSError as exc:
        if gone:
            print(f"The receipt for {stored} WAS removed (verified by reading the ledger "
                  f"again), but the closing audit record could not be written ({exc}). The "
                  f"'removing' record is in {audit_file}. Re-dispatch is no longer refused "
                  "on its account.", file=err)
            return 4
        print(f"The receipt for {stored} is still in the ledger ({problem or 'not removed'}) "
              f"and the 'remove_failed' record could not be written ({exc}); the 'removing' "
              f"record is in {audit_file}.", file=err)
        return 3
    if not gone:
        print(f"The receipt for {stored} is still in the ledger ({problem or 'not removed'}); "
              "'remove_failed' is recorded in the audit file.", file=err)
        return 3
    note = (f" (the ledger write reported an error, {problem}; the removal was verified by "
            "reading the ledger again)" if problem else "")
    print(f"\nRemoved the delivery receipt for {stored}{note}. Re-dispatch is no longer "
          "refused on its account.", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
