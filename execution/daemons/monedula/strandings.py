"""
strandings.py — Monedula stranding detection and escalation.

A *stranding* is an active payment_profile that Monedula cannot act on. The
profile reads as configuration — correct payee, correct amount, ``status:
active`` — while being inert. Before this module the daemon logged a WARNING
and continued, the run exited clean, and nothing else recorded it: 2,748
``is UNREACHABLE`` warnings across 17 days produced zero escalations, so the
operator was never told a payment had not happened.

Silent stranding is not a safe failure just because it blocks rather than
pays. A payment that cannot fire is still a payment that did not fire.

WHY THIS MODULE EXISTS SEPARATELY FROM THE NOTIFIER
---------------------------------------------------
``lib/notify`` is the right channel for *telling* the operator, but it is not
a durable record and it is not always up. Monedula's own log shows both legs
of the notifier failing for long stretches: ``gws +send`` returning rc=2/rc=4
(DNS) 1,256 times, each falling back to Telegram, and 1,262 rubric-load
failures during the same DNS outages. A fix that escalated only through the
notifier would have been silent for exactly the periods that most needed it.

So a stranding is recorded as a Neotoma ``escalation`` entity *first* — that
is the durable, queryable source of truth an operator or a later audit can
read — and the notifier is a best-effort second leg on top. Neither failing
takes the run down, but the run no longer reports success.

Note this is a different failure from ateles#554. That one is on Telegram's
*read* side: Telegram permits a single ``getUpdates`` consumer per bot token,
and Cyphorhinus holds a permanent long-poll, so Monedula's approval reads get
HTTP 409. Sending is unaffected and needs no separate transport here.

DEDUPLICATION
-------------
Monedula wakes every ~15 minutes. Escalating per occurrence would have
produced 2,748 escalations over the same window — its own incident, and
noise that trains the operator to ignore the channel. The condition is
escalated **per (profile, reason)**, not per occurrence:

  * first observation of a (profile, reason) pair escalates immediately;
  * the same pair stays quiet while it persists unchanged;
  * a *changed* reason for the same profile escalates again (the operator
    fixed one defect and uncovered another — that is new information);
  * an unchanged pair re-escalates only after ``REESCALATE_AFTER_HOURS``, so
    a stranding cannot fade out of view entirely;
  * a pair that stops appearing is cleared, so if it recurs later it is
    reported as new rather than suppressed by a stale record.

State lives beside the daemon's other run state as a small JSON file. It is
advisory: if it is missing or unreadable the worst case is one duplicate
escalation, never a suppressed one — the failure mode points at noise rather
than silence, deliberately.

NOTHING DUE IS NOT A STRANDING
------------------------------
A profile that is perfectly reachable but simply has no payment due today is
normal operation and must never escalate. This module is only ever handed
profiles the loader *rejected*; a reachable profile that no calendar event
matched, or whose due date has not arrived, never reaches this code. Getting
that boundary wrong is what makes an alarm ignorable.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Cloudflare fronts the hosted Neotoma instance and blocks urllib's default
# User-Agent with a 1010 "browser signature" 403. Any explicit UA passes.
NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0"

# An unchanged stranding is re-escalated at most this often. Long enough that
# a persistent defect does not spam the operator every 15 minutes; short
# enough that it cannot sit unnoticed for a week the way the log warnings did.
REESCALATE_AFTER_HOURS = 24

STATE_FILE = Path(__file__).parent / ".monedula_strandings.json"


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stranding:
    """One active profile the daemon cannot act on, and why.

    ``reason`` is a stable machine key used for deduplication and routing;
    ``detail`` is the operator-facing sentence. ``label`` identifies the
    profile to a human. No payee identifiers, amounts, IBANs or addresses are
    carried here — this text reaches a public repo's tests and an escalation
    body, so it names the profile and the defect, never the payment.
    """

    entity_id: str
    label: str
    reason: str
    detail: str

    @property
    def key(self) -> str:
        """Dedup key: the condition, per profile — not the occurrence."""
        return f"{self.entity_id or self.label}:{self.reason}"


# Reasons a profile is unactionable. Each maps to one rejection branch in
# handlers/payment_profile.load_profiles_from_neotoma().
REASON_UNREACHABLE = "unreachable_no_trigger"
REASON_MISSING_LABEL = "missing_label"
REASON_BAD_PAYMENT_TYPE = "unknown_payment_type"
REASON_BAD_AMOUNT = "invalid_amount"
REASON_BAD_LEGAL_TYPE = "invalid_wise_legal_type"
# Not a per-profile defect: the whole profile fetch failed, so *every* profile
# is stranded and the daemon cannot even enumerate them. Logged 1,753 times as
# a bare WARNING and 102 times as an ERROR, and never escalated once.
REASON_FETCH_FAILED = "profile_fetch_failed"


# ---------------------------------------------------------------------------
# Dedup state
# ---------------------------------------------------------------------------


def _load_state(path: Path = STATE_FILE) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        # Missing or corrupt state means "nothing known" — every current
        # stranding escalates once. Erring toward a duplicate rather than a
        # suppression is the deliberate direction for a payment alarm.
        return {}


def _save_state(state: dict, path: Path = STATE_FILE) -> None:
    try:
        path.write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError as exc:
        # Losing the state costs a duplicate escalation next tick, which is
        # strictly better than taking the run down over bookkeeping.
        log.warning(f"could not persist stranding state: {exc}")


def select_new_strandings(
    strandings: list[Stranding],
    *,
    state: dict,
    now: float | None = None,
    reescalate_after_hours: int = REESCALATE_AFTER_HOURS,
) -> tuple[list[Stranding], dict]:
    """Return the strandings that warrant an escalation now, plus new state.

    Pure function — no I/O, no clock of its own — so the dedup rule is
    directly testable. See the module docstring for the rule itself.
    """
    now = time.time() if now is None else now
    cutoff = reescalate_after_hours * 3600
    fresh: list[Stranding] = []
    next_state: dict = {}

    for s in strandings:
        prior = state.get(s.key)
        last_at = prior.get("escalated_at", 0) if isinstance(prior, dict) else 0
        if not prior or (now - last_at) >= cutoff:
            fresh.append(s)
            next_state[s.key] = {"escalated_at": now, "reason": s.reason}
        else:
            # Still stranded, already reported, not yet stale — stay quiet but
            # carry the original timestamp so the interval measures from the
            # first report rather than resetting on every tick.
            next_state[s.key] = {"escalated_at": last_at, "reason": s.reason}

    # Keys absent from this run are dropped, not retained: a condition that
    # cleared and later returns is genuinely new and should be reported again.
    return fresh, next_state


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------


def _post_escalation(entity: dict, idempotency_key: str) -> bool:
    """POST one escalation entity to Neotoma. Returns True on success."""
    base_url = os.environ.get("NEOTOMA_BASE_URL", "").strip().rstrip("/")
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
    if not base_url:
        log.error("NEOTOMA_BASE_URL unset — cannot record stranding escalation")
        return False

    is_loopback = "localhost" in base_url or "127.0.0.1" in base_url
    body = json.dumps(
        {
            "entities": [entity],
            "idempotency_key": idempotency_key,
            "observation_source": "workflow_state",
        }
    ).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if bearer and not is_loopback:
        headers["Authorization"] = f"Bearer {bearer}"

    req = urllib.request.Request(
        f"{base_url}/store", data=body, method="POST", headers=headers
    )
    req.add_header("User-Agent", NEOTOMA_USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # The escalation write itself failing is the one case with no durable
        # record left, so it is logged at ERROR rather than swallowed.
        log.error(f"stranding escalation write FAILED: {exc}")
        return False


def build_escalation_entity(s: Stranding, *, observed_at: str) -> dict:
    """Build the Neotoma escalation entity for one stranding.

    Fields follow the registered `escalation` schema (v1.0): title, body,
    severity, source_agent, source_entity_id, source_entity_type, status, tags.

    `observed_at` is deliberately NOT sent as a field: the arch lens confirmed
    against the live schema that it is undeclared on `escalation`, and an
    undeclared field is an unknown_fields defect rather than tolerated
    pass-through (ateles#599 review). The observation time is not lost — it is
    stated in the body prose above, which is the operator-facing surface, and
    Neotoma stamps its own `observed_at` on ingest.
    """
    return {
        "entity_type": "escalation",
        "title": f"Monedula cannot act on payment profile: {s.label}",
        "body": (
            f"Monedula skipped an ACTIVE payment_profile it cannot act on.\n\n"
            f"Profile: {s.label}\n"
            f"Reason: {s.reason}\n"
            f"Detail: {s.detail}\n\n"
            f"The profile is marked active and reads as ready to pay, but no "
            f"trigger path can fire for it, so the payment has not happened "
            f"and will not happen until this is corrected. This escalation is "
            f"raised once per profile per condition, not once per run.\n\n"
            f"Observed at {observed_at} by the Monedula payment daemon."
        ),
        "severity": "error",
        "source_agent": "monedula@ateles-swarm",
        "source_entity_id": s.entity_id,
        "source_entity_type": "payment_profile",
        "status": "open",
        "tags": ["monedula", "payments", "stranded_profile", s.reason],
    }


def escalate(
    strandings: list[Stranding],
    *,
    notify=None,
    state_file: Path = STATE_FILE,
    now: float | None = None,
) -> list[Stranding]:
    """Escalate newly-observed strandings. Returns those actually escalated.

    Writes a durable Neotoma ``escalation`` per stranding, then notifies
    best-effort. Deduplicated per (profile, reason) — see module docstring.
    """
    if not strandings:
        # Clear state so a condition that returns later reports as new.
        _save_state({}, state_file)
        return []

    state = _load_state(state_file)
    fresh, next_state = select_new_strandings(strandings, state=state, now=now)
    _save_state(next_state, state_file)

    if not fresh:
        log.info(
            f"{len(strandings)} stranded payment profile(s) — already escalated, "
            f"suppressed until {REESCALATE_AFTER_HOURS}h have passed"
        )
        return []

    observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time()))
    for s in fresh:
        entity = build_escalation_entity(s, observed_at=observed_at)
        # Idempotency covers the condition per day, so a retry inside one
        # window cannot create duplicates even if the state file is lost.
        day = observed_at[:10]
        _post_escalation(entity, f"monedula-stranding-{s.key}-{day}")
        log.error(
            f"STRANDED payment profile {s.label!r}: {s.reason} — escalated. {s.detail}"
        )

    if notify is not None:
        labels = ", ".join(s.label for s in fresh)
        try:
            notify(
                f"monedula: {len(fresh)} payment profile(s) STRANDED and cannot be "
                f"paid — {labels}. Escalations filed in Neotoma.",
                priority="blocker",
            )
        except Exception as exc:  # noqa: BLE001 — notification must not fail the run
            log.warning(f"stranding notification failed: {exc}")

    return fresh
