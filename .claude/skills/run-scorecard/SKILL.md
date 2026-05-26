---
name: run-scorecard
description: "Execute BTC or altcoin liquidity regime scorecard and save the report. Use when user says \"run BTC scorecard\", \"run liquidity scorecard\", \"execute scorecard\", or \"altcoin scorecard\". Can be invoked via /run-scorecard."
triggers:
  - run BTC scorecard
  - run liquidity scorecard
  - execute scorecard
  - altcoin scorecard
  - run-scorecard
user_invocable: true
entity_id: ent_d8abc4cb866c5c41b63f15dd
---

# Run Liquidity Scorecard

Execute the BTC or altcoin liquidity regime scorecard per reference docs and save the report to the required location with all required contents.

## When to Use

Use this skill when:
- User says "run BTC scorecard", "run liquidity scorecard", "execute scorecard", "altcoin scorecard"
- User requests a liquidity regime assessment for the weekly or quarterly process

## Required Documents (load first)

1. **Scorecard execution and saving:** [docs/workflow_requirements_rules.mdc](docs/workflow_requirements_rules.mdc) (Scorecard Execution & Saving)
2. **BTC scorecard:** [strategy/reference/btc-liquidity-regime-scorecard.md](strategy/reference/btc-liquidity-regime-scorecard.md)
3. **Altcoin scorecard:** [strategy/reference/altcoin-liquidity-regime-scorecard.md](strategy/reference/altcoin-liquidity-regime-scorecard.md)
4. **Context:** [strategy/operations/operating-manual.md](strategy/operations/operating-manual.md) (Weekly Rules), [strategy/operations/finance/quarterly-portfolio-review-process.md](strategy/operations/finance/quarterly-portfolio-review-process.md) (Steps 1.1, 1.2) as referenced

## Workflow

1. **Choose scorecard:** BTC or altcoin based on user request.
2. **Execute scorecard** per the reference doc: assign scores (-2 to +2) for each indicator, compute category totals, determine final regime classification. Use data sources and rationale as specified in the scorecard.
3. **Save report** to `strategy/operations/finance/` with exact filename:
   - BTC: `btc-liquidity-regime-report-YYYY-MM-DD.md`
   - Altcoin: `altcoin-liquidity-regime-report-YYYY-MM-DD.md`
   Use current date for YYYY-MM-DD unless user specifies another date.
4. **Required contents:** All scores (-2 to +2 for each indicator), category totals, final regime classification, data sources and verification notes, rationale for each score.

## Constraints

- Save to `strategy/operations/finance/` only; do not use ad-hoc paths.
- Include all required contents; do not omit rationale or data sources.
- Use the exact file naming above.

## Related Rules

- [docs/workflow_requirements_rules.mdc](docs/workflow_requirements_rules.mdc) — Scorecard Execution & Saving
- [strategy/reference/btc-liquidity-regime-scorecard.md](strategy/reference/btc-liquidity-regime-scorecard.md)
- [strategy/reference/altcoin-liquidity-regime-scorecard.md](strategy/reference/altcoin-liquidity-regime-scorecard.md)
