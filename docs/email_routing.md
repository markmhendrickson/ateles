# Email routing flow

End-to-end pipeline for inbound email triage and routing to the appropriate
T4 skill. Wired on branch `claude/email-routing-agent-EQcLJ`.

## Pipeline

```
Gmail message
  │
  ▼
Turdus            lib/agents/triage.py        Claude Haiku 4.5
  │  ClassificationResult {bucket, target_agent, priority,
  │                        requires_operator, summary, rationale}
  ▼
Anthus            lib/agents/dispatch.py      label-based routing table
  │  DispatchPlan {chain: [...], requires_operator_signoff}
  ▼
Buteo             lib/agents/buteo.py         Claude Opus 4.7
  │  RedlineReport {headline_risk, alignment_summary,
  │                 clause_review[], open_questions,
  │                 next_steps, operator_signoff_required}
  ▼
Pavo              lib/agents/pavo.py          Claude Sonnet 4.6
  │  CommercialFraming {tone_read, concessions, non_negotiables,
  │                     reply_draft, escalation_note,
  │                     send_recommendation}
  ▼
Onychomys         escalation note surfaced to operator
  │
  ▼
Operator approves → Gmail draft (not yet wired) → send
```

## Bucket → agent routing

| Bucket | Chain | Operator sign-off |
|---|---|---|
| legal | buteo → pavo | required |
| commercial | pavo | required |
| code | gryllus | optional |
| scheduling | onychomys | n/a |
| personal | onychomys | n/a |
| notification | (none) | n/a |
| noise | (none) | n/a |

## Driver script

```bash
# Run on a Gmail thread export (FULL_CONTENT JSON)
python scripts/run_email_flow.py \
    --thread-json /tmp/nick-thread.json \
    --output /tmp/email-flow-output.md \
    --json-output /tmp/email-flow-output.json
```

Dry-run only — does NOT save a Gmail draft and does NOT send anything.

## Models

| Skill | Model | Why |
|---|---|---|
| Turdus (triage) | `claude-haiku-4-5-20251001` | Fast, cheap, one call per message |
| Buteo (legal) | `claude-opus-4-7` | Clause-by-clause reasoning needs strong inference |
| Pavo (framing) | `claude-sonnet-4-6` | Synthesis + tone-matching balanced model |

Without `ANTHROPIC_API_KEY` set, each skill returns a structured stub that
explains what the live call would do, so the dry-run pipeline still produces
a complete artifact in CI / sandboxed environments.

## Agent definitions in Neotoma

| Agent | entity_id |
|---|---|
| Turdus | `ent_138a463654de2b1d46cec0db` |
| Buteo | `ent_92f55cf7bd0bfc97710539c1` |
| Pavo | `ent_bb1bf70f7c2c1bad4a0bf9b0` |

Both new entities are linked `PART_OF` the Ateles plan `ent_99ace4dd6673aa36ed08b1fe`.

## Scope of this slice

This is the minimal end-to-end implementation (per the design call on
2026-05-28). What's intentionally NOT in scope here:

- Turdus daemon poll loop using the new classifier (still uses
  snippet-only keyword classifier; new path exposed via
  `_classify_and_plan` for future promotion)
- Full Phase 6 `participant_contract` emergent dispatch
- Buteo / Pavo as standing daemons with AAuth keypairs
- Gmail draft creation
- Prompt caching across calls

All of the above are tracked for Phase 7 / Phase 8.
