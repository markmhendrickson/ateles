#!/usr/bin/env python3
"""
Backfill /analyze against private docs files.

Walks a base directory (default: ``docs/private`` in the repo where this is
invoked) for analyzable markdown files, computes per-file fingerprints, and
emits one ``ANALYZE_TRIGGER: <path>`` line per file that has not yet been
analyzed (per Neotoma) so the calling agent can fan out ``/analyze`` calls.

This script does NOT itself call ``/analyze`` — that requires the agent (the
``/analyze`` skill runs LLM-driven). The script is a deterministic batch
driver: it filters the corpus, dedupes against prior analyses by querying
Neotoma, and prints work items for the agent to process.

The agent reads the trigger lines, runs ``/analyze <path>`` per file with
``ANALYZE_BACKFILL=1`` set (so the skill emits compact JSON status lines
instead of full chat output), and resumes from the script's progress log on
crash / interruption.

Usage:
    python execution/scripts/backfill_analyze_private_docs.py
    python execution/scripts/backfill_analyze_private_docs.py --base-dir docs/private
    python execution/scripts/backfill_analyze_private_docs.py --include 'insights/**/*.md'
    python execution/scripts/backfill_analyze_private_docs.py --since 2025-01-01
    python execution/scripts/backfill_analyze_private_docs.py --dry-run
    python execution/scripts/backfill_analyze_private_docs.py --no-dedupe

Exit codes:
    0 — completed normally (work items printed, or no work)
    1 — usage / config error
    2 — Neotoma dedupe lookup failed (script still prints work items and
        exits 2 so the agent can decide whether to proceed degraded)
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Defaults: relative globs under --base-dir that are worth analyzing. The
# private docs submodule typically holds insights/, strategy/, icp/,
# customer-development/, market/, and similar. Add more glob patterns to
# DEFAULT_INCLUDE rather than expanding scope at the directory level.
DEFAULT_INCLUDE = [
    "insights/**/*.md",
    "strategy/**/*.md",
    "icp/**/*.md",
    "customer-development/**/*.md",
    "market/**/*.md",
    "competitive/**/*.md",
    "partnership/**/*.md",
    "research/**/*.md",
]

DEFAULT_EXCLUDE = [
    "**/README.md",
    "**/INDEX.md",
    "**/_drafts/**",
    "**/.archive/**",
    "**/_template*",
]

ANALYSIS_KIND_DEFAULT = "relevance"  # private docs are usually insights/research,
# not competitive teardowns. /analyze auto-detects kind from content, so this
# is only a hint passed in the trigger line.

PROGRESS_LOG_NAME = "analyze_backfill_progress.jsonl"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive backfill /analyze runs over private docs files."
    )
    parser.add_argument(
        "--base-dir",
        default=None,
        help=(
            "Root directory to walk. Defaults to docs/private under the"
            " current working directory."
        ),
    )
    parser.add_argument(
        "--include",
        action="append",
        default=None,
        help=(
            "Glob pattern (relative to --base-dir) to include. Repeatable."
            " Overrides DEFAULT_INCLUDE when passed."
        ),
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        help=(
            "Glob pattern (relative to --base-dir) to exclude. Repeatable."
            " Appended to DEFAULT_EXCLUDE."
        ),
    )
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "ISO date (YYYY-MM-DD). Skip files whose mtime is earlier than"
            " this date."
        ),
    )
    parser.add_argument(
        "--no-dedupe",
        action="store_true",
        help=(
            "Skip the Neotoma dedupe lookup; emit triggers for every matched"
            " file. Useful when Neotoma is unreachable or for forced reruns."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the matched files but do NOT emit ANALYZE_TRIGGER lines.",
    )
    parser.add_argument(
        "--progress-log",
        default=None,
        help=(
            "Path to a JSONL file used to record per-file outcomes. Defaults"
            " to <base-dir>/" + PROGRESS_LOG_NAME + "."
        ),
    )
    parser.add_argument(
        "--analysis-kind-hint",
        default=ANALYSIS_KIND_DEFAULT,
        choices=["competitive", "partnership", "relevance", "mixed"],
        help="Hint passed in the trigger line; /analyze still auto-detects.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of trigger lines to emit (after dedupe).",
    )
    return parser.parse_args()


def resolve_base_dir(arg: str | None) -> Path:
    if arg is not None:
        return Path(arg).expanduser().resolve()
    # Default: docs/private relative to cwd.
    candidate = Path.cwd() / "docs" / "private"
    if candidate.exists():
        return candidate.resolve()
    print(
        f"Error: --base-dir not provided and {candidate} does not exist."
        " Pass --base-dir explicitly.",
        file=sys.stderr,
    )
    sys.exit(1)


def matches_any(rel_path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel_path, pat) for pat in patterns)


def walk_corpus(
    base_dir: Path, includes: list[str], excludes: list[str], since: str | None
) -> list[Path]:
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            print(
                f"Error: --since must be ISO date (YYYY-MM-DD); got {since!r}.",
                file=sys.stderr,
            )
            sys.exit(1)

    matched: list[Path] = []
    for path in sorted(base_dir.rglob("*.md")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(base_dir))
        if not matches_any(rel, includes):
            continue
        if matches_any(rel, excludes):
            continue
        if since_dt is not None:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if mtime < since_dt:
                continue
        matched.append(path)
    return matched


def file_fingerprint(path: Path) -> str:
    """SHA256 of the file contents — first 12 hex chars. Used in the
    per-analysis idempotency key the /analyze skill computes."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def neotoma_dedupe_lookup(target_identifiers: list[str]) -> tuple[set[str], str | None]:
    """Return the subset of target_identifiers that already have an `analysis`
    entity in Neotoma.

    Uses the ``neotoma`` CLI: ``entities search --identifier <path>
    --entity-type analysis --by target_identifier --limit 1`` per path.

    Returns (already_analyzed_set, error_reason_or_none). On any failure
    returns (set(), reason) so the caller can decide to proceed degraded.
    """
    try:
        from shutil import which

        if not which("neotoma"):
            return set(), "neotoma CLI not on PATH"
    except Exception as exc:  # pragma: no cover — defensive
        return set(), f"shutil.which failed: {exc}"

    already: set[str] = set()
    for ident in target_identifiers:
        try:
            res = subprocess.run(
                [
                    "neotoma",
                    "entities",
                    "search",
                    "--identifier",
                    ident,
                    "--entity-type",
                    "analysis",
                    "--by",
                    "target_identifier",
                    "--limit",
                    "1",
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode != 0:
                continue
            try:
                payload = json.loads(res.stdout or "{}")
            except json.JSONDecodeError:
                continue
            if (payload.get("entities") or []):
                already.add(ident)
        except subprocess.TimeoutExpired:
            return already, f"neotoma CLI timed out on {ident}"
        except Exception as exc:
            return already, f"neotoma CLI error on {ident}: {exc}"

    return already, None


def main() -> int:
    args = parse_args()
    base_dir = resolve_base_dir(args.base_dir)
    if not base_dir.is_dir():
        print(f"Error: --base-dir is not a directory: {base_dir}", file=sys.stderr)
        return 1

    includes = args.include or DEFAULT_INCLUDE
    excludes = (args.exclude or []) + DEFAULT_EXCLUDE

    matched = walk_corpus(base_dir, includes, excludes, args.since)
    if not matched:
        print(f"No files matched under {base_dir}.", file=sys.stderr)
        return 0

    # Compute target identifiers (absolute paths) and fingerprints.
    rows = [
        {
            "path": str(p.resolve()),
            "rel": str(p.relative_to(base_dir)),
            "fingerprint": file_fingerprint(p),
        }
        for p in matched
    ]

    dedupe_error: str | None = None
    if args.no_dedupe:
        already: set[str] = set()
    else:
        already, dedupe_error = neotoma_dedupe_lookup([r["path"] for r in rows])
        if dedupe_error:
            print(
                f"Warning: dedupe lookup failed ({dedupe_error}). Proceeding"
                " without dedupe; the /analyze skill will still update existing"
                " analyses idempotently via its per-analysis key.",
                file=sys.stderr,
            )

    pending = [r for r in rows if r["path"] not in already]
    if args.limit is not None:
        pending = pending[: args.limit]

    print(
        f"Matched {len(rows)} file(s); {len(already)} already analyzed;"
        f" {len(pending)} pending.",
        file=sys.stderr,
    )

    progress_log_path = (
        Path(args.progress_log).expanduser().resolve()
        if args.progress_log
        else base_dir / PROGRESS_LOG_NAME
    )

    if args.dry_run:
        for r in pending:
            print(f"DRY: {r['rel']}  fp={r['fingerprint']}", file=sys.stderr)
        return 0

    # Emit one trigger line per pending file. The agent reads stdout and runs
    # /analyze on each; the skill's ANALYZE_BACKFILL=1 path emits compact JSON
    # status which the agent can append to the progress log.
    for r in pending:
        trigger = {
            "path": r["path"],
            "rel": r["rel"],
            "fingerprint": r["fingerprint"],
            "kind_hint": args.analysis_kind_hint,
            "progress_log": str(progress_log_path),
        }
        print(f"ANALYZE_TRIGGER: {json.dumps(trigger, sort_keys=True)}")

    print(
        f"Run /analyze on each ANALYZE_TRIGGER line above with"
        f" ANALYZE_BACKFILL=1 to emit JSON status; append outcomes to"
        f" {progress_log_path}.",
        file=sys.stderr,
    )

    return 0 if dedupe_error is None else 2


if __name__ == "__main__":
    sys.exit(main())
