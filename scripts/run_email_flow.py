#!/usr/bin/env python3
"""
scripts/run_email_flow.py — Drive the full email-routing flow end-to-end.

Pipeline: Turdus (triage) → Anthus (dispatch) → Buteo (legal) → Pavo (PM)
       → Onychomys escalation note. Output is a single markdown artifact.

Usage:
    # From an exported Gmail thread JSON (FULL_CONTENT format)
    python scripts/run_email_flow.py --thread-json path/to/thread.json \\
        --output /tmp/email-flow-output.md

    # Or supply sender/subject/body directly
    python scripts/run_email_flow.py \\
        --sender 'ram.talwar@gmail.com' \\
        --subject 'Re: terms' \\
        --body-file /tmp/latest.txt \\
        --output /tmp/email-flow-output.md

In dry-run mode (default) nothing is sent and no Gmail draft is created.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.agents import buteo, dispatch, pavo, runner, triage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("run_email_flow")


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _load_thread(path: Path) -> tuple[str, str, str, str, str]:
    """
    Parse a Gmail thread JSON (FULL_CONTENT format) and return:
    (sender, subject, latest_body, thread_summary, prior_positions)
    """
    data = json.loads(path.read_text())
    messages = data.get("messages") or []
    if not messages:
        raise ValueError(f"No messages in thread JSON: {path}")

    latest = messages[-1]
    sender = latest.get("sender", "")
    subject = latest.get("subject", "")
    latest_body = latest.get("plaintextBody") or _strip_html(latest.get("htmlBody", ""))
    if not latest_body:
        latest_body = latest.get("snippet", "")

    operator_email = "markmhendrickson@gmail.com"
    prior_lines: list[str] = []
    operator_lines: list[str] = []
    for m in messages[:-1]:
        s = m.get("sender", "")
        d = m.get("date", "")
        snippet = (m.get("plaintextBody") or _strip_html(m.get("htmlBody", "")) or m.get("snippet", ""))[:600]
        line = f"[{d}] {s}: {snippet}"
        prior_lines.append(line)
        if operator_email in s.lower():
            operator_lines.append(line)

    thread_summary = "\n".join(prior_lines)
    prior_positions = "\n".join(operator_lines)
    return sender, subject, latest_body, thread_summary, prior_positions


def _counterparty_first_name(sender: str) -> str:
    if "<" in sender and ">" in sender:
        name_part = sender.split("<")[0].strip().strip('"')
        if name_part:
            return name_part.split()[0]
    local = sender.split("@")[0]
    local = re.sub(r"[._-]+", " ", local)
    return local.split()[0].capitalize() if local else ""


def _render_markdown(
    *,
    sender: str,
    subject: str,
    classification: triage.ClassificationResult,
    plan: dispatch.DispatchPlan,
    redline: buteo.RedlineReport | None,
    framing: pavo.CommercialFraming | None,
) -> str:
    parts: list[str] = []
    parts.append("# Email-routing dry-run artifact\n")
    parts.append(f"Generated: {datetime.now(UTC).isoformat()}\n")
    parts.append(f"Sender: `{sender}`\n")
    parts.append(f"Subject: `{subject}`\n")

    parts.append("\n## Turdus — classification\n")
    parts.append(f"- bucket: **{classification.bucket}**")
    parts.append(f"- target_agent: **{classification.target_agent or '(none)'}**")
    parts.append(f"- priority: **{classification.priority}**")
    parts.append(f"- requires_operator: **{classification.requires_operator}**")
    parts.append(f"- summary: {classification.summary}")
    parts.append(f"- rationale: {classification.rationale}")
    if classification.stub:
        parts.append("- *(stub — ANTHROPIC_API_KEY not set; keyword fallback used)*")

    parts.append("\n## Anthus — dispatch plan\n")
    parts.append(f"- chain: **{' → '.join(plan.chain) or '(none)'}**")
    parts.append(f"- operator sign-off required: **{plan.requires_operator_signoff}**")
    parts.append(f"- notify_handler: **{plan.notify_handler}**")

    parts.append("\n## Apis — task dispatch\n")
    parts.append(
        "- In production: Turdus creates `email_message` + `task` entities; "
        "Apis SSE handler resolves domain tag → skill via `_DOMAIN_ROUTES` "
        "(`legal → buteo`, `commercial → pavo`); subprocess-spawns the skill."
    )
    parts.append(
        f"- This dry-run: same `lib.agents.runner.dispatch()` invoked in-process "
        f"({len([a for a in plan.chain if a in ('buteo', 'pavo')])} skill(s) executed)."
    )

    if redline is not None:
        parts.append("\n## Buteo — legal review\n")
        if redline.stub:
            parts.append("*(stub — re-run with ANTHROPIC_API_KEY for full clause analysis)*\n")
        parts.append(f"**Headline risk:** {redline.headline_risk}\n")
        if redline.alignment_summary:
            parts.append(f"**Alignment:** {redline.alignment_summary}\n")
        if redline.clause_review:
            parts.append("### Clause-by-clause\n")
            for c in redline.clause_review:
                parts.append(
                    f"\n#### {c.clause}  ·  risk: {c.risk_level}  ·  rec: {c.recommendation}\n"
                )
                parts.append(f"- **Counterparty:** {c.counterparty_position}")
                parts.append(f"- **Our position:** {c.our_position}")
                parts.append(f"- **Suggested redline:**\n\n  > {c.suggested_redline}\n")
                parts.append(f"- **Rationale:** {c.rationale}")
        if redline.open_questions_for_operator:
            parts.append("\n### Open questions for operator\n")
            for q in redline.open_questions_for_operator:
                parts.append(f"- {q}")
        if redline.next_steps:
            parts.append("\n### Next steps\n")
            for s in redline.next_steps:
                parts.append(f"- {s}")

    if framing is not None:
        parts.append("\n## Pavo — commercial framing & reply draft\n")
        if framing.stub:
            parts.append("*(stub — re-run with ANTHROPIC_API_KEY for tone-matched draft)*\n")
        parts.append(f"**Tone read:** {framing.tone_read}\n")
        if framing.key_concessions_we_can_offer:
            parts.append("**Key concessions we can offer:**")
            for c in framing.key_concessions_we_can_offer:
                parts.append(f"- {c}")
        if framing.non_negotiables:
            parts.append("\n**Non-negotiables:**")
            for n in framing.non_negotiables:
                parts.append(f"- {n}")
        parts.append("\n### Draft reply (NOT SENT)\n")
        parts.append(f"**Subject:** {framing.reply_draft.subject}\n")
        parts.append("```")
        parts.append(framing.reply_draft.body_markdown)
        parts.append("```")
        parts.append(f"\n**send_recommendation:** `{framing.send_recommendation}`\n")
        parts.append(f"\n## Onychomys — escalation note for operator\n\n> {framing.escalation_note}\n")

    parts.append("\n---\n")
    parts.append(
        "_Generated by `scripts/run_email_flow.py`. Dry-run only — no Gmail draft created, no message sent._"
    )
    return "\n".join(parts) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--thread-json", type=Path, help="Gmail thread JSON (FULL_CONTENT)")
    ap.add_argument("--sender")
    ap.add_argument("--subject")
    ap.add_argument("--body-file", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--json-output", type=Path, help="Optional structured JSON dump")
    args = ap.parse_args(argv)

    if args.thread_json:
        sender, subject, latest_body, thread_summary, prior_positions = _load_thread(args.thread_json)
    else:
        if not (args.sender and args.subject and args.body_file):
            ap.error("Provide either --thread-json OR --sender/--subject/--body-file")
        sender = args.sender
        subject = args.subject
        latest_body = args.body_file.read_text()
        thread_summary = ""
        prior_positions = ""

    log.info("turdus: classifying message from %s", sender)
    classification = triage.classify(
        sender=sender, subject=subject, body=latest_body, thread_context=thread_summary
    )
    log.info(
        "turdus: bucket=%s target=%s priority=%s",
        classification.bucket,
        classification.target_agent,
        classification.priority,
    )

    log.info("anthus: planning dispatch")
    plan = dispatch.plan(classification)
    log.info("anthus: chain=%s", plan.chain)

    # In production Turdus would create an email_message + task entity here;
    # Apis would pick the task off SSE and call runner.dispatch(). We invoke
    # the same runner directly so the in-process flow matches the daemon flow.
    log.info("apis: dispatching chain=%s", plan.chain)
    ctx = runner.TaskContext(
        sender=sender,
        subject=subject,
        latest_body=latest_body,
        thread_summary=thread_summary,
        prior_positions=prior_positions,
        counterparty_first_name=_counterparty_first_name(sender),
        classification=classification,
    )
    ctx = runner.dispatch(ctx, plan)
    redline: buteo.RedlineReport | None = ctx.artifacts.get("buteo")
    framing: pavo.CommercialFraming | None = ctx.artifacts.get("pavo")

    md = _render_markdown(
        sender=sender,
        subject=subject,
        classification=classification,
        plan=plan,
        redline=redline,
        framing=framing,
    )
    args.output.write_text(md)
    log.info("wrote %s (%d bytes)", args.output, len(md))

    if args.json_output:
        dump = {
            "sender": sender,
            "subject": subject,
            "classification": asdict(classification),
            "plan": asdict(plan),
            "redline": asdict(redline) if redline else None,
            "framing": asdict(framing) if framing else None,
        }
        args.json_output.write_text(json.dumps(dump, indent=2, default=str))
        log.info("wrote %s", args.json_output)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
