# Agent Confirmation Requirements

**Status:** Active  
**Last Updated:** 2025-12-24  
**Related:** `/shared/docs/agent-context.md`

---

## Purpose

This policy defines mandatory confirmation requirements for agents before executing actions that have real-world consequences (emails, transactions, etc.).

---

## Email Sending Confirmation

**MANDATORY:** Agents must **NEVER** send emails without explicit user confirmation.

### Confirmation Process

1. **Preview requirement:** Display complete email details including:
   - Recipient(s)
   - Subject
   - Body content
   - CC/BCC recipients (if any)
   - Thread/Reply context (if replying)
   - **Attachments** (file names and paths, if any)

2. **Confirmation step:** Wait for explicit user approval before sending

3. **Attachments special handling:**
   - **MANDATORY:** Never send emails automatically when attachments are included
   - **MANDATORY:** Always show updated draft with attachment details before sending
   - Display attachment file names and paths in the preview
   - Wait for explicit user confirmation before sending emails with attachments

4. **Applies to:** All email sending operations (new emails, replies, drafts sent via MCP or other tools)

**Rationale:** Prevents accidental email sends, ensures user control over all communications, and provides opportunity to review and edit before sending.

---

## Transaction Preview & Confirmation

**MANDATORY:** Agents must always preview and get user confirmation before submitting any transaction to transaction processors (e.g., Wise API, crypto wallet scripts, payment processors).

### Preview Requirements

Display complete transaction details including:
- Amount (source and target if different currencies)
- Recipient information (name, account details)
- Fees and total cost
- Reference/description
- Payment method/funding source
- Any relevant warnings or notes

### Confirmation Process

1. **Confirmation step:** Wait for explicit user approval before executing the transaction
2. **Dry-run mode:** Use `--dry-run` flags when available to show what would be executed
3. **Applies to:** All transaction types (Wise transfers, crypto transactions, payment scripts, etc.)

**Rationale:** Prevents accidental transactions, ensures user awareness of all transaction details, and provides opportunity to correct errors before funds are moved.

---

## Wise Transfer Transaction Recording

**MANDATORY:** All Wise transfers created via `scripts/wise_transfer.py` are automatically saved as transactions in `$DATA_DIR/transactions/transactions.parquet`.

### Transaction Details

- **Amount handling:** Fees are added on top of the target amount (recipient receives full amount, you pay amount + fees)
- **Transaction fields:** Transfer includes transfer ID, recipient details, reference, and fee information
- **Category:** Defaults to 'transfer' (can be more specific like 'donation' if detected in reference)
- **Account:** Stored with `account_id='wise'` and `bank_provider='wise'`
- **Currency conversion:** Automatically converts to USD using `CurrencyConverter` (Frankfurter / ECB rates via `execution/scripts/frankfurter_fx.py`) when available

**Rationale:** Ensures all Wise transfers are tracked in the transaction database for financial record-keeping and analysis.

---

## Related Documentation

- `/shared/docs/agent-context.md` - Agent context and quick reference (index to all rule documents)







