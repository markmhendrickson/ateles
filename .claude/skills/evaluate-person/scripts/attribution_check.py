#!/usr/bin/env python3
"""Misreading check for the `evaluate-person` skill (step 4).

For every citation drawn from material the SUBJECT wrote, this records the
re-derived attribution -- author, genre, obligation direction, and the opposite
reading -- and then looks for systematic bias across the whole set.

Motivating failure: in one ad-hoc review, five separate misattributions all ran
the same direction. The subject's own to-do list ("Mark, four items, two weeks
late, all blocking him") was read as a complaint about the operator when it was
her own accounting of what she owed him. Each misreading was individually
plausible; only the *pattern* revealed the bias, and only because the operator
happened to know the documents.

Hence two layers:

  1. Per-citation re-derivation (the four questions of SKILL.md 4.2). A citation
     whose obligation direction cannot be resolved is `ambiguous` and may not
     support a finding.
  2. The direction tripwire (SKILL.md 4.3). If corrections cluster in one
     direction -- >= 3 one way with none the other -- this exits non-zero. A
     biased reader passes each individual check the same wrong way; only the
     count catches it.

Genres and their DEFAULT obligation reading (the heart of the check):

    self-accounting       a task list / status note / personal log the subject
                          keeps about their OWN commitments. A name beside an
                          item defaults to WHO IT IS OWED TO, not who is at
                          fault. This is the genre that was misread five times.
    to-operator           a message the subject sent the operator.
    to-third-party        a message to someone else.
    record-of-others      minutes, transcripts, forwarded text -- the words are
                          a HUMAN third party's, not necessarily the subject's.
    relayed-machine-output
                          the subject SENT it but did not AUTHOR it: agent or
                          tool output pasted into her channel, a model's
                          diagnosis read aloud on a call, a generated report
                          forwarded without comment. Metadata says the subject;
                          the reasoning is not hers. See below.

D5, dogfood run 2026-09-22. SKILL.md 4.2 q1 says confirm authorship "from the
artifact's own metadata -- sender field, file owner, message author -- not
inferred from content". Followed literally that gives the WRONG answer for a
real Slack message whose author field said the subject and whose text was
verbatim output from her coding agent, which she flagged herself one message
later. The transcript sweep hit the same trap independently: a line labelled
with the subject's name that was machine output read aloud while screen-sharing.

Metadata establishes who SENT an artifact. It does not establish who AUTHORED
it. Relayed machine output must never be attributed to the subject as her own
reasoning or her own commitment -- crediting her with analysis she did not
perform is a misattribution in exactly the same way that reading her to-do list
as a complaint was, and in a corpus where the operator is actively encouraging
her to delegate to agents it will recur constantly. So this genre may not carry
`subject-at-fault` or a subject-owed obligation direction on the strength of the
relayed text, and it must name the evidence that the text is machine-authored.

SOURCING is a SEPARATE axis from genre (D8), and the two are routinely confused
because a reliable-looking format launders an unreliable claim:

    first-hand-documented   the artifact IS the fact -- a signed PDF, a commit,
                            a calendar entry, a transaction record.
    corroborated            a report, independently confirmed elsewhere. Name
                            the corroborating artifact.
    single-verbal-report    someone said it and it is written down. The WRITING
                            is reliable; the FACT rests on one person's say-so.
    unsourced               no traceable origin.

Live case: "PROPOSAL AND NDA SIGNED" sits in a written Google Sheet, so the ASR
hazard ("NDA" mistranscribed from another word) does not apply and all four of
SKILL.md 4.2's questions pass. It is still an uncorroborated verbal report, and
the source record itself grades it that way ("not corroborated by a document on
the instance"). A citation can pass every authorship check and still be one
person's unrecorded say-so presented in a high-trust format. Genre reliability
is about WHAT WORD WAS USED. Sourcing reliability is about WHETHER THE UNDERLYING
FACT IS DOCUMENTED. A finding resting on a single verbal report must say so.

Usage
-----
    attribution_check.py add --path A --citation "todo.md line 12" \\
        --author-confirmed-by "file owner metadata" \\
        --genre self-accounting \\
        --obligation-direction subject-owes-operator \\
        --convention "other items in the list name the person owed" \\
        --opposite-reading "a complaint that Mark is late" \\
        --distinguisher "her weekly status note of 2026-03-06 restates them as hers" \\
        --sourcing first-hand-documented \\
        --initial-reading subject-at-fault --final-reading operator-at-fault \\
        [--resolved-by-challenge]
    attribution_check.py add --path A --citation "slack D0BE 2026-09-11 11:55" \\
        --author-confirmed-by "slack message author field" \\
        --genre relayed-machine-output --authored-by "her coding agent" \\
        --relay-evidence "she flags it one message later: 'It had said this earlier'" \\
        --obligation-direction unresolved --sourcing unsourced \\
        --opposite-reading "her own technical diagnosis" \\
        --distinguisher "the agent transcript she pasted from" \\
        --initial-reading subject-at-fault --final-reading ambiguous
    attribution_check.py check  --path A
    attribution_check.py render --path A [--format html|markdown]

Exit codes: 0 pass, 1 unresolved ambiguity or tripped bias detector, 2 usage.

Stdlib only.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

GENRES = (
    "self-accounting",
    "to-operator",
    "to-third-party",
    "record-of-others",
    # D5: the subject SENT it, an agent or tool AUTHORED it. Metadata says her;
    # the reasoning is not hers. Must not be attributed to her as her own.
    "relayed-machine-output",
)

# Genres whose text is not the subject's own reasoning or commitment.
NOT_SUBJECT_AUTHORED = ("record-of-others", "relayed-machine-output")

# D8: how well SOURCED the underlying fact is -- orthogonal to how reliable the
# artifact's FORMAT is. A written sheet is a reliable record of a verbal claim.
SOURCING = (
    "first-hand-documented",
    "corroborated",
    "single-verbal-report",
    "unsourced",
)

# Sourcing grades that cannot silently carry a finding.
WEAK_SOURCING = ("single-verbal-report", "unsourced")

# How a finding attributes fault. The tripwire counts movement between these.
READINGS = ("subject-at-fault", "operator-at-fault", "no-fault", "ambiguous")

DIRECTIONS = (
    "subject-owes-operator",
    "operator-owes-subject",
    "subject-owes-third-party",
    "third-party-owes-subject",
    "unresolved",
)

# Minimum one-directional corrections before the tripwire fires (SKILL.md 4.3).
TRIPWIRE_THRESHOLD = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict:
    if not path.exists():
        return {"created_at": _now(), "citations": []}
    return json.loads(path.read_text())


def _save(path: Path, data: dict) -> None:
    data["updated_at"] = _now()
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def cmd_add(args: argparse.Namespace) -> int:
    path = Path(args.path)
    data = _load(path)
    data["citations"].append(
        {
            "citation": args.citation,
            "author_confirmed_by": args.author_confirmed_by,
            "genre": args.genre,
            "authored_by": args.authored_by,
            "relay_evidence": args.relay_evidence,
            "sourcing": args.sourcing,
            "corroborated_by": args.corroborated_by,
            "obligation_direction": args.obligation_direction,
            "convention": args.convention,
            "opposite_reading": args.opposite_reading,
            "distinguisher": args.distinguisher,
            "initial_reading": args.initial_reading,
            "final_reading": args.final_reading,
            "sourcing_disclosed": bool(args.sourcing_disclosed),
            "resolved_by_challenge": bool(args.resolved_by_challenge),
            "recorded_at": _now(),
        }
    )
    _save(path, data)
    moved = args.initial_reading != args.final_reading
    print(
        f"recorded {args.citation!r}: {args.genre} / {args.obligation_direction}"
        + (f"  [CORRECTED {args.initial_reading} -> {args.final_reading}]" if moved else "")
    )
    return 0


def _corrections(data: dict) -> Counter:
    """Count corrections by the direction they moved the reading."""
    c: Counter = Counter()
    for e in data["citations"]:
        a, b = e.get("initial_reading"), e.get("final_reading")
        if a and b and a != b:
            c[f"{a} -> {b}"] += 1
    return c


def _toward_subject_fault(label: str) -> bool:
    return label.endswith("-> subject-at-fault")


def _away_from_subject_fault(label: str) -> bool:
    return label.startswith("subject-at-fault ->")


def _failures(data: dict) -> list[str]:
    out: list[str] = []
    cits = data["citations"]
    if not cits:
        out.append("no citations recorded -- every subject-authored citation needs a row")
        return out

    for e in cits:
        c = e["citation"]
        if not e.get("author_confirmed_by"):
            out.append(f"{c}: author not confirmed from artifact metadata (4.2 q1)")
        if e.get("genre") not in GENRES:
            out.append(f"{c}: genre missing or unknown (4.2 q2)")

        # --- D5: sent-by is not authored-by ---
        if e.get("genre") == "relayed-machine-output":
            if not e.get("relay_evidence"):
                out.append(
                    f"{c}: genre 'relayed-machine-output' with no relay evidence -- "
                    "name what shows the text is machine-authored (the subject "
                    "flagging it, the tool's own formatting, the agent transcript). "
                    "Metadata says who SENT it and cannot say who AUTHORED it (4.2 q1)"
                )
            if e.get("final_reading") == "subject-at-fault":
                out.append(
                    f"{c}: relayed machine output read as the subject's fault -- "
                    "she sent it, an agent wrote it. It is not her reasoning and not "
                    "her commitment, so it cannot support a finding against her. "
                    "Downgrade to context, or cite what she wrote about it instead"
                )
            if e.get("obligation_direction") in (
                "subject-owes-operator", "subject-owes-third-party"
            ):
                out.append(
                    f"{c}: relayed machine output read as an obligation the subject "
                    "took on -- an agent's output in her channel is not her promise. "
                    "Cite her own words adopting it, or mark the direction unresolved"
                )

        # --- D8: genre reliability is not sourcing reliability ---
        sourcing = e.get("sourcing")
        if sourcing not in SOURCING:
            out.append(
                f"{c}: sourcing missing or unknown -- a citation's FORMAT being "
                f"reliable says nothing about whether the underlying FACT is "
                f"documented. Grade it one of {SOURCING}"
            )
        elif sourcing == "corroborated" and not e.get("corroborated_by"):
            out.append(
                f"{c}: graded 'corroborated' with nothing named as corroborating it "
                "-- name the independent artifact, or grade it single-verbal-report"
            )
        elif sourcing in WEAK_SOURCING and e.get("final_reading") != "ambiguous":
            if not e.get("sourcing_disclosed"):
                out.append(
                    f"{c}: rests on a {sourcing.replace('-', ' ')} but is not flagged "
                    "as such in the finding. A written artifact is a reliable record "
                    "of WHAT WAS SAID and no evidence at all that the thing said is "
                    "true ('PROPOSAL AND NDA SIGNED' in a sheet is one person's "
                    "say-so). Pass --sourcing-disclosed once the finding that uses "
                    "this citation states the limitation in its own text"
                )
        if e.get("obligation_direction") == "unresolved":
            if e.get("final_reading") != "ambiguous":
                out.append(
                    f"{c}: obligation direction unresolved but final reading is "
                    f"{e.get('final_reading')!r} -- an unresolved citation must be "
                    "marked 'ambiguous' and may not support a finding (4.2 q3)"
                )
        if not e.get("opposite_reading"):
            out.append(f"{c}: opposite reading not written out (4.2 q4)")
        if e.get("final_reading") == "ambiguous" and not e.get("distinguisher"):
            out.append(
                f"{c}: marked ambiguous with no distinguishing artifact named -- "
                "say what would settle it, or drop the citation"
            )
        if e.get("genre") == "self-accounting" and e.get("obligation_direction") in (
            "operator-owes-subject",
            "third-party-owes-subject",
        ):
            # Not an error -- a self-accounting CAN log what one is owed -- but it
            # is exactly the reading that went wrong five times, so it must carry
            # the document's own naming convention as justification.
            if not e.get("convention"):
                out.append(
                    f"{c}: a self-accounting read as someone else's obligation is the "
                    "precise failure this check exists for -- record the document's "
                    "own naming convention that justifies it (4.2 q3)"
                )

    # --- the direction tripwire (4.3) ---
    corr = _corrections(data)
    toward = sum(n for lbl, n in corr.items() if _toward_subject_fault(lbl))
    away = sum(n for lbl, n in corr.items() if _away_from_subject_fault(lbl))
    for name, hot, cold in (("toward", toward, away), ("away from", away, toward)):
        if hot >= TRIPWIRE_THRESHOLD and cold == 0:
            out.append(
                f"DIRECTION TRIPWIRE: {hot} correction(s) moved the reading {name} "
                "the subject's fault and none moved the other way. That asymmetry is "
                "the signature of systematic misreading, not of a one-off error. "
                "Re-read the UNCHANGED citations in this class with the opposite "
                "prior before rendering, and report the asymmetry to the operator "
                "regardless of what the re-read finds (SKILL.md 4.3)."
            )
    return out


def cmd_check(args: argparse.Namespace) -> int:
    data = _load(Path(args.path))
    problems = _failures(data)
    corr = _corrections(data)
    if problems:
        print(f"ATTRIBUTION CHECK FAILED ({len(problems)} problem(s)):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    n = len(data["citations"])
    print(f"ATTRIBUTION OK: {n} subject-authored citation(s) re-derived.")
    if corr:
        print("corrections by direction:")
        for lbl, cnt in sorted(corr.items()):
            print(f"  {lbl}: {cnt}")
    else:
        print("no corrections -- every initial reading survived re-derivation.")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    data = _load(Path(args.path))
    rows = data["citations"]
    cols = ("citation", "genre", "authored_by", "sourcing", "obligation_direction",
            "initial_reading", "final_reading", "distinguisher")
    heads = ("Citation", "Genre", "Authored by", "Sourcing", "Obligation runs",
             "Initial reading", "Final reading", "What would settle it")
    if args.format == "html":
        print('<table class="attribution"><thead><tr>'
              + "".join(f"<th>{h}</th>" for h in heads)
              + "</tr></thead><tbody>")
        for e in rows:
            tds = "".join(
                f"<td>{html.escape(str(e.get(c)))if e.get(c) is not None else '-'}</td>"
                for c in cols
            )
            print(f"<tr>{tds}</tr>")
        print("</tbody></table>")
        corr = _corrections(data)
        if corr:
            print("<p class=\"meta\">Corrections by direction: "
                  + html.escape(", ".join(f"{k} ({v})" for k, v in sorted(corr.items())))
                  + "</p>")
    else:
        print("| " + " | ".join(heads) + " |")
        print("|" + "---|" * len(heads))
        for e in rows:
            print("| " + " | ".join(str(e.get(c) or "-") for c in cols) + " |")
        corr = _corrections(data)
        if corr:
            print("\nCorrections by direction: "
                  + ", ".join(f"{k} ({v})" for k, v in sorted(corr.items())))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add")
    a.add_argument("--path", required=True)
    a.add_argument("--citation", required=True)
    a.add_argument("--author-confirmed-by", required=True,
                   help="the artifact metadata that proves authorship -- not an inference from content")
    a.add_argument("--genre", required=True, choices=GENRES)
    a.add_argument("--authored-by", default=None,
                   help="who actually wrote the text, when that differs from who sent it")
    a.add_argument("--relay-evidence", default=None,
                   help="what shows the text is machine-authored (required for relayed-machine-output)")
    a.add_argument("--sourcing", required=True, choices=SOURCING,
                   help="how well SOURCED the underlying fact is -- separate from the artifact's format")
    a.add_argument("--corroborated-by", default=None,
                   help="the independent artifact confirming it (required for --sourcing corroborated)")
    a.add_argument("--sourcing-disclosed", action="store_true",
                   help="set when the finding using this citation states its sourcing limitation in its own text")
    a.add_argument("--obligation-direction", required=True, choices=DIRECTIONS)
    a.add_argument("--convention", default=None,
                   help="the document's own naming convention, inferred from its other items")
    a.add_argument("--opposite-reading", required=True)
    a.add_argument("--distinguisher", default=None)
    a.add_argument("--initial-reading", required=True, choices=READINGS)
    a.add_argument("--final-reading", required=True, choices=READINGS)
    a.add_argument("--resolved-by-challenge", action="store_true",
                   help="set when this row changed because of operator pushback (step 6)")
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
