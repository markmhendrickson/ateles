#!/usr/bin/env python3
"""Corpus-sweep manifest for the `evaluate-person` skill (step 2).

Records, per surface, how many items matched the subject, how many were actually
read, and what any filter excluded. Refuses to call a sweep complete when a
surface claims `swept` without a known denominator or with unread matches.

Motivating failure: an evaluation was drafted from 3 of 24 Slack conversations
behind a date floor chosen because it "seemed recent enough". Nothing in the
process noticed, because 3 conversations felt like enough material. The fix is
to make "enough material" unrepresentable -- a surface is either read to its
boundary or it is marked `partial` and says so in the output.

Usage
-----
    sweep_manifest.py init   --path M --subject NAME [--window FROM..TO]
    sweep_manifest.py record --path M --surface slack --status swept \\
        --total-matched 24 --total-read 24 [--excluded-by-filter 0] \\
        [--filter "window 2026-01-01..2026-09-22"] [--note "..."]
    sweep_manifest.py check  --path M
    sweep_manifest.py render --path M [--format html|markdown]
    sweep_manifest.py cite   --path M --surfaces slack,gmail,drive

Exit codes: 0 pass, 1 check failed, 2 usage error.

Stdlib only. No network, no Neotoma writes -- this is a local bookkeeping file.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

VALID_STATUS = ("swept", "partial", "unreachable", "not-applicable")

DEFAULT_SURFACES = [
    "operator-neotoma",
    "client-neotoma",
    "gmail",
    "slack",
    "meeting-transcripts",
    "calendar",
    "drive",
    "repos",
    "external-tooling",
    "local-files",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"manifest not found: {path} (run `init` first)")
    return json.loads(path.read_text())


def _save(path: Path, data: dict) -> None:
    data["updated_at"] = _now()
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.exists() and not args.force:
        sys.exit(f"manifest already exists: {path} (use --force to overwrite)")
    data = {
        "subject": args.subject,
        "window": args.window,
        "created_at": _now(),
        "surfaces": {
            name: {
                "status": None,
                "total_matched": None,
                "total_read": None,
                "excluded_by_filter": None,
                "filter": None,
                "note": None,
                "recorded_at": None,
            }
            for name in (args.surfaces.split(",") if args.surfaces else DEFAULT_SURFACES)
        },
    }
    _save(path, data)
    print(f"initialized manifest for {args.subject} at {path}")
    print(f"surfaces pending: {', '.join(sorted(data['surfaces']))}")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    path = Path(args.path)
    data = _load(path)
    if args.status not in VALID_STATUS:
        sys.exit(f"status must be one of {VALID_STATUS}")
    if args.status != "swept" and not (args.note or args.filter):
        sys.exit(
            f"status '{args.status}' requires --note explaining why the surface "
            "was not fully swept (skill SKILL.md 2.1)"
        )
    data["surfaces"][args.surface] = {
        "status": args.status,
        "total_matched": args.total_matched,
        "total_read": args.total_read,
        "excluded_by_filter": args.excluded_by_filter,
        "filter": args.filter,
        "note": args.note,
        "recorded_at": _now(),
    }
    _save(path, data)
    print(f"recorded {args.surface}: {args.status}")
    return 0


def _failures(data: dict) -> list[str]:
    """Return the list of completeness violations. Empty list == pass."""
    out: list[str] = []
    for name, s in sorted(data["surfaces"].items()):
        status = s.get("status")
        if status is None:
            out.append(f"{name}: no disposition recorded")
            continue
        if status != "swept":
            # partial/unreachable/not-applicable are legitimate, but must carry
            # a reason -- enforced at record time and re-checked here.
            if not (s.get("note") or s.get("filter")):
                out.append(f"{name}: status '{status}' with no reason recorded")
            continue
        matched, read = s.get("total_matched"), s.get("total_read")
        if matched is None:
            out.append(
                f"{name}: claims 'swept' with no denominator "
                "(total_matched is null) -- cannot distinguish complete from lucky"
            )
            continue
        if read is None:
            out.append(f"{name}: claims 'swept' with no total_read")
            continue
        if read < matched:
            out.append(
                f"{name}: claims 'swept' but read {read} of {matched} matched items "
                f"({matched - read} unread) -- this is 'partial', not 'swept'"
            )
        if s.get("excluded_by_filter") is None and s.get("filter"):
            out.append(
                f"{name}: a filter is recorded ({s['filter']!r}) but its "
                "excluded count is null -- an unmeasured filter is an unjustified one"
            )
    return out


def cmd_check(args: argparse.Namespace) -> int:
    data = _load(Path(args.path))
    problems = _failures(data)
    if problems:
        print(f"SWEEP INCOMPLETE ({len(problems)} problem(s)):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\nDo not proceed to step 3. A conclusion drawn now is drawn from an "
            "unknown fraction of the corpus.",
            file=sys.stderr,
        )
        return 1
    swept = sum(1 for s in data["surfaces"].values() if s["status"] == "swept")
    total = len(data["surfaces"])
    print(f"SWEEP OK: {swept}/{total} surfaces swept to boundary; rest dispositioned.")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    data = _load(Path(args.path))
    rows = sorted(data["surfaces"].items())
    if args.format == "html":
        print('<table class="sweep"><thead><tr>'
              "<th>Surface</th><th>Status</th><th>Matched</th>"
              "<th>Read</th><th>Excluded by filter</th><th>Note</th>"
              "</tr></thead><tbody>")
        for name, s in rows:
            cells = [
                name,
                s.get("status") or "-",
                s.get("total_matched"),
                s.get("total_read"),
                s.get("excluded_by_filter"),
                s.get("note") or s.get("filter") or "",
            ]
            tds = "".join(
                f"<td>{html.escape(str(c)) if c is not None else '-'}</td>" for c in cells
            )
            print(f"<tr>{tds}</tr>")
        print("</tbody></table>")
    else:
        print("| Surface | Status | Matched | Read | Excluded | Note |")
        print("|---|---|---|---|---|---|")
        for name, s in rows:
            print(
                f"| {name} | {s.get('status') or '-'} | {s.get('total_matched')} "
                f"| {s.get('total_read')} | {s.get('excluded_by_filter')} "
                f"| {s.get('note') or s.get('filter') or ''} |"
            )
    return 0


def cmd_cite(args: argparse.Namespace) -> int:
    """Emit the surface-citation block that must accompany a negative finding."""
    data = _load(Path(args.path))
    wanted = [s.strip() for s in args.surfaces.split(",") if s.strip()]
    searched, not_searched = [], []
    for name in wanted:
        s = data["surfaces"].get(name)
        if s is None:
            sys.exit(f"surface not in manifest: {name}")
        if s["status"] == "swept":
            n = s.get("total_matched")
            searched.append(f"{name} ({n} items, full range)" if n is not None else name)
        else:
            reason = s.get("note") or s.get("status")
            not_searched.append(f"{name} ({reason})")
    parts = []
    if searched:
        parts.append("Searched: " + ", ".join(searched) + ".")
    if not_searched:
        parts.append("Not searched: " + ", ".join(not_searched) + ".")
    if not parts:
        sys.exit("no surfaces resolved -- a negative finding needs a citation")
    print(" ".join(parts))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init")
    i.add_argument("--path", required=True)
    i.add_argument("--subject", required=True)
    i.add_argument("--window", default=None)
    i.add_argument("--surfaces", default=None, help="comma-separated; defaults to the standard list")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    r = sub.add_parser("record")
    r.add_argument("--path", required=True)
    r.add_argument("--surface", required=True)
    r.add_argument("--status", required=True, choices=VALID_STATUS)
    r.add_argument("--total-matched", type=int, default=None)
    r.add_argument("--total-read", type=int, default=None)
    r.add_argument("--excluded-by-filter", type=int, default=None)
    r.add_argument("--filter", default=None)
    r.add_argument("--note", default=None)
    r.set_defaults(func=cmd_record)

    c = sub.add_parser("check")
    c.add_argument("--path", required=True)
    c.set_defaults(func=cmd_check)

    d = sub.add_parser("render")
    d.add_argument("--path", required=True)
    d.add_argument("--format", choices=("html", "markdown"), default="markdown")
    d.set_defaults(func=cmd_render)

    t = sub.add_parser("cite")
    t.add_argument("--path", required=True)
    t.add_argument("--surfaces", required=True)
    t.set_defaults(func=cmd_cite)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
