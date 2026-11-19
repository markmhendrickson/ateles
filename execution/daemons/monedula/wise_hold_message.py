"""Operator-facing explanation for a held Wise journal row."""

import payment_journal


def hold_text(entry: dict, *, email: bool = False) -> str:
    """Describe the durable row, giving an equal-time conflict precedence."""
    if entry.get("timestamp_conflict_status"):
        return ("The two Wise reads of this transfer conflict at the same time; "
                "nothing has been changed. A later observation from Wise is "
                "needed. Check Wise Activity; do not pay again.")
    state = entry.get("state")
    if email:
        if state == "funding_pending":
            return "submitted, not confirmed; Monedula is checking; do not pay again"
        if entry.get("failure_status") == "not_submitted":
            return "not submitted; no payment was made. " + payment_journal.REPLACEMENT_HOLD_MESSAGE
        if state == "failed" or (state == "done" and entry.get("last_status") not in (None, "outgoing_payment_sent")):
            return "returned or cancelled. " + payment_journal.REPLACEMENT_HOLD_MESSAGE
        if entry.get("transfer_id"):
            return "transfer exists, outcome unknown; Monedula is checking; do not pay again"
        return ("Wise submission outcome unknown; check Wise Activity by date, "
                "amount and reference; do not pay again. Monedula cannot check "
                "automatically without a transfer ID")
    if state == "done" and "last_status" not in entry and entry.get("outcome_status") != "sent":
        return "Outcome unconfirmed; check Wise Activity; do not pay again."
    if state == "done" and entry.get("last_status") != "outgoing_payment_sent":
        return ("The transfer was previously sent, but its latest status is "
                f"{entry.get('last_status') or 'unknown'}; review the return. "
                "Task and session bookkeeping held; do not pay again.")
    if state == "done":
        if entry.get("effects_applied") is not True:
            return "Transfer sent; task and session bookkeeping held; do not pay again."
        return "Transfer already sent; do not pay again."
    if state == "funding_pending":
        return "Submitted, not confirmed. Monedula is checking the transfer; do not pay again."
    if state == "failed":
        if entry.get("failure_status") == "not_submitted":
            return "Not submitted; no payment was made. " + payment_journal.REPLACEMENT_HOLD_MESSAGE
        return "The transfer was returned or cancelled. " + payment_journal.REPLACEMENT_HOLD_MESSAGE
    if entry.get("transfer_id"):
        return "A transfer exists, but its outcome is unknown. Monedula is checking it; do not pay again."
    return ("Wise submission outcome unknown. Check Wise Activity by date, amount "
            "and reference; do not pay again. Monedula cannot check automatically "
            "without a transfer ID.")
