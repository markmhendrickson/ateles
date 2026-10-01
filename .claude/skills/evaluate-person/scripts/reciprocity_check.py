#!/usr/bin/env python3
"""Reciprocity gate for the `evaluate-person` skill (steps 1.3 and 3.3).

An expectation table lists what the subject owed. A person's ability to meet it
almost always depends on things they were owed BACK -- an answer, a sign-off, a
price, a correction of a misunderstanding. Nothing in the skill swept for those,
so a literal run built a one-sided table and tested the subject against it.

Motivating failure, dogfood run 2026-09-22. Three owed-TO-subject items were
decisive:

  * a reference price range owed by the CEO, open ~6 weeks, gating four named
    leads and every referral handoff. Without it the unsent-work finding reads
    as pure negligence;
  * a website sign-off owed by two people, requested with a 24-hour ask, which
    blocked her outreach while it was outstanding;
  * a two-month misunderstanding about whether she could pull the operator and
    the CEO into conversations, never corrected until late August, and the
    single largest identified cause of the pipeline shortfall.

A run that followed the skill literally would have produced a materially harsher
and less accurate evaluation AND PASSED EVERY CHECK -- including the 4.3
direction tripwire, which only counts corrections to SUBJECT-AUTHORED citations
and so can never be tripped by a one-sided expectation table.

Hence the gate: **every `not met` or `partially met` finding must state what the
subject was owed in the same window, by whom, and whether it arrived** -- or
explicitly record that the counterpart sweep found nothing owed. "Nothing owed"
is a legitimate answer; not having looked is not. The difference between an
evaluation and a case for the prosecution is whether this question was asked.

Usage
-----
    # what the subject was owed, per expectation window
    reciprocity_check.py owed --path R --expectation E8 \\
        --item "reference price range" --owed-by "CEO" \\
        --requested-on 2026-08-05 --in-force-from 2026-08-05 \\
        --delivered no --blocking "four named leads, every referral handoff"

    # the finding, naming the counterpart items it accounts for
    reciprocity_check.py finding --path R --expectation E8 --verdict "not met" \\
        --counterpart-items "reference price range" \\
        --accounted "verdict downgraded: the gating input never arrived"

    # a finding with nothing owed back -- legitimate, but must be said
    reciprocity_check.py finding --path R --expectation E2 --verdict "not met" \\
        --nothing-owed "swept Slack, Gmail and the tracker for open asks to the \\
        operator in this window; none found"

    reciprocity_check.py check  --path R
    reciprocity_check.py render --path R [--format html|markdown]

Exit codes: 0 pass, 1 gate failed, 2 usage error.

Stdlib only.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Verdicts that make a claim against the subject, and so cannot stand without
# the counterpart question having been asked. `met` and `insufficient evidence`
# are exempt: neither asserts a shortfall.
ACCOUNTABLE_VERDICTS = ("not met", "partially met")

DELIVERED = ("yes", "late", "no", "partial")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict:
    if not path.exists():
        return {"created_at": _now(), "owed_to_subject": [], "findings": []}
    data = json.loads(path.read_text())
    data.setdefault("owed_to_subject", [])
    data.setdefault("findings", [])
    return data


def _save(path: Path, data: dict) -> None:
    data["updated_at"] = _now()
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def cmd_owed(args: argparse.Namespace) -> int:
    path = Path(args.path)
    data = _load(path)
    data["owed_to_subject"].append(
        {
            "expectation_id": args.expectation,
            "item": args.item,
            "owed_by": args.owed_by,
            "requested_on": args.requested_on,
            "in_force_from": args.in_force_from,
            "in_force_to": args.in_force_to,
            "delivered": args.delivered,
            "delivered_on": args.delivered_on,
            "blocking": args.blocking,
            "source": args.source,
            "recorded_at": _now(),
        }
    )
    _save(path, data)
    print(
        f"owed to subject: {args.item!r} by {args.owed_by} "
        f"[{args.expectation}] delivered={args.delivered}"
    )
    return 0


def cmd_finding(args: argparse.Namespace) -> int:
    path = Path(args.path)
    data = _load(path)
    items = [i.strip() for i in (args.counterpart_items or "").split(",") if i.strip()]
    data["findings"].append(
        {
            "expectation_id": args.expectation,
            "verdict": args.verdict,
            "counterpart_items": items,
            "accounted": args.accounted,
            "nothing_owed": args.nothing_owed,
            "recorded_at": _now(),
        }
    )
    _save(path, data)
    print(f"finding {args.expectation}: {args.verdict} "
          f"({len(items)} counterpart item(s))")
    return 0


def _failures(data: dict) -> list[str]:
    out: list[str] = []
    findings = data["findings"]
    owed = data["owed_to_subject"]
    if not findings:
        out.append(
            "no findings recorded -- every finding from SKILL.md 3.3 needs a row "
            "here before the page is rendered"
        )
        return out

    known_items = {o.get("item") for o in owed}
    by_expectation: dict[str, list[dict]] = {}
    for o in owed:
        by_expectation.setdefault(o.get("expectation_id"), []).append(o)

    for f in findings:
        eid = f.get("expectation_id")
        verdict = (f.get("verdict") or "").strip().lower()
        if verdict not in ACCOUNTABLE_VERDICTS:
            continue
        cited = f.get("counterpart_items") or []
        nothing = f.get("nothing_owed")

        if not cited and not nothing:
            out.append(
                f"{eid}: verdict {verdict!r} with no counterpart account -- state "
                "what the subject was owed in this same window, by whom, and "
                "whether it arrived, or record --nothing-owed naming the surfaces "
                "you swept for open asks. A one-sided expectation table produces a "
                "case for the prosecution, and no other guard in this skill "
                "catches it (SKILL.md 1.3 / 3.3)"
            )
            continue

        if nothing and cited:
            out.append(
                f"{eid}: recorded both --nothing-owed and counterpart items "
                f"({', '.join(cited)}) -- these contradict; drop one"
            )

        if nothing and not cited:
            # A negative claim about the counterpart sweep is a claim about the
            # sweep first, exactly as SKILL.md 2.3 requires of any negative.
            if not any(w in nothing.lower() for w in ("swept", "searched", "surface")):
                out.append(
                    f"{eid}: --nothing-owed must name the surfaces swept for open "
                    "asks to the operator in this window -- 'nothing was owed' is a "
                    "claim about your sweep before it is a claim about the operator "
                    "(SKILL.md 2.3)"
                )
            continue

        for item in cited:
            if item not in known_items:
                out.append(
                    f"{eid}: cites counterpart item {item!r} with no `owed` row -- "
                    "record it with who owed it, when it was requested and whether "
                    "it arrived"
                )
        if not f.get("accounted"):
            out.append(
                f"{eid}: names counterpart items but does not say how the verdict "
                "accounts for them -- naming an unmet dependency and then scoring "
                "the subject as if it had been met is the failure with extra steps"
            )

        # An undelivered blocker inside the window, unnamed by the finding.
        for o in by_expectation.get(eid, []):
            if o.get("delivered") in ("no", "partial", "late") and o.get("item") not in cited:
                out.append(
                    f"{eid}: {o.get('item')!r} was owed to the subject by "
                    f"{o.get('owed_by')} in this window and is recorded as "
                    f"delivered={o.get('delivered')}, but the finding does not "
                    "name it. A shortfall verdict must account for every "
                    "outstanding input it depended on"
                )
    return out


def cmd_check(args: argparse.Namespace) -> int:
    data = _load(Path(args.path))
    problems = _failures(data)
    if problems:
        print(f"RECIPROCITY GATE FAILED ({len(problems)} problem(s)):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\nDo not render. Every shortfall verdict must say what the subject was "
            "owed in the same window.",
            file=sys.stderr,
        )
        return 1
    n = len(data["findings"])
    acc = sum(
        1 for f in data["findings"]
        if (f.get("verdict") or "").strip().lower() in ACCOUNTABLE_VERDICTS
    )
    print(
        f"RECIPROCITY OK: {n} finding(s), {acc} shortfall verdict(s) each accounting "
        f"for what the subject was owed; {len(data['owed_to_subject'])} counterpart "
        "obligation(s) recorded."
    )
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    data = _load(Path(args.path))
    rows = data["owed_to_subject"]
    cols = ("expectation_id", "item", "owed_by", "requested_on", "in_force_from",
            "delivered", "delivered_on", "blocking")
    heads = ("Expectation", "What the subject was owed", "Owed by", "Requested",
             "In force from", "Delivered", "Delivered on", "What it blocked")
    if args.format == "html":
        print('<table class="reciprocity"><thead><tr>'
              + "".join(f"<th>{h}</th>" for h in heads)
              + "</tr></thead><tbody>")
        for e in rows:
            tds = "".join(
                f"<td>{html.escape(str(e.get(c))) if e.get(c) is not None else '-'}</td>"
                for c in cols
            )
            print(f"<tr>{tds}</tr>")
        print("</tbody></table>")
    else:
        print("| " + " | ".join(heads) + " |")
        print("|" + "---|" * len(heads))
        for e in rows:
            print("| " + " | ".join(str(e.get(c) or "-") for c in cols) + " |")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("owed", help="record something the SUBJECT was owed")
    o.add_argument("--path", required=True)
    o.add_argument("--expectation", required=True, help="the expectation row this bears on")
    o.add_argument("--item", required=True)
    o.add_argument("--owed-by", required=True)
    o.add_argument("--requested-on", default=None)
    o.add_argument("--in-force-from", default=None)
    o.add_argument("--in-force-to", default=None)
    o.add_argument("--delivered", required=True, choices=DELIVERED)
    o.add_argument("--delivered-on", default=None)
    o.add_argument("--blocking", default=None, help="what its absence blocked")
    o.add_argument("--source", default=None, help="resolvable citation for the ask")
    o.set_defaults(func=cmd_owed)

    f = sub.add_parser("finding", help="record a finding and its counterpart account")
    f.add_argument("--path", required=True)
    f.add_argument("--expectation", required=True)
    f.add_argument("--verdict", required=True)
    f.add_argument("--counterpart-items", default=None,
                   help="comma-separated items from `owed` that this verdict accounts for")
    f.add_argument("--accounted", default=None,
                   help="how the verdict accounts for them")
    f.add_argument("--nothing-owed", default=None,
                   help="the surfaces swept that found nothing owed to the subject here")
    f.set_defaults(func=cmd_finding)

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
