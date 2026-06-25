# Agent Workflow Requirements

**Status:** Active  
**Last Updated:** 2025-01-15  
**Related:** `/shared/docs/agent-context.md`, `/strategy/operations/README.md`

---

## Purpose

This document defines mandatory requirements for agents when executing workflows, including scorecard execution, quarterly reviews, and file naming conventions.

## Scope

Applies to all agents executing repository workflows — covers file-location requirements, scorecard saving, quarterly reviews, and the workflow rules below.

---

## Scorecard Execution & Saving

**MANDATORY:** Whenever liquidity regime scorecards are executed, results MUST be saved to the repository.

### File Requirements

- **Location:** `/strategy/operations/finance/`
- **File naming:**
  - `btc-liquidity-regime-report-YYYY-MM-DD.md`
  - `altcoin-liquidity-regime-report-YYYY-MM-DD.md`

### Required Contents

- All scores (-2 to +2 for each indicator)
- Category totals
- Final regime classification
- Data sources and verification notes
- Rationale for each score

### References

- Scorecards: `/strategy/reference/btc-liquidity-regime-scorecard.md`, `/strategy/reference/altcoin-liquidity-regime-scorecard.md`
- Process: `/strategy/operations/operating-manual.md` (Weekly Rules), `/strategy/operations/finance/quarterly-portfolio-review-process.md` (Steps 1.1, 1.2)

**Rationale:** Creates audit trail, enables historical comparison, supports pattern recognition.

---

## Quarterly Review Reports

**MANDATORY:** Quarterly portfolio reviews must generate comprehensive reports.

### File Requirements

- **Location:** `/strategy/operations/finance/`
- **File naming:** `quarterly-portfolio-review-YYYY-QX.md`
- **Process:** `/strategy/operations/finance/quarterly-portfolio-review-process.md`
- **Template:** `/strategy/operations/finance/quarterly-portfolio-review-template.md`

---

## File Naming Conventions

### Date Format

- Use `YYYY-MM-DD` format for all dated files
- Examples: `2025-01-15`, `2025-12-05`

### Report Files

- **Quarterly reviews:** `finance/quarterly-portfolio-review-YYYY-QX.md`
- **BTC scorecard reports:** `finance/btc-liquidity-regime-report-YYYY-MM-DD.md`
- **Altcoin scorecard reports:** `finance/altcoin-liquidity-regime-report-YYYY-MM-DD.md`

### General Conventions

- Use descriptive, clear filenames
- Include dates in filenames when relevant
- Mark canonical documents with clear headers indicating status

**Reference:** `/README.md` (Document Naming section) for general conventions.

---

## Document Updates

When creating or updating plans in any domain:

1. Update relevant canonical documents in `/strategy/strategy/`, `/strategy/tactics/`, or `/strategy/operations/[domain]/`
2. Maintain consistency with existing principles
3. Follow document hierarchy (strategy > tactics > operations)
4. For finance domain, update documents in `/strategy/strategy/`, `/strategy/tactics/`, or `/strategy/operations/finance/`

---

## Related Documentation

- `/shared/docs/agent-context.md` - Agent context and quick reference (index to all rule documents)
- `/strategy/operations/README.md` - Complete workflow documentation
- `/strategy/operations/finance/quarterly-portfolio-review-process.md` - Quarterly review workflow
- `/strategy/reference/btc-liquidity-regime-scorecard.md` - BTC scorecard
- `/strategy/reference/altcoin-liquidity-regime-scorecard.md` - Altcoin scorecard







