#!/usr/bin/env python3
"""
Backfill Neotoma ``transcription`` entities from existing transcript sidecars.

Context (ateles#1083): every voice memo Tyto transcribed while pointed at
hosted Neotoma failed at the STORE step with ``ERR_FILE_PATH_IS_SERVER_LOCAL``
— the expensive part (whisper) succeeded and wrote a ``<audio>.transcript.txt``
sidecar next to the recording; only the durable Neotoma write was lost. Once
the store-path fix in ``transcribe_audio.py`` (``save_transcription`` now
attaches audio via ``neotoma ingest --source-file`` instead of the
server-local ``store --file-path``) has landed, this script recovers every
affected memo WITHOUT re-invoking whisper: it reads the sidecar text straight
off disk and calls the same ``save_transcription`` path Tyto itself uses, so
the backfill is authenticated, tenant-scoped, and idempotency-keyed exactly
like a live transcription — never a parallel writer.

Usage:
    python execution/scripts/backfill_voice_memo_transcriptions_from_sidecars.py [dir ...]
    python execution/scripts/backfill_voice_memo_transcriptions_from_sidecars.py --dry-run
    python execution/scripts/backfill_voice_memo_transcriptions_from_sidecars.py --limit 5

With no positional directory, scans ``TYTO_VOICE_MEMOS_DIR`` (or Tyto's
documented default). Only files with BOTH the audio and its
``<audio>.<ext>.transcript.txt`` sidecar are eligible; an audio file with no
sidecar is not this script's job (it was never transcribed, or the sidecar
write itself failed — either way, re-run transcription, not this backfill).

A memo already represented by a ``transcription`` entity (matched the same
way live transcription dedupes: content hash, falling back to path) is
skipped, so a re-run after a partial backfill or a crash is safe to repeat.

Exit codes:
    0 — completed (see printed counts; a failed store is still reported here,
        not a nonzero exit, to allow a heterogeneous backlog to finish)
    1 — usage / config error (e.g. scan directory does not exist)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import transcribe_audio as ta  # noqa: E402

# Same extensions Tyto's Voice Memos watcher matches on (tyto.py
# VOICE_MEMO_INCLUDE_QTA / memo_exts). Kept as a local constant rather than
# importing tyto.py, which pulls in daemon-runtime/AAuth machinery this
# read-mostly backfill script has no business depending on.
_DEFAULT_MEMO_EXTENSIONS = (".m4a", ".wav", ".qta")

# The sidecar naming convention _write_transcript_sidecars uses: the ORIGINAL
# suffix is preserved and ".transcript.txt" is appended, not swapped in — so
# for "memo.m4a" the sidecar is "memo.m4a.transcript.txt", not
# "memo.transcript.txt".
_SIDECAR_SUFFIX = ".transcript.txt"


def _default_scan_dir() -> Path | None:
    import os

    env = os.environ.get("TYTO_VOICE_MEMOS_DIR", "").strip()
    if env:
        return Path(env)
    home = Path.home()
    default = home / "Library" / "Group Containers" / "group.com.apple.VoiceMemos.shared" / "Recordings"
    return default if default.exists() else None


def _sidecar_for(audio_path: Path) -> Path:
    return audio_path.with_suffix(audio_path.suffix + _SIDECAR_SUFFIX)


def find_backfill_candidates(
    scan_dirs: list[Path], extensions: tuple[str, ...] = _DEFAULT_MEMO_EXTENSIONS
) -> list[Path]:
    """Return audio files under ``scan_dirs`` that have a sidecar transcript.

    Non-recursive per directory (matches the Voice Memos layout Tyto watches:
    a flat directory of recordings) but accepts multiple directories so a
    caller can point at more than one memo location in one run.
    """
    exts = {e.lower() for e in extensions}
    candidates: list[Path] = []
    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for path in sorted(scan_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in exts:
                continue
            if _sidecar_for(path).is_file():
                candidates.append(path)
    return candidates


def backfill_one(audio_path: Path, *, dry_run: bool = False) -> str:
    """Backfill a single memo from its sidecar. Returns a status string.

    One of: "stored", "skipped_existing", "would_store" (dry-run), "failed".
    Never invokes whisper — the sidecar IS the transcription result.
    """
    sidecar = _sidecar_for(audio_path)
    text = sidecar.read_text(encoding="utf-8").strip()
    if not text:
        return "failed"

    if ta.is_already_transcribed(audio_path):
        return "skipped_existing"

    if dry_run:
        return "would_store"

    try:
        ta.save_transcription(
            audio_path,
            {
                "transcription_text": text,
                "language": "auto",
                # No audio_duration_seconds / engine metadata survives in the
                # sidecar alone — omitted rather than fabricated. A live
                # transcription (not this backfill) is the source for those.
            },
            observation_source="import",
            attach_audio_file=True,
        )
        return "stored"
    except Exception as exc:  # noqa: BLE001 — keep the backlog moving
        print(f"  FAILED {audio_path.name}: {exc}", file=sys.stderr)
        return "failed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dirs",
        nargs="*",
        type=Path,
        help="Directories to scan (default: TYTO_VOICE_MEMOS_DIR / Tyto's default)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report counts, store nothing")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N stores")
    args = parser.parse_args(argv)

    scan_dirs = args.dirs or ([d] if (d := _default_scan_dir()) else [])
    if not scan_dirs:
        print(
            "No scan directory: pass one or more directories, or set "
            "TYTO_VOICE_MEMOS_DIR.",
            file=sys.stderr,
        )
        return 1
    missing = [d for d in scan_dirs if not d.is_dir()]
    if missing:
        print(f"Not a directory: {', '.join(str(d) for d in missing)}", file=sys.stderr)
        return 1

    candidates = find_backfill_candidates(scan_dirs)
    print(f"Scanned {len(scan_dirs)} dir(s); {len(candidates)} memo(s) have a transcript sidecar.")

    counts = {"stored": 0, "skipped_existing": 0, "would_store": 0, "failed": 0}
    for audio_path in candidates:
        if args.limit is not None and counts["stored"] >= args.limit:
            break
        status = backfill_one(audio_path, dry_run=args.dry_run)
        counts[status] += 1
        print(f"  {status}: {audio_path.name}")

    print(
        f"Done. scanned={len(candidates)} stored={counts['stored']} "
        f"skipped_existing={counts['skipped_existing']} "
        f"would_store={counts['would_store']} failed={counts['failed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
