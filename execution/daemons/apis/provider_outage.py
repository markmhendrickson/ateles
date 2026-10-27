"""What the dispatcher says, and to whom, when no model provider is usable.

On 2026-10-27 all three frontier providers were unusable at once (one
exhausted, one without authentication, one with a usage reading stale for six
days) and the dispatcher's answer named only the one it happened to check
last. Nobody was told. This module is the one place that builds the answer and
raises the alert:

* ``outage_message`` names EVERY provider's own reason, in one line.
* ``report_outage`` sends one operator notification per outage, through the
  existing ``lib.notify.Notifier``'s repeat-condition dedupe: the first call
  delivers, every later call for the same open outage is suppressed, and
  ``clear_outage`` (called when a provider next works) re-arms it, so an outage
  that recurs after a recovery is reported again and a burst of failing
  dispatches is reported once.
* A wait that ends by itself at a stated time (a cooling window, the weekly
  pace line) is not an outage the operator can act on; ``is_self_clearing``
  says so and the caller does not page for it.

Nothing here reads or copies a credential. A provider without authentication
is reported by name and by what is missing; setting it up is the operator's.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping

import harness_router

log = logging.getLogger(__name__)

# The condition being reported, not this particular send: a key that varied per
# call would defeat the dedupe (see Notifier.send).
OUTAGE_DEDUPE_KEY = "apis:no_provider_usable"


def outage_message(reasons: Mapping[str, str]) -> str:
    """One line naming each provider and why it cannot run."""
    if not reasons:
        return "no subscription-backed harness provider has usable headroom (no reason recorded)"
    listed = "; ".join(f"{name}: {reason}" for name, reason in reasons.items())
    return f"no subscription-backed harness provider has usable headroom — {listed}"


def is_self_clearing(
    providers: Collection[str], *, fly_bound: Collection[str] = ()
) -> bool:
    """True when EVERY listed provider is waiting on a time that ends the wait.

    That is a live cooling window (a stated reset) or the weekly pace line
    (``returns_at``). A stale, missing, malformed or unknown reading, an
    exhausted headroom, a missing binary or a failed login does not clear by
    itself, so an outage containing one is operator-actionable.
    """
    if not providers:
        return False
    for provider in providers:
        if harness_router.persisted_cooling(provider) is not None:
            continue
        gate = (
            None if provider in fly_bound else harness_router.usage_gate(provider)
        )
        if gate is not None and not gate.allowed and gate.code == harness_router.GATE_PACED:
            continue
        return False
    return True


def report_outage(notifier, reasons: Mapping[str, str]) -> bool:
    """Notify the operator once for this outage. Never raises.

    Returns True only when a notification was delivered by this call; False
    when it was suppressed as a repeat of the open outage, when there is no
    notifier, or when delivery failed.
    """
    if notifier is None:
        return False
    try:
        from lib.notify import Priority

        message = (
            "Dispatch is stopped: "
            f"{outage_message(reasons)}. "
            "Sent once per outage; it repeats only after a provider has worked "
            "again and then failed again."
        )
        return bool(
            notifier.send(
                message,
                priority=Priority.BLOCKER,
                handler="apis",
                dedupe_key=OUTAGE_DEDUPE_KEY,
            )
        )
    except Exception as exc:  # noqa: BLE001 — an alert must never break dispatch
        log.debug(f"[apis] provider-outage notification failed: {exc}")
        return False


def clear_outage(notifier) -> None:
    """Re-arm the alert: a provider can run, so the outage is over. Never raises."""
    if notifier is None:
        return
    try:
        notifier.clear_dedupe(OUTAGE_DEDUPE_KEY)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[apis] provider-outage clear failed: {exc}")
