#!/usr/bin/env python3
"""Instrument validation log for the `evaluate-person` skill (step 3).

Every count that reaches the evaluation must have an entry here showing the
query that produced it, a known-positive case the query was anchored against,
and whether the anchor passed -- *in the same execution as the count itself*.
A count whose run recorded no live non-zero anchor is refused.

Motivating failure: Neotoma field counts taken *before* a schema migration were
read as facts about the subject's output. The same session also hit undeclared
schema fields that accept writes and read back empty. In both cases the number
was a property of the instrument, not of the person.

Motivating failure, second round (D16, dogfood run 2026-09-22). Four background
scans ran over the same corpus. Two were broken, and they failed in OPPOSITE
directions:

  * one reported a name absent. Correct answer, zero evidence: the run's input
    file list was deleted mid-flight, so every lookup after the first read a
    missing path and returned 0. The only thing that exposed it was that the
    ANCHOR names collapsed to 0 in the same execution.
  * one reported the same name present in all 414 files searched, and another
    name 513 times against 414 files. Wrong answer: `grep -lic | wc -l` counts
    files, not matches. The structural tell is that a match count exceeding the
    file population is impossible -- not that any individual row looked wrong.

So the anchor requirement is NOT a guard on zeros. It is a guard on every number
the instrument emits. A non-zero is exactly as capable of being fabricated as a
zero, and a confident wrong non-zero is more likely to be believed. Two of four
runs were wrong; without same-run anchors either could have shipped as fact, and
the two supported opposite findings with equal apparent confidence.

The rule this encodes: a number is a claim about your tooling before it is a
claim about the world. Prove the query live, in the run that produced the
number, on a case you have already seen with your own eyes.

Usage
-----
    # 1. open a run and record the anchors THAT RUN observed
    instrument_log.py run --path L --run-id scan-01 \\
        --population 414 \\
        --anchor "DeGannes in transcript 2026-08-14" --anchor-count 3 \\
        --anchor "Manju in transcript 2026-08-14" --anchor-count 27

    # 2. record counts against that run
    instrument_log.py add --path L --run-id scan-01 \\
        --metric "slack msgs from subject" \\
        --query "search_threads from:subject" --count 312 \\
        --anchor "thread 1a2b3c seen manually on 2026-03-04" \\
        --anchor-result pass
    instrument_log.py add --path L --run-id scan-01 --metric "..." \\
        --query "..." --count 0 --anchor "..." --anchor-result pass \\
        --hazard undeclared-schema-field

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
        return {"created_at": _now(), "entries": [], "runs": {}}
    data = json.loads(path.read_text())
    data.setdefault("entries", [])
    data.setdefault("runs", {})
    return data


def _save(path: Path, data: dict) -> None:
    data["updated_at"] = _now()
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def cmd_run(args: argparse.Namespace) -> int:
    """Open (or re-record) an instrument run and its same-run anchor block."""
    path = Path(args.path)
    data = _load(path)
    anchors = list(args.anchor or [])
    counts = list(args.anchor_count or [])
    if len(anchors) != len(counts):
        sys.exit(
            "each --anchor needs exactly one --anchor-count, in order "
            f"(got {len(anchors)} anchor(s), {len(counts)} count(s))"
        )
    if not anchors:
        sys.exit(
            "a run needs at least one known-positive anchor observed IN THIS RUN "
            "-- that is the whole point of the block (SKILL.md 3.1)"
        )
    data["runs"][args.run_id] = {
        "run_id": args.run_id,
        "population": args.population,
        "anchors": [{"case": c, "count": n} for c, n in zip(anchors, counts)],
        "note": args.note,
        "recorded_at": _now(),
    }
    _save(path, data)
    live = sum(1 for n in counts if n > 0)
    print(
        f"run {args.run_id!r}: {len(anchors)} anchor(s), {live} non-zero"
        + (f", population {args.population}" if args.population is not None else "")
    )
    for problem in _run_failures(data["runs"][args.run_id]):
        print(f"  ! {problem}", file=sys.stderr)
    return 0


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
            "run_id": args.run_id,
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


def _run_failures(run: dict) -> list[str]:
    """Structural checks on a single instrument run's anchor block (D16).

    Two independent failure signatures, both observed live:
      * every anchor collapsed to zero -- the run's input vanished under it;
      * an anchor count exceeding the population searched -- impossible, so the
        instrument is counting something other than matches.
    """
    out: list[str] = []
    rid = run.get("run_id")
    anchors = run.get("anchors") or []
    if not anchors:
        out.append(f"run {rid}: no known-positive anchor observed in this run")
        return out
    if not any((a.get("count") or 0) > 0 for a in anchors):
        out.append(
            f"run {rid}: every known-positive anchor came back zero in this run "
            f"({', '.join(str(a.get('case')) for a in anchors)}) -- the instrument "
            "saw nothing it was proven to see, so every number from this run is a "
            "measurement of the instrument. Re-run and recount."
        )
    pop = run.get("population")
    if pop is not None:
        for a in anchors:
            n = a.get("count")
            if n is not None and n > pop:
                out.append(
                    f"run {rid}: anchor {a.get('case')!r} counted {n} against a "
                    f"population of {pop} -- a match count cannot exceed the "
                    "population searched. The instrument is counting the wrong "
                    "thing (the live case was `grep -lic | wc -l`, which emits a "
                    "line per file including zero-match files). Every number from "
                    "this run is void, including the plausible ones."
                )
    return out


def _failures(data: dict) -> list[str]:
    out: list[str] = []
    runs = data.get("runs", {})
    if not data["entries"]:
        out.append("log is empty -- no count in the evaluation has been validated")

    for run in sorted(runs.values(), key=lambda r: str(r.get("run_id"))):
        out.extend(_run_failures(run))

    for e in data["entries"]:
        m = e["metric"]
        # --- D16: same-run anchoring, for EVERY count, not just zeros ---
        rid = e.get("run_id")
        if not rid:
            out.append(
                f"{m}: no run_id -- a count must name the instrument run that "
                "produced it, and that run must have recorded a live non-zero "
                "anchor. An anchor proven once and cited later says nothing about "
                "the run that emitted this number (SKILL.md 3.1)"
            )
        elif rid not in runs:
            out.append(
                f"{m}: cites run {rid!r}, which has no anchor block -- open it "
                f"with `run --run-id {rid} --anchor ... --anchor-count ...`"
            )
        if not e.get("anchor"):
            out.append(f"{m}: no known-positive anchor named")
            continue
        if e.get("anchor_result") != "pass":
            out.append(
                f"{m}: anchor did not pass ({e.get('anchor_result')}) -- "
                f"count {e.get('count')} is a measurement of the instrument"
            )
            continue
        # A ZERO additionally needs the named instrument hazards ruled out: a
        # well-formed query against an undeclared field returns a true zero.
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
    runs = len(data.get("runs", {}))
    print(
        f"INSTRUMENTS OK: {n} count(s) logged across {runs} anchored run(s); "
        f"{zeros} zero(s) hazard-checked."
    )
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    data = _load(Path(args.path))
    rows = data["entries"]
    runs = data.get("runs", {})

    def anchor_summary(e: dict) -> str:
        r = runs.get(e.get("run_id") or "")
        if not r:
            return "-"
        return ", ".join(f"{a.get('case')}={a.get('count')}" for a in r.get("anchors", []))

    if args.format == "html":
        print('<table class="instruments"><thead><tr>'
              "<th>Metric</th><th>Count</th><th>Query</th>"
              "<th>Known-positive anchor</th><th>Anchor</th><th>Run</th>"
              "<th>Same-run anchors</th><th>Computed</th>"
              "</tr></thead><tbody>")
        for e in rows:
            cells = [
                e["metric"], e.get("count"), e.get("query"),
                e.get("anchor"), e.get("anchor_result"), e.get("run_id"),
                anchor_summary(e), e.get("computed_at"),
            ]
            tds = "".join(
                f"<td>{html.escape(str(c)) if c is not None else '-'}</td>" for c in cells
            )
            print(f"<tr>{tds}</tr>")
        print("</tbody></table>")
    else:
        print("| Metric | Count | Query | Known-positive anchor | Anchor | Run "
              "| Same-run anchors | Computed |")
        print("|---|---|---|---|---|---|---|---|")
        for e in rows:
            print(
                f"| {e['metric']} | {e.get('count')} | `{e.get('query')}` "
                f"| {e.get('anchor')} | {e.get('anchor_result')} | {e.get('run_id')} "
                f"| {anchor_summary(e)} | {e.get('computed_at')} |"
            )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="record an instrument run's same-run anchor block")
    r.add_argument("--path", required=True)
    r.add_argument("--run-id", required=True)
    r.add_argument("--population", type=int, default=None,
                   help="items searched by this run; anchors exceeding it are impossible")
    r.add_argument("--anchor", action="append",
                   help="a case already seen that this run MUST match; repeatable")
    r.add_argument("--anchor-count", action="append", type=int,
                   help="what this run actually returned for the matching --anchor")
    r.add_argument("--note", default=None)
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("add")
    a.add_argument("--path", required=True)
    a.add_argument("--metric", required=True)
    a.add_argument("--query", required=True)
    a.add_argument("--count", type=int, required=True)
    a.add_argument("--run-id", default=None,
                   help="the instrument run this count came from; must have an anchor block")
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
