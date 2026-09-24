"""Per-row instruments for AD-21 through AD-34.

AD-21..AD-26 (load-bearing) and the reused five-rules rows (AD-4, AD-11, AD-13, AD-17) execute
against a `ReferenceAdapter` instance and return the set of row ids the instance turns red. AD-31 and
AD-32 are static checks over this fixture's own contracts and the sixth's document. AD-27..AD-30 and
AD-33..AD-34 register as `INSTRUMENT_MISSING` — no fixtures for grant/workflow/provenance exist in
this repo yet, and expanding them is #921/#963 scope, not this issue's (arch: no bootstrap scope creep).
"""

from __future__ import annotations

from pathlib import Path

from execution.conformance.adapter_admission.reference import (
    CRED_NONE,
    CRED_OTHER,
    CRED_OWNER,
    Delivery,
    OutboundAttempt,
    ReferenceAdapter,
)
from execution.conformance.row_result import RowId

# Rows an instrument can actually execute today (AD-27..30, AD-33..34 are INSTRUMENT_MISSING).
EXECUTABLE_ROWS: frozenset[RowId] = frozenset(
    {"AD-4", "AD-11", "AD-13", "AD-17", "AD-21", "AD-22", "AD-23", "AD-24", "AD-25", "AD-26", "AD-31", "AD-32"}
)
INSTRUMENT_MISSING_ROWS: frozenset[RowId] = frozenset(
    {"AD-27", "AD-28", "AD-29", "AD-30", "AD-33", "AD-34"}
)
ALL_ROWS: frozenset[RowId] = EXECUTABLE_ROWS | INSTRUMENT_MISSING_ROWS


def check_ad21(adapter: ReferenceAdapter) -> bool:
    """Obligation 1: coverage is a counter. Red when an unmapped event is not counted dropped/unmapped."""
    before = adapter.dropped_unmapped_count
    adapter.deliver(Delivery(external_id="unmapped-1", kind="event", credential=CRED_NONE, mapped=False))
    return adapter.dropped_unmapped_count == before  # red: counter did not move


def check_ad4(adapter: ReferenceAdapter) -> bool:
    """Five-rules row reused by obligation 2: no resolution to the operator from an unrecognized credential."""
    disposition = adapter.deliver(
        Delivery(external_id="approval-1", kind="conclusion", credential=CRED_NONE)
    )
    return disposition.attributed_to_operator  # red: resolved to the operator


def check_ad22(adapter: ReferenceAdapter) -> bool:
    """Obligation 2: same negative test — the fallthrough must turn AD-4 red when reintroduced."""
    return check_ad4(adapter)


def check_ad11(adapter: ReferenceAdapter) -> bool:
    """Five-rules row reused by obligation 3: a redelivery produces exactly one write; a confirmed
    outbound key is refused on re-take."""
    # A real redelivery is a distinct wire message carrying the same external_id — a second,
    # separate Delivery object, never the same Python object reused. Reusing one object would
    # give `per_attempt_inbound_keying`'s id()-based mutant the same key both times, masking
    # the inbound half of the obligation-3 mutation from this instrument.
    first = Delivery(external_id="delivery-redelivered", kind="event", credential=CRED_OTHER)
    adapter.deliver(first)
    before_ids = len(adapter.inbound_seen_delivery_ids)
    redelivery = Delivery(external_id="delivery-redelivered", kind="event", credential=CRED_OTHER)
    adapter.deliver(redelivery)
    after_ids = len(adapter.inbound_seen_delivery_ids)
    inbound_dup = after_ids != before_ids  # a second distinct key was recorded for the same delivery

    attempt = OutboundAttempt(dedup_key="action-1", action_class="comment")
    adapter.take_outbound(attempt)
    retake = OutboundAttempt(dedup_key="action-1", action_class="comment")
    adapter.take_outbound(retake)
    outbound_dup = retake.taken  # a confirmed key was taken again

    return inbound_dup or outbound_dup


def check_ad23(adapter: ReferenceAdapter) -> bool:
    """Obligation 3: same instrument as AD-11."""
    return check_ad11(adapter)


def check_ad17(adapter: ReferenceAdapter) -> bool:
    """Five-rules row reused by obligation 4: the adapter keeps no history — no cursor/sync-log state."""
    return adapter.last_seen_cursor_path is not None


def check_ad24(adapter: ReferenceAdapter) -> bool:
    """Obligation 4: same instrument as AD-17."""
    return check_ad17(adapter)


def check_ad13(adapter: ReferenceAdapter) -> bool:
    """Five-rules row reused by obligation 5: read-back before ack; nothing acked during a halt."""
    attempt = OutboundAttempt(dedup_key="decision-1", action_class="comment")
    adapter.take_outbound(attempt)
    premature_ack = attempt.acked_before_readback

    halted_attempt = OutboundAttempt(dedup_key="decision-2", action_class="comment")
    adapter.take_outbound(halted_attempt, halted=True)
    ack_during_halt = halted_attempt.acked_during_halt

    return premature_ack or ack_during_halt


def check_ad25(adapter: ReferenceAdapter) -> bool:
    """Obligation 5: same instrument as AD-13."""
    return check_ad13(adapter)


def check_ad26(adapter: ReferenceAdapter) -> bool:
    """Obligation 6: an outbound class absent from the policy must never be taken."""
    attempt = OutboundAttempt(dedup_key="unlisted-1", action_class="publish_unlisted")
    adapter.take_outbound(attempt)
    return attempt.taken  # red: an unlisted class was taken


ROW_CHECKS = {
    "AD-4": check_ad4,
    "AD-11": check_ad11,
    "AD-13": check_ad13,
    "AD-17": check_ad17,
    "AD-21": check_ad21,
    "AD-22": check_ad22,
    "AD-23": check_ad23,
    "AD-24": check_ad24,
    "AD-25": check_ad25,
    "AD-26": check_ad26,
}

# AD-31: static mapping check — AD-21..26 <-> AD-9..14 + AD-18. Kept as an explicit table so a
# mapping gap (an obligation with no five-rules row, or vice versa) is a diffable data structure,
# not something inferred from prose (docs/foundation/adapters.md#the-obligations-are-the-five-rules-restated-as-failures).
OBLIGATION_TO_FIVE_RULES_ROW: dict[str, str] = {
    "AD-21": "AD-12",  # obligation 1 (coverage/disposition) restates AD-12's disposition rule
    "AD-22": "AD-3",  # obligation 2 (identity) restates AD-3/AD-9's identity rule
    "AD-23": "AD-11",  # obligation 3 (dedup) restates AD-11
    "AD-24": "AD-17",  # obligation 4 (no maintained freshness) restates AD-17
    "AD-25": "AD-13",  # obligation 5 (read-back before ack) restates AD-13
    "AD-26": "AD-18",  # obligation 6 (outbound gate) restates AD-18
}


def check_ad31() -> bool:
    """Red if an obligation row has no five-rules row behind it, or vice versa isn't reachable."""
    obligation_rows = {f"AD-2{n}" for n in range(1, 7)}
    mapped_obligations = set(OBLIGATION_TO_FIVE_RULES_ROW.keys())
    if mapped_obligations != obligation_rows:
        return True  # red: a gap in the mapping table itself
    return any(not target for target in OBLIGATION_TO_FIVE_RULES_ROW.values())


REQUIRED_DOCUMENT_PARTS: tuple[str, ...] = (
    "Scope, and the enumeration's boundary",
    "The inbound table",
    "The identity section",
    "The linkage section",
    "The outbound table",
    "Recoveries",
    "What this adapter never does, at this system specifically",
    "What the document does not decide",
)


def check_ad32(document_text: str) -> bool:
    """Lint the sixth's adapter document for the eight required parts (structure only, per
    docs/foundation/adapters.md#what-an-adapters-document-must-contain)."""
    return any(part not in document_text for part in REQUIRED_DOCUMENT_PARTS)


def default_sixth_document_path() -> Path:
    return Path(__file__).parent / "sixth_adapter_document.md"
