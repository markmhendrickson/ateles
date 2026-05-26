---
name: quarterly-portfolio-review
description: "Generate a quarterly portfolio review report from the template and process. Use when user says \\"quarterly review\\", \\"run quarterly portfolio review\\", or \\"generate quarterly review\\". Can be invoked via /quarterly-portfolio-review."
triggers:
  - quarterly review
  - run quarterly portfolio review
  - generate quarterly review
  - quarterly-portfolio-review
user_invocable: true
entity_id: ent_8be8fcea6cd823507f4956f9
---

# Quarterly Portfolio Review

Generate a comprehensive quarterly portfolio review report using the process doc and template, and save to the required location with correct naming.

## When to Use

Use this skill when:
- User says "quarterly review", "run quarterly portfolio review", "generate quarterly review"
- User requests a quarterly portfolio review for a given quarter

## Required Documents (load first)

1. **Quarterly review requirements:** [docs/workflow_requirements_rules.mdc](docs/workflow_requirements_rules.mdc) (Quarterly Review Reports)
2. **Template:** [strategy/operations/finance/quarterly-portfolio-review-template.md](strategy/operations/finance/quarterly-portfolio-review-template.md)
3. **Process (if present):** [strategy/operations/finance/quarterly-portfolio-review-process.md](strategy/operations/finance/quarterly-portfolio-review-process.md)

## Workflow

1. **Determine quarter:** Use user-specified quarter or current quarter (e.g. 2025-Q1). Format YYYY-QX for filename.
2. **Load process doc** if it exists; follow steps for data gathering, regime assessment, and report structure.
3. **Use template:** Fill [strategy/operations/finance/quarterly-portfolio-review-template.md](strategy/operations/finance/quarterly-portfolio-review-template.md) with data and findings per process. Include references to scorecard reports (e.g. `btc-liquidity-regime-report-YYYY-MM-DD.md`, `altcoin-liquidity-regime-report-YYYY-MM-DD.md`) when applicable.
4. **Save report** to `strategy/operations/finance/quarterly-portfolio-review-YYYY-QX.md` (e.g. `quarterly-portfolio-review-2025-Q1.md`).

## Constraints

- Save only to `strategy/operations/finance/` with filename `quarterly-portfolio-review-YYYY-QX.md`.
- Follow the template structure and process doc when available.
- Use date format YYYY-MM-DD for dates in content and YYYY-QX for the report filename.

## Related Rules

- [docs/workflow_requirements_rules.mdc](docs/workflow_requirements_rules.mdc) — Quarterly Review Reports
- [strategy/operations/finance/quarterly-portfolio-review-template.md](strategy/operations/finance/quarterly-portfolio-review-template.md)
- [strategy/operations/finance/quarterly-portfolio-review-process.md](strategy/operations/finance/quarterly-portfolio-review-process.md) (if present)
