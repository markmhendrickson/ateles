#!/usr/bin/env python3
"""Read-only census of Wise journal holds; pass an explicit journal path.

This reader never invokes ``payment_journal.load`` because that can create the
first-use marker. It prints counts and handler names, never payment details.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import payment_journal


def classify(row: object, data: dict) -> str:
    kind, _ = payment_journal.classify_journal_row(row, data)
    if isinstance(row, dict) and row.get("rail") == "btc" and kind in {
        "btc_valid_settled", "btc_valid_unresolved"
    }:
        return "btc"
    if kind == "legacy_settled":
        return "legacy_settled_sent"
    if kind == "wise_modern_resolved":
        return "wise_new_resolved_paid"
    if isinstance(row, dict) and row.get("rail") == "wise" and isinstance(row.get("profile_entity_id"), str) and row["profile_entity_id"].strip():
        return "wise_new_UNRESOLVED_with_profile_id"
    return "unclassified_UNRESOLVED_all_profiles"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    try:
        data = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("the daemon would REFUSE to load this journal: unreadable or invalid JSON")
        print("all payments would hold")
        return 1
    problems = payment_journal.validate_journal_data(data)
    if problems:
        print("the daemon would REFUSE to load this journal: " + ", ".join(problems))
        print("all payments would hold")
        return 1
    rows = data.get("obligations") or {}
    kinds = Counter()
    held_profiles = Counter()
    hold_all = 0
    for row in rows.values():
        kind = classify(row, data)
        kinds[kind] += 1
        if kind == "wise_new_UNRESOLVED_with_profile_id":
            held_profiles[str(row.get("handler_name") or "(unnamed)")] += 1
        elif kind.endswith("UNRESOLVED_all_profiles") or kind == "unreadable_UNRESOLVED":
            hold_all += 1
    print(f"rows in the journal: {len(rows)}")
    for kind, n in sorted(kinds.items()):
        print(f"  {kind}: {n}")
    print(f"rows that would hold ALL Wise obligations: {hold_all}")
    print("payees held individually (handler name: unresolved rows):")
    if not held_profiles:
        print("  none")
    for name, n in sorted(held_profiles.items()):
        print(f"  {name}: {n}")
    print("RESULT:", "no new Wise obligation would be blocked" if not hold_all and not held_profiles
          else "some Wise obligations would be blocked, see above")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
