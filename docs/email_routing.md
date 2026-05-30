# Email routing flow

End-to-end pipeline for inbound email triage and routing to the appropriate
T4 skill. Wired on branch `claude/email-routing-agent-EQcLJ`.

## Pipeline

```
Gmail message
  │
  ▼
Turdus            lib/agents/triage.py        model_tier=triage
  │  ClassificationResult {bucket, target_agent, priority,
  │                        requires_operator, summary, rationale}
  │  (writes email_message + task entities in Neotoma)
  ▼
Anthus            lib/agents/dispatch.py      bucket → DispatchPlan
  │  DispatchPlan {chain: [...], requires_operator_signoff}
  │  (encoded into task.domain_tags + task.dispatch_chain)
  ▼
Apis              execution/daemons/apis      universal task dispatcher
  │  SSE: task.created → _resolve_skill via _DOMAIN_ROUTES
  │  lib/agents/runner.dispatch(ctx, plan) walks the chain
  ▼
Buteo             lib/agents/buteo.py         model_tier=reasoning
  │  RedlineReport {headline_risk, alignment_summary,
  │                 clause_review[], open_questions,
  │                 next_steps, operator_signoff_required}
  ▼
Pavo              lib/agents/pavo.py          model_tier=synthesis
  │  CommercialFraming {tone_read, concessions, non_negotiables,
  │                     reply_draft, escalation_note,
  │                     send_recommendation}
  ▼
Onychomys         escalation entity surfaced to operator
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

## Models (via capability tiers, not hardwired)

Agents never name a concrete model. Each declares a **tier** in its
`agent_definition.model_tier`; `lib/model_tiers.py` resolves tier → model
ID at call time. Bumping a family is one `correct()` call, never a code
commit.

| Skill | Tier | Default model | Why |
|---|---|---|---|
| Turdus (triage) | `triage` | `claude-haiku-4-5-20251001` | One call per message; fast + cheap |
| Buteo (legal) | `reasoning` | `claude-opus-4-7` | Clause-by-clause inference |
| Pavo (framing) | `synthesis` | `claude-sonnet-4-6` | Synthesis + tone-matching |

Override order (first match wins):
1. `MODEL_<AGENT>` env var (per-agent absolute override)
2. `agent_definition.model_tier` from Neotoma
3. `DEFAULT_AGENT_TIER` in `lib/model_tiers.py`

Then resolve tier → model: `MODEL_TIER_<TIER>` env var, else `DEFAULT_TIER_TO_MODEL`.

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

## Buteo determinism + playbook layer

Buteo is the only agent in this pipeline that opts out of capability-tier
auto-bump. Its `agent_definition` carries:

- `model_pin = "claude-opus-4-7"` — exact model ID, overrides tier resolution
- `prompt_version = "2026-05-28.1"` — frozen system-prompt version
- `temperature = 0` — deterministic decoding

Every `RedlineReport` stamps these three values + the `playbook_id` it
consumed, so re-running the same input is reproducible and any silent
substrate drift is caught by diff.

The `playbook` entity (schema 1.0.0) carries accumulated negotiation
memory for a relationship: `standard_positions`, `non_negotiables`,
`accepted_redlines`, `rejected_positions`. Buteo loads it as context so
the first-pass redline anchors on operator-approved positions instead of
re-deriving them. **Playbooks are operator-authored only** — Buteo never
writes to its own playbook.

Initial Bottega8 partnership playbook: `ent_416966d0c0f8ce0708eb52d0`.

Full design rationale: `docs/buteo_design_rationale.md`.

## Apis integration

Apis (`execution/daemons/apis/apis.py`) is the universal task dispatcher.
Two new routes wired:

```python
_DOMAIN_ROUTES = {
    ...,
    "legal":      "buteo",
    "commercial": "pavo",
}
```

With matching `_DOMAIN_PATTERNS` for `legal` (contract / NDA / IP /
indemnity / clause / SOW) and `commercial` (sourcing fee / revenue share /
deal terms / GTM / partnership). In production, Apis's SSE handler picks
the task off the stream, calls `lib.agents.runner.dispatch()`, and the
runner walks the chain — same code path as the dry-run driver.

## Scope of this slice

What's intentionally NOT in scope here:

- Turdus daemon poll loop creating actual `task` entities with the
  dispatch chain encoded (still uses snippet-only keyword classifier;
  new path exposed via `_classify_and_plan` for future promotion)
- Apis subprocess dispatch (`claude --print --skill <skill>`) — currently
  log-only per the existing Phase 4 skeleton
- Full Phase 6 `participant_contract` emergent dispatch
- Buteo / Pavo as standing daemons with AAuth keypairs
- Gmail draft creation
- Prompt caching across calls

All of the above are tracked for Phase 7 / Phase 8.
