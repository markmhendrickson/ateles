#!/usr/bin/env python3
"""Deliverable gate for the `evaluate-person` skill (step 5.1a / 5.1b).

The template's six sections are all RETROSPECTIVE -- what happened, and how well
we know it. Nothing says what to DO with it. The motivating use case was being
asked to give a colleague's 90-day review: the evaluation answers "what is true"
and leaves "what do I actually say" to be derived by hand, which is the manual
step the workflow exists to remove.

So two derived sections sit immediately after Key takeaways:

  **Feedback to deliver**   the small number of things that must actually be
                            said to the subject.
  **Discussion topics**     genuinely open questions -- NOT feedback, and never
                            rendered as such.

The separation is the whole point, and it is the reason for this gate. Collapsed,
they produce the exact failure the skill was built to prevent: open questions
delivered as criticisms.

    feedback:         "You've been ignoring the scoring model."
    discussion topic: "Our scoring penalises the contacts you're strongest in --
                       what should we do about that?"

Same fact. Different act. One is a judgement about conduct; the other is a
question about a system. A future run will be tempted to merge them, because
both are "things to raise in the meeting".

What this gate enforces
-----------------------
1. **No feedback item citing a finding that failed the SKILL.md 4 attribution
   checks.** Nothing gets delivered that the evidence layer would not support.
   Read from the attribution ledger: a citation whose final reading is
   `ambiguous`, or whose obligation direction is `unresolved`, cannot carry an
   item. (An attribution ledger that does not itself pass `check` fails here
   too -- these sections are the last place to be trusting an unchecked ledger.)
2. **No shortfall feedback item lacking a counterpart account.** Per D14, an
   item whose finding is a shortfall must carry, in the same breath, what the
   subject was OWED -- so it lands as a shared problem rather than pure fault.
   The account is not re-entered here: it is READ from `reciprocity_check.py`'s
   ledger by expectation id, so one table serves both gates and they cannot
   drift.
3. **No discussion topic phrased as a criticism.** A topic that is really a
   verdict about the subject's conduct is feedback wearing a question mark.
4. **Both sections carry the interrogation state.** These are inferences built
   on findings that have not yet been challenged (SKILL.md 6), so they are the
   most likely thing on the page to be wrong. Render them with a VISIBLE "not
   yet interrogated" marker rather than omitting them: a draft stays usable, and
   nobody mistakes an unchallenged recommendation for a settled one.

VOICE (operator decision -- do not deviate). Feedback items are NEUTRAL POINTS
THE OPERATOR WILL PHRASE THEMSELVES. Not scripted sentences, not quotable lines,
not the operator's voice. A performance review is spoken aloud; pre-written
phrasing would either sound unlike them or be discarded. Each item is the
substance to convey plus what it rests on:

    good:  "Four items she logged as two weeks late and blocking you, closed
            since / not closed"
    bad:   "I want to talk about some things that slipped."

Usage
-----
    deliverable_check.py feedback --path D --id FB1 \\
        --substance "four items she logged as two weeks late and blocking you, \\
                     closed since / not closed" \\
        --rests-on "todo.md line 12; her status note of 2026-03-06" \\
        --expectation E8 --finding-anchor f8 --shortfall \\
        --actionable-by-subject "re-baseline the list weekly" \\
        --citations "todo.md line 12"

    deliverable_check.py topic --path D --id DT1 \\
        --question "our scoring penalises the contacts you're strongest in -- \\
                    what should we do about that?" \\
        --kind scoring-disagreement --finding-anchor f4 \\
        --why-open "two readings both fit the evidence"

    deliverable_check.py interrogation --path D --state not-yet-interrogated
    deliverable_check.py check  --path D [--attribution A] [--reciprocity R]
    deliverable_check.py render --path D [--format html|markdown]

Exit codes: 0 pass, 1 gate failed, 2 usage error.

Stdlib only.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Why a question is genuinely open. These are the three sources SKILL.md 5.1b
# recognises; anything else is a finding in disguise.
TOPIC_KINDS = (
    # the evaluation reached `insufficient evidence`
    "insufficient-evidence",
    # two readings both fit the evidence
    "two-readings",
    # a decision is owed TO the subject rather than by them
    "decision-owed-to-subject",
    # the system/standard itself is in question rather than the subject's conduct
    "scoring-disagreement",
)

# The interrogation states. `not-yet-interrogated` is the DEFAULT and must be
# rendered visibly; it is not an error condition.
INTERROGATION_STATES = ("not-yet-interrogated", "interrogated")

# Readings/directions from attribution_check.py that cannot carry a delivered
# item. Kept as literals rather than imported: these scripts are invoked as
# subprocesses from the skill and must stay independently runnable.
UNUSABLE_READINGS = ("ambiguous",)
UNUSABLE_DIRECTIONS = ("unresolved",)

ACCOUNTABLE_VERDICTS = ("not met", "partially met")

# --- the criticism detector (guard 3) -------------------------------------
#
# A discussion topic is a question about a system, a gap, or a decision. The
# patterns below are second-person judgements about conduct -- the shape of
# "you've been ignoring the scoring model" with a question mark stapled on.
#
# Deliberately NARROW. A broad detector would fire on legitimate topics that
# merely mention the subject ("you said X in March -- was that still the plan?"),
# and a gate that blocks legitimate runs gets switched off, which is worse than
# no gate. So it matches second-person + a fault/shortfall verb, not mere
# second-person address.
CRITICISM_PATTERNS = (
    (r"\byou(?:'ve| have| had)?\s+(?:been\s+)?"
     r"(?:ignor|fail|neglect|miss|drop|overlook|disregard|skip)\w*",
     "a second-person shortfall claim"),
    (r"\byou\s+(?:never|always|didn't|did not|don't|do not|aren't|are not|"
     r"weren't|were not|haven't|have not)\b",
     "a second-person absolute or negation about conduct"),
    (r"\bwhy\s+(?:did|didn't|did not|haven't|have not|do|don't|do not|"
     r"are|aren't|weren't)\s+you\b",
     "a 'why did you' question, which is an accusation in interrogative form"),
    (r"\byour\s+(?:failure|refusal|inability|unwillingness|negligence|"
     r"lack of\b)",
     "a noun phrase attributing a shortfall to the subject"),
    (r"\bshould(?:n't| not)?\s+you\s+have\b",
     "a 'should you have' construction, which asserts the shortfall it asks about"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict:
    if not path.exists():
        return {
            "created_at": _now(),
            "feedback": [],
            "topics": [],
            "interrogation_state": "not-yet-interrogated",
        }
    data = json.loads(path.read_text())
    data.setdefault("feedback", [])
    data.setdefault("topics", [])
    # NOT setdefault. A missing interrogation state must reach the gate as a
    # missing state, because the whole point of 5.1c is that the state is
    # EXPLICIT and VISIBLE -- defaulting it here would have `check` pass a
    # ledger that render() then marks from a value nobody set. Fail closed on
    # the field that carries the safety meaning.
    if "interrogation_state" not in data:
        data["interrogation_state"] = None
    return data


def _save(path: Path, data: dict) -> None:
    data["updated_at"] = _now()
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _load_sidecar(path_str: str | None) -> dict | None:
    """Load an attribution or reciprocity ledger, or None when not supplied."""
    if not path_str:
        return None
    p = Path(path_str)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


# --------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------


def cmd_feedback(args: argparse.Namespace) -> int:
    path = Path(args.path)
    data = _load(path)
    cits = [c.strip() for c in (args.citations or "").split(";") if c.strip()]
    data["feedback"].append(
        {
            "id": args.id,
            "substance": args.substance,
            "rests_on": args.rests_on,
            "expectation_id": args.expectation,
            "finding_anchor": args.finding_anchor,
            "shortfall": bool(args.shortfall),
            "actionable_by_subject": args.actionable_by_subject,
            "citations": cits,
            "recorded_at": _now(),
        }
    )
    _save(path, data)
    print(f"feedback {args.id}: {args.substance[:60]!r}"
          + (" [shortfall]" if args.shortfall else ""))
    return 0


def cmd_topic(args: argparse.Namespace) -> int:
    path = Path(args.path)
    data = _load(path)
    data["topics"].append(
        {
            "id": args.id,
            "question": args.question,
            "kind": args.kind,
            "finding_anchor": args.finding_anchor,
            "why_open": args.why_open,
            "recorded_at": _now(),
        }
    )
    _save(path, data)
    print(f"topic {args.id} [{args.kind}]: {args.question[:60]!r}")
    return 0


def cmd_interrogation(args: argparse.Namespace) -> int:
    path = Path(args.path)
    data = _load(path)
    data["interrogation_state"] = args.state
    _save(path, data)
    print(f"interrogation state: {args.state}")
    return 0


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


def _criticism_hits(text: str) -> list[str]:
    low = " ".join((text or "").split()).lower()
    return [why for pat, why in CRITICISM_PATTERNS if re.search(pat, low)]


def _attribution_index(attr: dict) -> dict[str, dict]:
    return {c.get("citation"): c for c in attr.get("citations", [])}


def _failures(data: dict, attr: dict | None, recip: dict | None) -> list[str]:
    out: list[str] = []
    fb = data["feedback"]
    topics = data["topics"]

    # --- guard 4: the interrogation state must be recorded and renderable ---
    state = data.get("interrogation_state")
    if state not in INTERROGATION_STATES:
        out.append(
            f"interrogation state {state!r} is not one of {INTERROGATION_STATES} -- "
            "these two sections are inferences on findings that step 6 has not yet "
            "challenged, so their state must be explicit and visible on the page. "
            "Omitting them is not the remedy; an unmarked recommendation read as "
            "settled is (SKILL.md 5.1a/6)"
        )

    if not fb and not topics:
        out.append(
            "neither feedback items nor discussion topics recorded -- the "
            "evaluation answers 'what is true' and leaves 'what do I actually "
            "say' to be derived by hand, which is the manual step this workflow "
            "exists to remove (SKILL.md 5.1a). If there is genuinely nothing to "
            "deliver and nothing open, record that explicitly rather than "
            "leaving both empty"
        )

    seen_ids: set[str] = set()
    for item in fb + topics:
        iid = item.get("id")
        if not iid:
            out.append("an item has no id -- each must be addressable for the "
                       "page anchor")
        elif iid in seen_ids:
            out.append(f"duplicate item id {iid!r}")
        else:
            seen_ids.add(iid)

    index = _attribution_index(attr) if attr is not None else {}

    # --- guards 1 and 2, per feedback item ---
    for f in fb:
        fid = f.get("id")
        if not f.get("substance"):
            out.append(f"{fid}: no substance recorded")
        if not f.get("rests_on"):
            out.append(
                f"{fid}: no 'rests on' recorded -- an item is the substance to "
                "convey PLUS what it rests on. The operator phrases it themselves "
                "and cannot do that without knowing what the point is grounded in"
            )
        if not f.get("finding_anchor"):
            out.append(
                f"{fid}: no finding anchor -- these are the most DERIVED content on "
                "the page (inferences on inferences), so every item must link down "
                "to the finding it comes from. Short-then-long still holds: these "
                "are short pointers into the long evidence, not standalone "
                "assertions (SKILL.md 5.1a)"
            )
        if not f.get("actionable_by_subject"):
            out.append(
                f"{fid}: no statement of what the subject can act on -- feedback is "
                "ordered by what the subject can act on, not by severity, and an "
                "item nobody can act on is a finding rather than feedback"
            )

        # A feedback item written as a scripted line rather than as substance.
        if _looks_scripted(f.get("substance") or ""):
            out.append(
                f"{fid}: substance reads as a scripted sentence in the operator's "
                "voice rather than as the point to convey. A performance review is "
                "spoken aloud; pre-written phrasing either sounds unlike them or "
                "gets discarded. Write what must land plus what it rests on -- "
                "'four items she logged as two weeks late and blocking you, closed "
                "since / not closed' -- not 'I want to talk about some things that "
                "slipped'"
            )

        # --- guard 1: attribution must have survived SKILL.md 4 ---
        for c in f.get("citations") or []:
            if attr is None:
                continue
            row = index.get(c)
            if row is None:
                out.append(
                    f"{fid}: cites {c!r}, which has no row in the attribution "
                    "ledger. An item may only cite a finding that survived the "
                    "SKILL.md 4 attribution checks -- nothing gets delivered that "
                    "the evidence layer would not support"
                )
                continue
            if row.get("final_reading") in UNUSABLE_READINGS:
                out.append(
                    f"{fid}: cites {c!r}, whose final reading is "
                    f"{row.get('final_reading')!r}. An ambiguous citation may not "
                    "support a finding (SKILL.md 4.2 q4), so it certainly may not "
                    "be said to the subject"
                )
            if row.get("obligation_direction") in UNUSABLE_DIRECTIONS:
                out.append(
                    f"{fid}: cites {c!r}, whose obligation direction is unresolved "
                    "-- you do not yet know whether this runs to or from the "
                    "subject, which is precisely the misreading that motivated "
                    "SKILL.md 4"
                )
            if row.get("genre") in ("relayed-machine-output", "record-of-others"):
                out.append(
                    f"{fid}: cites {c!r}, genre {row.get('genre')!r} -- she sent it, "
                    "she did not author it. It is not her reasoning and not her "
                    "commitment, so it cannot be delivered to her as feedback"
                )

        # --- guard 2: shortfall items carry the counterpart account ---
        if f.get("shortfall"):
            eid = f.get("expectation_id")
            if not eid:
                out.append(
                    f"{fid}: marked a shortfall with no expectation id -- the "
                    "counterpart account is read from the reciprocity ledger by "
                    "expectation, so the item cannot be checked without it"
                )
            elif recip is not None:
                out.extend(_counterpart_failures(fid, eid, recip))

    # --- guard 3: a discussion topic may not be a criticism ---
    for t in topics:
        tid = t.get("id")
        q = t.get("question") or ""
        if not q:
            out.append(f"{tid}: no question recorded")
        if t.get("kind") not in TOPIC_KINDS:
            out.append(
                f"{tid}: kind {t.get('kind')!r} is not one of {TOPIC_KINDS} -- a "
                "discussion topic is open because the evaluation reached "
                "insufficient evidence, because two readings both fit, or because "
                "a decision is owed TO the subject. Anything else is a finding "
                "wearing a question mark"
            )
        if not t.get("why_open"):
            out.append(f"{tid}: does not say why the question is open")
        if not t.get("finding_anchor"):
            out.append(
                f"{tid}: no finding anchor -- a topic is a short pointer into the "
                "long evidence, not a standalone assertion (SKILL.md 5.1a)"
            )
        hits = _criticism_hits(q)
        if hits:
            out.append(
                f"{tid}: phrased as a criticism, not a question ({hits[0]}). "
                "Collapsing feedback and discussion topics produces the exact "
                "failure this skill was built to prevent -- open questions "
                "delivered as criticisms. \"You've been ignoring the scoring "
                "model\" is FEEDBACK; \"our scoring penalises the contacts you're "
                "strongest in -- what should we do about that\" is a DISCUSSION "
                "TOPIC. Same fact, different act. Either re-phrase it as a "
                "question about the system, or move it to Feedback to deliver "
                "where it will be gated on its evidence"
            )
        elif not q.strip().endswith("?"):
            out.append(
                f"{tid}: not phrased as a question -- a discussion topic is a "
                "genuinely open question, and a statement in this section reads as "
                "a verdict the subject is expected to accept"
            )
    return out


def _counterpart_failures(fid: str, eid: str, recip: dict) -> list[str]:
    """Read the counterpart account from reciprocity_check.py's own ledger.

    Deliberately NOT a parallel field: D14's machinery already records what the
    subject was owed, per expectation window, and re-entering it here would let
    the two drift -- which is the failure mode CLAUDE.md names as "a comment
    claiming two constants match is not a mechanism that keeps them matching".
    """
    out: list[str] = []
    findings = [
        r for r in recip.get("findings", [])
        if r.get("expectation_id") == eid
        and (r.get("verdict") or "").strip().lower() in ACCOUNTABLE_VERDICTS
    ]
    if not findings:
        out.append(
            f"{fid}: shortfall feedback on {eid} with no shortfall finding recorded "
            "for that expectation in the reciprocity ledger -- feedback may only "
            "cite a finding that exists and survived its own gates"
        )
        return out

    for r in findings:
        cited = r.get("counterpart_items") or []
        nothing = r.get("nothing_owed")
        if not cited and not nothing:
            out.append(
                f"{fid}: shortfall on {eid} carries no counterpart account. Per D14 "
                "every item whose finding is a shortfall must say, in the same "
                "breath, what the subject was OWED -- so it lands as a shared "
                "problem rather than as pure fault. Record it with "
                "`reciprocity_check.py owed` / `finding`, or say what was swept and "
                "found nothing (SKILL.md 1.3.1 / 3.3)"
            )
    return out


# A scripted line is first-person and about the ACT of speaking, rather than
# about the substance. Narrow on purpose -- see CRITICISM_PATTERNS.
SCRIPTED_PATTERNS = (
    r"^\s*(?:i|we)\s+(?:want|wanted|need|needed|would like|'d like|think|feel)\b",
    r"^\s*(?:let's|lets|let us)\b",
    r"\bi\s+(?:want|need)\s+to\s+(?:talk|discuss|raise|flag|go over)\b",
    r"^\s*(?:thanks|thank you|first off|look|listen|so,)\b",
)


def _looks_scripted(text: str) -> bool:
    low = " ".join((text or "").split()).lower()
    return any(re.search(p, low) for p in SCRIPTED_PATTERNS)


def cmd_check(args: argparse.Namespace) -> int:
    data = _load(Path(args.path))
    attr = _load_sidecar(args.attribution)
    recip = _load_sidecar(args.reciprocity)
    problems = _failures(data, attr, recip)
    if problems:
        print(f"DELIVERABLE GATE FAILED ({len(problems)} problem(s)):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\nDo not render. Feedback is what must be SAID; discussion topics are "
            "what is genuinely OPEN. Keeping them apart is the point.",
            file=sys.stderr,
        )
        return 1
    print(
        f"DELIVERABLE OK: {len(data['feedback'])} feedback item(s), "
        f"{len(data['topics'])} discussion topic(s); "
        f"interrogation state {data['interrogation_state']}."
    )
    if data["interrogation_state"] == "not-yet-interrogated":
        print(
            "NOTE: both sections must render with a visible 'not yet interrogated' "
            "marker -- they are inferences on findings step 6 has not challenged."
        )
    return 0


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

BANNER_TEXT = (
    "Not yet interrogated. These two sections are derived from the findings "
    "below and have not been challenged by the step-6 interrogation loop, which "
    "is where an unchallenged inference usually breaks. Treat them as a draft of "
    "what to say, not as settled."
)


def _banner_html(state: str) -> str:
    if state == "interrogated":
        return ('<p class="interrogation ok">Interrogated. Every item below survived '
                "the step-6 challenge loop; see Revisions.</p>")
    return f'<p class="interrogation draft">{html.escape(BANNER_TEXT)}</p>'


def cmd_render(args: argparse.Namespace) -> int:
    data = _load(Path(args.path))
    state = data["interrogation_state"]
    fb, topics = data["feedback"], data["topics"]
    if args.format == "html":
        print('<section id="feedback-to-deliver">')
        print("<h2>Feedback to deliver</h2>")
        print(_banner_html(state))
        print("<ol>")
        for f in fb:
            anchor = f.get("finding_anchor") or ""
            link = (f' <a href="#{html.escape(anchor)}">Evidence</a>' if anchor else "")
            rests = f.get("rests_on") or ""
            print(f'<li id="{html.escape(str(f.get("id")))}">'
                  f'{html.escape(f.get("substance") or "")}'
                  + (f' <span class="rests">Rests on: {html.escape(rests)}.</span>'
                     if rests else "")
                  + link + "</li>")
        print("</ol>")
        print("</section>")
        print('<section id="discussion-topics">')
        print("<h2>Discussion topics</h2>")
        print('<p class="meta">Open questions, not feedback. Each is open because '
              "the evidence does not settle it or because the decision is not the "
              "subject's to make.</p>")
        print(_banner_html(state))
        print("<ol>")
        for t in topics:
            anchor = t.get("finding_anchor") or ""
            link = (f' <a href="#{html.escape(anchor)}">Evidence</a>' if anchor else "")
            why = t.get("why_open") or ""
            print(f'<li id="{html.escape(str(t.get("id")))}">'
                  f'{html.escape(t.get("question") or "")}'
                  + (f' <span class="rests">Open because: {html.escape(why)}.</span>'
                     if why else "")
                  + link + "</li>")
        print("</ol>")
        print("</section>")
    else:
        print("## Feedback to deliver\n")
        print(f"> {BANNER_TEXT}\n" if state != "interrogated"
              else "> Interrogated: every item survived the step-6 challenge loop.\n")
        for f in fb:
            print(f"- **{f.get('id')}** {f.get('substance')} "
                  f"(rests on: {f.get('rests_on')}; see {f.get('finding_anchor')})")
        print("\n## Discussion topics\n")
        print("Open questions, not feedback.\n")
        for t in topics:
            print(f"- **{t.get('id')}** {t.get('question')} "
                  f"(open because: {t.get('why_open')}; see {t.get('finding_anchor')})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("feedback", help="record something that must be SAID")
    f.add_argument("--path", required=True)
    f.add_argument("--id", required=True)
    f.add_argument("--substance", required=True,
                   help="the point to convey -- NOT a scripted sentence")
    f.add_argument("--rests-on", required=True,
                   help="what the point rests on, so the operator can phrase it")
    f.add_argument("--expectation", default=None,
                   help="the expectation row this derives from (required on a shortfall)")
    f.add_argument("--finding-anchor", default=None,
                   help="the in-page anchor of the finding this links down to")
    f.add_argument("--shortfall", action="store_true",
                   help="set when the underlying finding is `not met` or `partially met`")
    f.add_argument("--actionable-by-subject", default=None,
                   help="what the subject can actually do about it")
    f.add_argument("--citations", default=None,
                   help="semicolon-separated citations, matched against the attribution ledger")
    f.set_defaults(func=cmd_feedback)

    t = sub.add_parser("topic", help="record a genuinely OPEN question")
    t.add_argument("--path", required=True)
    t.add_argument("--id", required=True)
    t.add_argument("--question", required=True)
    t.add_argument("--kind", required=True, choices=TOPIC_KINDS)
    t.add_argument("--finding-anchor", default=None)
    t.add_argument("--why-open", default=None)
    t.set_defaults(func=cmd_topic)

    i = sub.add_parser("interrogation", help="set the step-6 interrogation state")
    i.add_argument("--path", required=True)
    i.add_argument("--state", required=True, choices=INTERROGATION_STATES)
    i.set_defaults(func=cmd_interrogation)

    c = sub.add_parser("check")
    c.add_argument("--path", required=True)
    c.add_argument("--attribution", default=None,
                   help="attribution_check.py ledger, for the survived-attribution gate")
    c.add_argument("--reciprocity", default=None,
                   help="reciprocity_check.py ledger, for the counterpart-account gate")
    c.set_defaults(func=cmd_check)

    d = sub.add_parser("render")
    d.add_argument("--path", required=True)
    d.add_argument("--format", choices=("html", "markdown"), default="markdown")
    d.set_defaults(func=cmd_render)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
