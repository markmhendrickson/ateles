#!/usr/bin/env python3
"""Instrument validation log for the `evaluate-person` skill (step 3).

Every count that reaches the evaluation must have an entry here showing the
query that produced it, a known-positive case the query was anchored against,
and whether the anchor passed. A zero with no passing anchor is refused.

Motivating failure: Neotoma field counts taken *before* a schema migration were
read as facts about the subject's output. The same session also hit undeclared
schema fields that accept writes and read back empty. In both cases the number
was a property of the instrument, not of the person.

The rule this encodes: a zero, or a surprisingly low count, is a claim about
your tooling before it is a claim about the world. Prove the query non-zero on
a case you have already seen with your own eyes, then trust it -- not before.

Usage
-----
    instrument_log.py add --path L --metric "slack msgs from subject" \\
        --query "search_threads from:subject" --count 312 \\
        --anchor "thread 1a2b3c seen manually on 2026-03-04" --anchor-result pass
    instrument_log.py add --path L --metric "..." --query "..." --count 0 \\
        --anchor "..." --anchor-result fail --note "field undeclared on schema"
    instrument_log.py check  --path L
    instrument_log.py render --path L [--format html|markdown]

Exit codes: 0 pass, 1 check failed, 2 usage error.

Stdlib only.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# The four instrument failures named in SKILL.md 3.2, offered as --hazard tags
# so a log entry can record which one was specifically ruled out.
HAZARDS = (
    "undeclared-schema-field",
    "pre-migration-snapshot",
    "stale-tracker",
    "field-name-variant",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict:
    if not path.exists():
        return {"created_at": _now(), "entries": []}
    return json.loads(path.read_text())


def _save(path: Path, data: dict) -> None:
    data["updated_at"] = _now()
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def cmd_add(args: argparse.Namespace) -> int:
    path = Path(args.path)
    data = _load(path)
    for h in args.hazard or []:
        if h not in HAZARDS:
            sys.exit(f"unknown hazard {h!r}; known: {HAZARDS}")
    data["entries"].append(
        {
            "metric": args.metric,
            "query": args.query,
            "count": args.count,
            "anchor": args.anchor,
            "anchor_result": args.anchor_result,
            "hazards_ruled_out": args.hazard or [],
            "computed_at": args.computed_at or _now(),
            "note": args.note,
        }
    )
    _save(path, data)
    status = "ANCHORED" if args.anchor_result == "pass" else "NOT TRUSTWORTHY"
    print(f"logged {args.metric!r} = {args.count} [{status}]")
    if args.anchor_result != "pass":
        print(
            "  the anchor did not pass -- the instrument is broken. Fix it and "
            "recount. Do not report this number.",
            file=sys.stderr,
        )
    return 0


def _failures(data: dict) -> list[str]:
    out: list[str] = []
    if not data["entries"]:
        out.append("log is empty -- no count in the evaluation has been validated")
    for e in data["entries"]:
        m = e["metric"]
        if not e.get("anchor"):
            out.append(f"{m}: no known-positive anchor named")
            continue
        if e.get("anchor_result") != "pass":
            out.append(
                f"{m}: anchor did not pass ({e.get('anchor_result')}) -- "
                f"count {e.get('count')} is a measurement of the instrument"
            )
            continue
        # A passing anchor licenses a non-zero count. A ZERO additionally needs
        # the anchor to have been run through the *same* query, which is what the
        # anchor field asserts -- so the remaining risk is a stale computation.
        if e.get("count") == 0 and not e.get("hazards_ruled_out"):
            out.append(
                f"{m}: reports zero with a passing anchor but no hazard ruled out "
                f"-- name which of {HAZARDS} was checked"
            )
    return out


def cmd_check(args: argparse.Namespace) -> int:
    data = _load(Path(args.path))
    problems = _failures(data)
    if problems:
        print(f"INSTRUMENTS NOT VALIDATED ({len(problems)} problem(s)):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    n = len(data["entries"])
    zeros = sum(1 for e in data["entries"] if e.get("count") == 0)
    print(f"INSTRUMENTS OK: {n} count(s) logged, all anchored; {zeros} zero(s) hazard-checked.")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    data = _load(Path(args.path))
    rows = data["entries"]
    if args.format == "html":
        print('<table class="instruments"><thead><tr>'
              "<th>Metric</th><th>Count</th><th>Query</th>"
              "<th>Known-positive anchor</th><th>Anchor</th><th>Computed</th>"
              "</tr></thead><tbody>")
        for e in rows:
            cells = [
                e["metric"], e.get("count"), e.get("query"),
                e.get("anchor"), e.get("anchor_result"), e.get("computed_at"),
            ]
            tds = "".join(
                f"<td>{html.escape(str(c)) if c is not None else '-'}</td>" for c in cells
            )
            print(f"<tr>{tds}</tr>")
        print("</tbody></table>")
    else:
        print("| Metric | Count | Query | Known-positive anchor | Anchor | Computed |")
        print("|---|---|---|---|---|---|")
        for e in rows:
            print(
                f"| {e['metric']} | {e.get('count')} | `{e.get('query')}` "
                f"| {e.get('anchor')} | {e.get('anchor_result')} | {e.get('computed_at')} |"
            )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add")
    a.add_argument("--path", required=True)
    a.add_argument("--metric", required=True)
    a.add_argument("--query", required=True)
    a.add_argument("--count", type=int, required=True)
    a.add_argument("--anchor", required=True, help="a case you have already seen that this query MUST match")
    a.add_argument("--anchor-result", required=True, choices=("pass", "fail"))
    a.add_argument("--hazard", action="append", help=f"one of {HAZARDS}; repeatable")
    a.add_argument("--computed-at", default=None)
    a.add_argument("--note", default=None)
    a.set_defaults(func=cmd_add)

    c = sub.add_parser("check")
    c.add_argument("--path", required=True)
    c.set_defaults(func=cmd_check)

    d = sub.add_parser("render")
    d.add_argument("--path", required=True)
    d.add_argument("--format", choices=("html", "markdown"), default="markdown")
    d.set_defaults(func=cmd_render)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
