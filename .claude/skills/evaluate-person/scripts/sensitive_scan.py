#!/usr/bin/env python3
"""RGPD Art. 9 scan for the `evaluate-person` skill (step 0.2 / 6.3).

A durable evaluation profile of a named third party is people-data processing
under Art. 6(1)(f), not the household exemption. Art. 9 special categories --
health, sex life, religion, politics, trade-union membership, ethnicity,
biometrics -- must not be persisted into the profile even when they turned up in
the corpus and even when they explain a performance fact.

Where a special category EXPLAINS something (an illness behind a delivery gap),
record the effect and not the cause:

    BAD:  "missed the March deadline while being treated for <condition>"
    GOOD: "unavailable 12-26 March; reason known to the operator, not recorded here"

This is a lexical screen, so it over-matches by design: it flags candidates for
a human read, it does not adjudicate. A hit is a BLOCKER, not a warning -- clear
it by rewording or by confirming the match is a false positive and saying so.

Usage
-----
    sensitive_scan.py FILE [FILE ...] [--json] [--quiet]
    cat page.html | sensitive_scan.py -

Exit codes: 0 clean, 1 hits found, 2 usage error.

Stdlib only. Reads files; writes nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Grouped by Art. 9 category. Word-boundary anchored to keep the false-positive
# rate survivable; still deliberately broad.
CATEGORIES: dict[str, list[str]] = {
    "health": [
        r"diagnos(?:is|ed|tic)", r"\billness\b", r"\bdisease\b", r"\bcancer\b",
        r"\bdepress(?:ion|ed)\b", r"\banxiety\b", r"\bburn(?:ed)?[- ]?out\b",
        r"\btherapy\b", r"\btherapist\b", r"\bpsychiatr", r"\bmedication\b",
        r"\bsurgery\b", r"\bhospital(?:ized|ised)?\b", r"\bchronic\b",
        r"\bdisab(?:led|ility)\b", r"\bsick ?leave\b", r"\bmedical leave\b",
        r"\bmiscarriage\b", r"\bpregnan(?:t|cy)\b", r"\bfertility\b",
        r"\bmental health\b", r"\bADHD\b", r"\bautis(?:m|tic)\b",
    ],
    "family-situation": [
        r"\bdivorc(?:e|ed|ing)\b", r"\bseparation from (?:her|his|their) (?:husband|wife|partner)\b",
        r"\bcustody\b", r"\bbereave(?:d|ment)\b", r"\bfuneral\b",
        r"\bdying\b", r"\bdeath of (?:her|his|their)\b", r"\bcaring for (?:her|his|their)\b",
        r"\bcaregiver\b", r"\belderly (?:mother|father|parent)\b",
    ],
    "finances": [
        r"\bsalary\b", r"\bdebt\b", r"\bbankrupt(?:cy)?\b", r"\bevict(?:ed|ion)\b",
        r"\bmortgage\b", r"\bloan\b", r"\bcredit score\b", r"\bfinancially\b",
        r"\bcould ?n.t afford\b", r"\bmoney problems\b", r"\bbroke\b",
    ],
    "religion-politics": [
        r"\bcatholic\b", r"\bmuslim\b", r"\bjewish\b", r"\bhindu\b", r"\bbuddhist\b",
        r"\bchurch\b", r"\bmosque\b", r"\bsynagogue\b", r"\bpray(?:s|ing|er)\b",
        r"\bvoted\b", r"\bpolitical(?:ly)?\b", r"\bleft-wing\b", r"\bright-wing\b",
        r"\bactivis(?:m|t)\b",
    ],
    "trade-union": [r"\bunion (?:member|membership|rep)\b", r"\bunionis(?:ed|ing)\b"],
    "ethnicity-origin": [
        r"\bimmigration status\b", r"\bvisa (?:problem|issue|status)\b",
        r"\bresidency permit\b", r"\bethnic(?:ity)?\b", r"\bracial\b",
    ],
    "sex-life-orientation": [
        r"\bsexual orientation\b", r"\bgay\b", r"\blesbian\b", r"\bbisexual\b",
        r"\btransgender\b", r"\baffair\b",
    ],
}

COMPILED = {
    cat: [re.compile(p, re.IGNORECASE) for p in pats]
    for cat, pats in CATEGORIES.items()
}


def scan_text(text: str, label: str) -> list[dict]:
    hits: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for cat, pats in COMPILED.items():
            for pat in pats:
                for m in pat.finditer(line):
                    snippet = line.strip()
                    if len(snippet) > 160:
                        s = max(0, m.start() - 60)
                        snippet = "..." + line[s:m.end() + 60].strip() + "..."
                    hits.append(
                        {
                            "file": label,
                            "line": lineno,
                            "category": cat,
                            "match": m.group(0),
                            "context": snippet,
                        }
                    )
    return hits


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("files", nargs="+", help="files to scan; '-' for stdin")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true", help="print only the verdict line")
    args = p.parse_args()

    hits: list[dict] = []
    for f in args.files:
        if f == "-":
            hits += scan_text(sys.stdin.read(), "<stdin>")
        else:
            path = Path(f)
            if not path.exists():
                sys.exit(f"no such file: {f}")
            hits += scan_text(path.read_text(errors="replace"), str(path))

    if args.json:
        print(json.dumps({"hits": hits, "clean": not hits}, indent=2))
        return 1 if hits else 0

    if not hits:
        print("SENSITIVE SCAN CLEAN: no Art. 9 special-category candidates found.")
        return 0

    by_cat: dict[str, int] = {}
    for h in hits:
        by_cat[h["category"]] = by_cat.get(h["category"], 0) + 1
    print(
        f"SENSITIVE SCAN BLOCKED: {len(hits)} candidate(s) across "
        f"{len(by_cat)} category(ies).",
        file=sys.stderr,
    )
    if not args.quiet:
        for h in hits:
            print(
                f"  {h['file']}:{h['line']} [{h['category']}] {h['match']!r}\n"
                f"      {h['context']}",
                file=sys.stderr,
            )
    print(
        "\nEach hit is a candidate, not a verdict. Clear every one by rewording to "
        "record the EFFECT rather than the cause, or by confirming in the run "
        "summary that the match is a false positive and why. Do not publish with "
        "an uncleared hit.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
