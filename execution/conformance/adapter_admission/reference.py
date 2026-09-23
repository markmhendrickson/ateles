"""The `ReferenceAdapter` shape instruments observe, and the in-repo fake that satisfies it.

`docs/foundation/adapters.md#what-an-adapters-document-must-contain` requires the sixth adapter's
document and code to demonstrate all six obligations. Until `lib.adapters` (#1190) exists, this module
is the fixture's own fake — deliberately built to the same shape `runtime.py` expects the real sixth
adapter to expose, so swapping the fake for the real `SIXTH_ADAPTER_FACTORY` requires no change to
`variants.py` or `instruments.py`. Fixtures only: no live network I/O, no production credentials
(arch/legal — synthetic labels only).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Synthetic credential namespace (arch #2, legal #3) — labels only, never real secret material.
CRED_NONE = "cred-none"
CRED_OWNER = "cred-owner"
CRED_OPERATOR = "cred-op"
CRED_OTHER = "cred-other"

SYSTEM_NAMESPACE = "admission-fixture"


@dataclass
class Delivery:
    external_id: str
    kind: str  # "conclusion" | "event" | "check_result"
    credential: str
    mapped: bool = True
    action_class: Optional[str] = None


@dataclass
class Disposition:
    delivery: Delivery
    outcome: str  # "verdict" | "observation" | "dropped" | "successor" | "batch_advanced"
    dropped_reason: Optional[str] = None
    attributed_to_operator: bool = False


@dataclass
class OutboundAttempt:
    dedup_key: str
    action_class: str
    taken: bool = False
    acked_before_readback: bool = False
    acked_during_halt: bool = False


@dataclass
class ReferenceAdapter:
    """A fresh instance per run (never shared across reference vs. variant runs — arch: isolation).

    Each field models one obligation's load-bearing state. A negative variant (`variants.py`) takes
    one of the six `make_*` factories, each returning a *mutated copy* that violates exactly one
    obligation while leaving the other five conformant.
    """

    # Obligation 1: coverage is a counter.
    dropped_unmapped_count: int = 0
    drop_counter_wired: bool = True

    # Obligation 2: identity resolves through the credential binding, never a fallthrough to the operator.
    unrecognized_credential_fallthrough_to_operator: bool = False

    # Obligation 3: dedup inbound on delivery id, outbound on dedup_key.
    inbound_seen_delivery_ids: set = field(default_factory=set)
    outbound_confirmed_keys: set = field(default_factory=set)
    per_attempt_inbound_keying: bool = False  # obl-3 mutant: keys on attempt token, not delivery id
    fresh_key_per_outbound_attempt: bool = False  # obl-3 mutant: never reuses confirmed dedup_key

    # Obligation 4: no maintained freshness state (last_seen cursor, sync log, artifact cache).
    last_seen_cursor_path: Optional[Path] = None  # non-None only in the obl-4 mutant

    # Obligation 5: read-back before ack; nothing acked during a halt.
    acks_before_readback: bool = False  # obl-5 mutant
    acks_during_halt: bool = False  # obl-5 mutant

    # Obligation 6: every outbound class listed in the action_policy, or NEVER.
    action_policy_classes: frozenset = field(default_factory=lambda: frozenset({"comment", "merge"}))
    unlisted_class_taken: bool = False  # obl-6 mutant

    def deliver(self, delivery: Delivery) -> Disposition:
        if self.per_attempt_inbound_keying:
            key = f"attempt-{id(delivery)}"
        else:
            key = delivery.external_id
        redelivery = key in self.inbound_seen_delivery_ids
        self.inbound_seen_delivery_ids.add(key)

        if not delivery.mapped:
            if self.drop_counter_wired:
                self.dropped_unmapped_count += 1
                return Disposition(delivery, outcome="dropped", dropped_reason="unmapped")
            # Mutant: drop counter unwired — the event is silently discarded, uncounted.
            return Disposition(delivery, outcome="dropped", dropped_reason=None)

        if delivery.kind == "conclusion":
            if delivery.credential == CRED_NONE or delivery.credential == CRED_OTHER:
                if self.unrecognized_credential_fallthrough_to_operator:
                    return Disposition(delivery, outcome="verdict", attributed_to_operator=True)
                return Disposition(delivery, outcome="observation")
            if delivery.credential == CRED_OWNER:
                return Disposition(delivery, outcome="verdict")

        if not redelivery:
            return Disposition(delivery, outcome="observation")
        # Redelivery of an already-seen id: obligation 3 requires exactly one write. The reference
        # dedupes on the (possibly per-attempt) key computed above, so a conformant adapter reports
        # this as the same observation, never a second effect.
        return Disposition(delivery, outcome="observation")

    def take_outbound(self, attempt: OutboundAttempt, *, halted: bool = False) -> None:
        if attempt.action_class not in self.action_policy_classes:
            if self.unlisted_class_taken:
                attempt.taken = True
            return  # conformant: NEVER — nothing is taken for an unlisted class

        if halted:
            if self.acks_during_halt:
                attempt.acked_during_halt = True
            return  # conformant: nothing acknowledged, nothing taken, during a halt

        key = (
            f"attempt-{id(attempt)}"
            if self.fresh_key_per_outbound_attempt
            else attempt.dedup_key
        )
        if key in self.outbound_confirmed_keys:
            return  # confirmed key refused
        if self.acks_before_readback:
            attempt.acked_before_readback = True
        attempt.taken = True
        self.outbound_confirmed_keys.add(key)

    def note_last_seen(self) -> None:
        """Only ever called by the obligation-4 mutant; the conformant adapter never calls this."""
        if self.last_seen_cursor_path is None:
            fd = tempfile.NamedTemporaryFile(prefix="admission-fixture-last-seen-", delete=False)
            self.last_seen_cursor_path = Path(fd.name)
            fd.close()

    def cleanup(self) -> None:
        """Disposable temp state cleanup (arch: variant isolation)."""
        if self.last_seen_cursor_path is not None and self.last_seen_cursor_path.exists():
            self.last_seen_cursor_path.unlink()


def make_reference_adapter() -> ReferenceAdapter:
    """The all-green sixth: satisfies all six obligations. This is the in-repo stand-in
    `runtime.py` falls back to describing (never silently substitutes) when #1190 is absent —
    the runner only ever calls `import_sixth_adapter_factory()`, which raises rather than
    returning this. Used directly by tests that must exercise the reference/variant contract
    without a real runtime import (`test_runner_reference.py`, `test_variants_self_check.py`).
    """
    return ReferenceAdapter()
