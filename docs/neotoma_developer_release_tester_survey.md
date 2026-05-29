# Neotoma Developer Release Tester Survey

Use this survey to qualify high-signal testers for the Neotoma developer release.

Intent: filter for builders with real state integrity pain, not broad curiosity.

## Intro Copy (Top of Form)

Neotoma is a deterministic state layer for long-running agents.

This developer release is for builders dealing with state drift, conflicting state, and unreproducible decisions across sessions and tools.

This short survey helps us prioritize who should get direct tester onboarding.

## Survey Questions

### 1) Role Context

Which best describes your role?

- Applied AI Engineer
- Staff/Principal Engineer
- Founder/CTO
- Product Engineer (agent systems)
- Research Engineer
- Other

### 2) Workflow Environment

Which environments do you actively use for agent workflows? (select all that apply)

- Claude Code
- Cursor
- Codex
- Agent orchestration framework
- Custom production automation stack
- None of the above

### 3) Runtime Horizon

Are your agents long-running across sessions?

- Yes, in production
- Yes, in pre-production
- Not yet, but planned in the next 3 months
- No

### 4) Integrity Failure Modes

Which issues do you regularly experience? (select up to 3)

- State drift over time
- Conflicting state across tools
- Silent or implicit state mutation
- Hard-to-audit state changes
- Inability to replay decision history
- Unreproducible decisions
- None of these

### 5) Severity

How painful are these issues right now?

- Critical (reliability blocker)
- High (frequent incidents)
- Medium (costly but manageable)
- Low (occasional annoyance)

### 6) Concrete Example

Describe one concrete state failure from the last 30 days.

(short answer)

### 7) Current Workarounds

How do you currently handle this?

- Structured logs / decision journals
- Search plus intuition
- Periodic reset and rebuild
- Ad hoc fixes
- I do not actively handle it

### 8) Desired Guarantee

If solved, what matters most?

- Deterministic behavior
- Replayability
- Auditability
- Faster debugging
- Lower incident frequency
- Other

### 9) Model Fit

Which statement best matches your need?

- I need deterministic, versioned, replayable state transitions
- I mainly need better retrieval/search
- I need both
- Not sure

### 10) Early-Product Tolerance

How do you approach early developer releases?

- I tolerate rough edges if the architecture is strong
- I can tolerate moderate friction
- I prefer polished products only
- I avoid early products

### 11) Feedback Commitment

Which forms of contribution can you commit to? (select all that apply)

- Async feedback
- Bug reports / repro steps
- 20-30 minute technical debrief call
- Trying edge cases intentionally
- Passive observation only

### 12) Timing

Can you run a real test workflow in the next 14 days?

- Yes
- Likely
- Unlikely
- No

### 13) Contact

- Name
- Email
- GitHub/LinkedIn/X (optional)

## Internal Scoring Rubric

Do not show this section in the external form.

### Strong ICP (accept now)

All of the following are true:

- Q3: production/pre-production/near-term long-running
- Q4: at least 2 state integrity failure modes selected
- Q5: Critical or High
- Q9: deterministic/replayable option (or "both")
- Q10: can tolerate early friction
- Q12: Yes or Likely

### Partial ICP (waitlist)

- Clear pain but lower urgency, unclear model fit, or weak testing commitment.

### Low ICP (decline for this stage)

Any strong disqualifier:

- Q4: None of these
- Q9: retrieval-only and no integrity pain
- Q10: polished-only or avoids early products
- Q12: No

## Suggested Follow-Up Policy

- Strong ICP: direct outreach and onboarding within 48 hours.
- Partial ICP: monthly updates and re-check at next release stage.
- Low ICP: thank-you note and optional public updates only.
