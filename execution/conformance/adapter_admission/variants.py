"""Six negative-variant factories, one per admission obligation.

Table is the foundation's own (`docs/foundation/conformance_suite.md#adapter-admission`), hard-coded
here once — arch: "Foundation table is SSOT... do not invent a second table in runner, README, or
tests." Nothing else in this package restates it; `runner.py` imports `EXPECTED_RED` from here.

Each factory takes a zero-arg adapter factory (the real `SIXTH_ADAPTER_FACTORY` from #1190, or the
in-repo fake in `reference.py`) and returns a *fresh* mutated instance — never mutates a shared
instance in place, so reference and variant runs never share state (arch: variant isolation).
"""

from __future__ import annotations

from typing import Callable

from execution.conformance.adapter_admission.reference import ReferenceAdapter

# The foundation table, verbatim mapping of obligation -> expected red row set.
# Obligation 2's set has cardinality 2 by contract (AD-4 and AD-22) — never SELF_CHECK_MULTI_RED.
EXPECTED_RED: dict[int, frozenset[str]] = {
    1: frozenset({"AD-21"}),
    2: frozenset({"AD-4", "AD-22"}),
    3: frozenset({"AD-11"}),
    4: frozenset({"AD-17"}),
    5: frozenset({"AD-13"}),
    6: frozenset({"AD-26"}),
}

VARIANT_DESCRIPTIONS: dict[int, str] = {
    1: "drop counter unwired; an unmapped event logged/discarded without counted dropped/unmapped",
    2: "an unrecognized credential resolves to the operator",
    3: "per-attempt token as inbound key; fresh outbound key per attempt",
    4: "introduces a last_seen cursor (or equivalent local freshness state)",
    5: "acks before read-back; acks during halt",
    6: "outbound class absent from policy, action taken anyway",
}


def make_variant_1(factory: Callable[[], ReferenceAdapter]) -> ReferenceAdapter:
    adapter = factory()
    adapter.drop_counter_wired = False
    return adapter


def make_variant_2(factory: Callable[[], ReferenceAdapter]) -> ReferenceAdapter:
    adapter = factory()
    adapter.unrecognized_credential_fallthrough_to_operator = True
    return adapter


def make_variant_3(factory: Callable[[], ReferenceAdapter]) -> ReferenceAdapter:
    adapter = factory()
    adapter.per_attempt_inbound_keying = True
    adapter.fresh_key_per_outbound_attempt = True
    return adapter


def make_variant_4(factory: Callable[[], ReferenceAdapter]) -> ReferenceAdapter:
    adapter = factory()
    adapter.note_last_seen()
    return adapter


def make_variant_5(factory: Callable[[], ReferenceAdapter]) -> ReferenceAdapter:
    adapter = factory()
    adapter.acks_before_readback = True
    adapter.acks_during_halt = True
    return adapter


def make_variant_6(factory: Callable[[], ReferenceAdapter]) -> ReferenceAdapter:
    adapter = factory()
    adapter.unlisted_class_taken = True
    return adapter


VARIANT_FACTORIES: dict[int, Callable[[Callable[[], ReferenceAdapter]], ReferenceAdapter]] = {
    1: make_variant_1,
    2: make_variant_2,
    3: make_variant_3,
    4: make_variant_4,
    5: make_variant_5,
    6: make_variant_6,
}
