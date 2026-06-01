#!/usr/bin/env python3
"""
Soft-delete obsolete ``transcription`` entities from the basename-collapsed migration.

Deletes rows whose **snapshot title** matches the old pattern ``Migrated transcription — {filename}``
(em dash immediately after ``transcription``), which Neotoma merged across duplicate
``audio_file_name`` values. Repair imports use ``Migrated transcription {legacy_id} — {name}`` and are kept.

**Do not** use ``canonical_name`` alone for this decision: it can remain basename-only after repair
observations updated the snapshot, which previously caused false-positive deletes.

Also removes the stray CLI test entity ``combined test`` when present.

Idempotent: re-running skips entities that are already deleted.

Run from repo root::

    execution/venv/bin/python execution/scripts/cleanup_obsolete_transcription_basenames.py --dry-run
    execution/venv/bin/python execution/scripts/cleanup_obsolete_transcription_basenames.py
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OLD_BASELINE_TITLE = re.compile(r"^Migrated transcription — ")
# Repair / re-import titles always have at least one character between ``transcription`` and `` — ``.
REPAIR_TITLE = re.compile(r"^Migrated transcription .+ — ")

DEFAULT_REASON = (
    "obsolete basename-collapsed parquet import; superseded by "
    "repair_transcription_merge_duplicates.py"
)


def _neotoma_json(args: list[str]) -> dict:
    cmd = ["neotoma", "--json", "--servers=use-existing", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        raise RuntimeError(proc.stderr or out or f"exit {proc.returncode}")
    return json.loads(out)


def _list_transcriptions(page_size: int = 2000) -> list[dict]:
    out = _neotoma_json(
        ["entities", "list", "--type", "transcription", "--limit", str(page_size)]
    )
    entities = out.get("entities") or []
    total = int(out.get("total") or len(entities))
    if total > len(entities):
        raise RuntimeError(
            f"Pagination required: total={total} returned={len(entities)}; raise page_size or add offset loop."
        )
    return entities


def _should_delete(entity: dict) -> tuple[bool, str]:
    snap = entity.get("snapshot") or {}
    title = (snap.get("title") or "").strip()
    cn = (entity.get("canonical_name") or "").strip()

    if cn == "combined test" or title == "combined test":
        return True, "combined test CLI artifact"

    # Never drop repair / disambiguated re-import rows: snapshot title carries legacy id.
    if REPAIR_TITLE.match(title):
        return False, ""

    # Baseline-collapse imports used ``Migrated transcription — {file}`` with nothing
    # between ``transcription`` and the em dash.  **Do not** key off ``canonical_name``
    # alone: Neotoma can retain the old basename-only canonical after newer observations
    # (repair imports) updated the snapshot title, which caused false-positive deletes.
    if OLD_BASELINE_TITLE.match(title):
        return True, "old basename-only migrated title (snapshot)"

    return False, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Delete at most N candidates (after discovery, in list order)",
    )
    parser.add_argument("--reason", type=str, default=DEFAULT_REASON)
    args = parser.parse_args()

    entities = _list_transcriptions()
    candidates: list[tuple[str, str]] = []
    for e in entities:
        eid = e.get("entity_id")
        if not eid:
            continue
        ok, why = _should_delete(e)
        if ok:
            label = (
                (e.get("snapshot") or {}).get("title") or e.get("canonical_name") or eid
            )
            candidates.append((eid, f"{why}: {label[:100]}"))

    if args.limit is not None:
        candidates = candidates[: args.limit]

    print(f"Found {len(candidates)} transcription(s) to soft-delete.")

    deleted = 0
    failures: list[tuple[str, str]] = []
    for eid, desc in candidates:
        print(f"  {eid}  ({desc})")
        if args.dry_run:
            continue
        try:
            _neotoma_json(
                [
                    "entities",
                    "delete",
                    eid,
                    "transcription",
                    "--reason",
                    args.reason,
                ]
            )
            deleted += 1
        except RuntimeError as exc:
            failures.append((eid, str(exc)))

    if args.dry_run:
        print("Dry run only; no deletes performed.")
        return 0

    print(f"Soft-deleted {deleted} transcription(s).")
    if failures:
        print(
            f"Failed {len(failures)} delete(s) (entity missing, transport, etc.):",
            file=sys.stderr,
        )
        for eid, err in failures[:50]:
            print(f"  {eid}: {err}", file=sys.stderr)
        if len(failures) > 50:
            print(f"  ... and {len(failures) - 50} more", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
