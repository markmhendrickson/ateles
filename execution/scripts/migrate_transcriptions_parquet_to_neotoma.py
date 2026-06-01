#!/usr/bin/env python3
"""
One-off migration: legacy ``transcriptions.parquet`` rows → Neotoma ``transcription`` entities.

Uses the same ``save_transcription`` path as ``transcribe_audio.py`` (``neotoma store``),
with ``observation_source=import`` and idempotency keys tied to parquet ``transcription_id``.

Run from repo root::

    execution/venv/bin/python execution/scripts/migrate_transcriptions_parquet_to_neotoma.py
    execution/venv/bin/python execution/scripts/migrate_transcriptions_parquet_to_neotoma.py --dry-run --limit 5
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "execution" / "scripts"))

try:
    from config import get_data_dir
except ImportError:
    from scripts.config import get_data_dir  # type: ignore

from transcribe_audio import (
    _json_safe_float,
    _json_safe_nonnegative_int,
    save_transcription,
)


def _fmt_date(val) -> str | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if hasattr(val, "date"):
        return val.date().isoformat()
    s = str(val).strip()
    return s[:10] if s else None


def _audio_path_from_row(data_dir: Path, rel_or_abs: str) -> Path:
    p = Path(str(rel_or_abs).strip())
    if p.is_absolute():
        return p
    return (data_dir / p).resolve()


def _row_to_transcription_result(row: pd.Series) -> dict:
    return {
        "transcription_text": str(row.get("transcription_text") or ""),
        "language": str(row.get("language") or "auto"),
        "audio_duration_seconds": _json_safe_float(row.get("audio_duration_seconds")),
        "file_size_bytes": _json_safe_nonnegative_int(row.get("file_size_bytes")),
    }


def _save_with_retries(**kwargs) -> None:
    """Call ``save_transcription`` with exponential backoff on transient Neotoma CLI errors."""
    transient_markers = (
        "Local transport API failed",
        "failed to start",
        "ECONNRESET",
        "ETIMEDOUT",
        "temporarily unavailable",
        "database is locked",
    )
    max_attempts = 8
    for attempt in range(max_attempts):
        try:
            save_transcription(**kwargs)
            return
        except RuntimeError as e:
            msg = str(e)
            if not any(m in msg for m in transient_markers):
                raise
            if attempt + 1 >= max_attempts:
                raise
            delay = min(32.0, 2.0**attempt)
            print(
                f"  retry in {delay:.0f}s ({attempt + 1}/{max_attempts}): {msg[:100]}...",
                flush=True,
            )
            time.sleep(delay)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parquet-path",
        type=Path,
        default=None,
        help="Path to transcriptions.parquet (default: $DATA_DIR/transcriptions/transcriptions.parquet)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions only; do not call Neotoma",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N rows (after optional --skip)",
    )
    parser.add_argument(
        "--skip",
        type=int,
        default=0,
        help="Skip the first N rows of the parquet file",
    )
    args = parser.parse_args()

    data_dir = Path(get_data_dir())
    pq = args.parquet_path or (data_dir / "transcriptions" / "transcriptions.parquet")
    if not pq.exists():
        print(f"No parquet at {pq}; nothing to migrate.")
        return 0

    df = pd.read_parquet(pq)
    total = len(df)
    print(f"Loaded {total} row(s) from {pq}")

    end = None if args.limit is None else args.skip + args.limit
    chunk = df.iloc[args.skip : end]

    done = 0
    failed = 0
    for idx, row in chunk.iterrows():
        legacy_id = str(row.get("transcription_id") or idx).strip()
        raw_path = str(row.get("audio_file_path") or "").strip()
        if not raw_path:
            print(f"  skip row {idx}: missing audio_file_path")
            continue

        audio_path = _audio_path_from_row(data_dir, raw_path)
        attach = audio_path.is_file()
        result = _row_to_transcription_result(row)

        name = row.get("audio_file_name")
        if name is None or (isinstance(name, float) and pd.isna(name)):
            name = audio_path.name
        else:
            name = str(name)

        # Include legacy_id in title so rows that share the same filename stay distinct
        # in Neotoma identity resolution (parquet has duplicate basenames across paths).
        extra = {
            "title": f"Migrated transcription {legacy_id} — {name}",
            "legacy_parquet_transcription_id": legacy_id,
            "legacy_parquet_audio_file_path": raw_path,
            "data_source": (
                f"migrate_transcriptions_parquet_to_neotoma.py parquet_id={legacy_id} "
                f"source_file=transcriptions.parquet"
            ),
        }
        td = _fmt_date(row.get("transcription_date"))
        if td:
            extra["transcription_date"] = td
        imp = _fmt_date(row.get("import_date"))
        if imp:
            extra["import_date"] = imp

        idem = f"migration-transcription-parquet-{legacy_id}"
        file_idem = f"migration-transcription-wav-parquet-{legacy_id}"

        print(
            f"[{done + 1}] legacy_id={legacy_id} audio={audio_path} "
            f"attach_wav={attach} chars={len(result['transcription_text'])}"
        )

        if args.dry_run:
            done += 1
            continue

        sd = row.get("source_directory")
        if sd is None or (isinstance(sd, float) and pd.isna(sd)):
            source_directory = None
        else:
            source_directory = str(sd) or None

        try:
            _save_with_retries(
                audio_path=audio_path,
                transcription_result=result,
                source_directory=source_directory,
                observation_source="import",
                idempotency_key=idem,
                file_idempotency_key=file_idem,
                attach_audio_file=attach,
                extra_entity_fields=extra,
            )
        except Exception as e:
            failed += 1
            print(f"  ERROR legacy_id={legacy_id}: {e}", flush=True)
            continue
        done += 1
        time.sleep(0.05)

    print(
        f"Migrated (or dry-run enumerated) {done} row(s)."
        + (f" Failed: {failed}." if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
