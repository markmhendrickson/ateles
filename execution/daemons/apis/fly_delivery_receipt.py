#!/usr/bin/env python3
"""Show or clear one Fly delivery receipt (the operator's reconcile step).

A delivery that launched on Fly leaves a durable receipt in the Fly ledger, and
while it exists every re-dispatch for that issue or task is refused and nothing
falls back to the local path. After inspecting the remote branch and the run
record and reconciling what happened, the operator clears the receipt here.
Nothing clears a receipt automatically.

The same ledger holds lens-review entries (``lens_receipts``), keyed
``lens:OWNER/REPO#PR@HEAD:LENS``, each recording one lens run on one pull
request head. They are listed and cleared here the same way; their
description adds the lens, pull request, head and when the notice was claimed.

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
"not removed"), and a ``removed`` event (clean), a ``removed_unconfirmed``
event (gone, but the removal raised) or a ``remove_failed`` event (still there)
is appended. The audit file is synced and so is its directory. No credential is read
or written.

Exit status:
  0  clean and verified: the removal returned without an error, a fresh read shows
     the receipt gone, and the ``removed`` event is written and synced (also a
     dry run and ``list``)
  1  refused: no such receipt, wrong ``--confirm``, or the receipt changed
  2  nothing was removed: the ledger could not be read, or the ``removing`` event
     (or its directory) could not be written and synced
  3  the removal failed and the receipt is still there
  4  the receipt WAS removed (verified, and the removal raised no error) but the
     ``removed`` event could not be written; the message names the audit file
  5  the final state could not be verified (the ledger could not be read back)
  6  the receipt is no longer visible, but the removal raised an error (for
     example a directory sync after the replace), so it is NOT confirmed
     durable: a crash may bring it back. ``removed_unconfirmed`` is recorded.
     Run ``list`` again, and run the clear again if it reappears.
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
    text = (
        f"key:       {key}\n"
        f"status:    {entry.get('status') or 'not reported'}\n"
        f"run id:    {entry.get('run_id') or 'not reported'}\n"
        f"branch:    {entry.get('branch') or 'not reported'}\n"
        f"commit:    {entry.get('commit') or 'not reported'}\n"
        f"recorded:  {_iso(entry.get('recorded_at'))}"
    )
    if entry.get("kind") == "lens":
        text += (
            f"\nlens:      {entry.get('lens') or 'not reported'}\n"
            f"pull:      {entry.get('pr') or 'not reported'}\n"
            f"head:      {entry.get('head') or 'not reported'}\n"
            f"notified:  {_iso(entry.get('notified_at'))}"
        )
    return text


def audit_path(ledger: "fly_runner.FlyLedger") -> Path:
    return ledger.path.with_name(ledger.path.name + ".audit.jsonl")


def _fsync_dir(directory: Path) -> None:
    """Make a created or replaced name in *directory* durable."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_audit(ledger: "fly_runner.FlyLedger", line: dict) -> None:
    """One JSON line, appended and synced, and then its directory synced so the
    file's name is durable too (it may have just been created). Raises OSError
    when any of that fails, and the caller then removes nothing."""
    path = audit_path(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    _fsync_dir(path.parent)


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
    clear.add_argument("key", help="the issue key, OWNER/REPO#NUMBER (or OWNER/REPO#task:ID, "
                       "or lens:OWNER/REPO#PR@HEAD:LENS)")
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
    if entry.get("kind") == "lens":
        identity.update({"kind": "lens", "lens": entry.get("lens"), "pr": entry.get("pr"),
                         "head": entry.get("head"), "notified_at": entry.get("notified_at")})
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
        removed, problem = None, type(exc).__name__
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
        print(f"UNVERIFIED: the ledger could not be read back ({type(exc).__name__}). The "
              f"'removing' record "
              f"is in {audit_file}. Check the ledger before doing anything else.", file=err)
        return 5
    # Success needs all three: the removal returned without an error (its own
    # directory sync included), the fresh read shows the receipt gone, and the
    # closing record is written and synced. A fresh read shows visibility, not
    # durability, so a removal that raised is never reported as complete.
    stage = ("remove_failed" if not gone
             else "removed_unconfirmed" if problem else "removed")  # fmt: skip
    detail = {"error": problem} if problem else {}
    try:
        _append_audit(ledger, {**identity, **detail, "at": when(), "stage": stage})
    except OSError as exc:
        if not gone:
            print(f"The receipt for {stored} is still in the ledger ({problem or 'not removed'})"
                  f" and the 'remove_failed' record could not be written "
                  f"({type(exc).__name__}); the 'removing' record is in {audit_file}.", file=err)
            return 3
        if problem:
            print(f"The receipt for {stored} is no longer visible, but its removal is NOT "
                  f"confirmed durable ({problem}) and the closing record could not be written "
                  f"({type(exc).__name__}). A crash may bring it back. Run 'list' again, and "
                  f"run the clear again if it reappears. The 'removing' record is in "
                  f"{audit_file}.", file=err)
            return 6
        print(f"The receipt for {stored} WAS removed (verified by reading the ledger again), "
              f"but the closing audit record could not be written ({type(exc).__name__}). The "
              f"'removing' record is in {audit_file}. Re-dispatch is no longer refused on its "
              "account.", file=err)
        return 4
    if not gone:
        print(f"The receipt for {stored} is still in the ledger ({problem or 'not removed'}); "
              "'remove_failed' is recorded in the audit file.", file=err)
        return 3
    if problem:
        print(f"The receipt for {stored} is no longer visible, but its removal is NOT "
              f"confirmed durable (the ledger write raised {problem}). A crash may bring it "
              "back. Run 'list' again, and run the clear again if it reappears. "
              "'removed_unconfirmed' is recorded in the audit file.", file=err)
        return 6
    print(f"\nRemoved the delivery receipt for {stored}. Re-dispatch is no longer refused "
          "on its account.", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
