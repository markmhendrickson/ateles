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

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
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

# Neotoma HTTP budget shared by the profile fetch and the escalation write.
#
# Both calls ran with a 15 s timeout and no retry. On 7 and 13 Nov 2026 the
# hosted instance timed out under that budget, so one slow response read as a
# whole-run outage. 30 s per attempt is double the old budget (the hosted
# instance's slow responses are seconds, not minutes) and two attempts with a
# short pause rides out a single slow or dropped response. Worst case is
# 2 x 30 s + 2 s = 62 s per call, well inside the ~15 minute tick, and the
# retry applies only to transport errors, timeouts and HTTP 5xx: a 4xx
# (auth/UA rejection) is deterministic and is reported at once, not retried.
NEOTOMA_TIMEOUT_SEC = 30
NEOTOMA_ATTEMPTS = 2
NEOTOMA_RETRY_PAUSE_SEC = 2

# A profile fetch that fails as a whole is almost always a transient Neotoma
# blip that clears on the next ~15 minute tick, and the run already refuses to
# claim the day or clear consent while it lasts. Escalating on the first
# failure produced an email pointing at a record that did not exist. It is
# escalated only once it has persisted this many consecutive ticks (~30-45
# minutes), long enough to ride out a blip, short enough that a real outage is
# reported well within the same morning.
FETCH_FAILURE_ESCALATE_AFTER_TICKS = 3


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
REASON_BAD_RECIPIENT_ID = "invalid_wise_recipient_id"
# Not a per-profile defect: the whole profile fetch failed, so *every* profile
# is stranded and the daemon cannot even enumerate them. Logged 1,753 times as
# a bare WARNING and 102 times as an ERROR, and never escalated once.
REASON_FETCH_FAILED = "profile_fetch_failed"
# Neotoma ANSWERED but the answer is unusable: an HTTP 4xx (expired or revoked
# token, Cloudflare User-Agent block) or an unparseable body. Unlike an outage
# this never clears by itself, so it escalates at once and keeps its remedy.
REASON_FETCH_REJECTED = "profile_fetch_rejected"
# Both mean "no profiles were loaded"; callers that must not treat that as
# "nothing due" test membership here.
FETCH_REASONS = (REASON_FETCH_FAILED, REASON_FETCH_REJECTED)

# The daemon's wake interval, used only to turn a tick count into minutes in
# operator-facing text.
def _tick_minutes() -> int:
    try:
        v = int(os.environ.get("MONEDULA_TICK_MINUTES", "").strip() or 15)
        return v if v > 0 else 15
    except ValueError:
        return 15


TICK_MINUTES = _tick_minutes()

# Outcomes of an escalation write, each worded differently to the operator.
OUTCOME_CONFIRMED = "confirmed"  # written and read back
OUTCOME_NOT_STORED = "not_stored"  # Neotoma refused it / it was never sent
OUTCOME_UNCONFIRMED = "unconfirmed"  # may have landed; read-back failed
OUTCOME_DEFERRED = "deferred"  # not attempted this tick (budget)


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


def _neotoma_headers(bearer: str, base_url: str) -> dict:
    is_loopback = "localhost" in base_url or "127.0.0.1" in base_url
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if bearer and not is_loopback:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, OSError))


def call_with_retry(fn, label: str):
    """Run ``fn`` with the shared Neotoma budget. Raises the last error."""
    last: BaseException | None = None
    for attempt in range(1, NEOTOMA_ATTEMPTS + 1):
        try:
            return fn()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            if not _retryable(exc) or attempt == NEOTOMA_ATTEMPTS:
                break
            log.warning(
                f"{label} attempt {attempt}/{NEOTOMA_ATTEMPTS} failed "
                f"({type(exc).__name__}); retrying"
            )
            time.sleep(NEOTOMA_RETRY_PAUSE_SEC)
    assert last is not None
    raise last


def _post_escalation(entity: dict, idempotency_key: str) -> str:
    """Write one escalation entity to Neotoma and READ IT BACK.

    Returns one of the OUTCOME_* values. Only OUTCOME_CONFIRMED means the
    record was read back. OUTCOME_NOT_STORED is claimed only when the write was
    never sent or Neotoma answered it with a 4xx; a timeout, a 5xx, a response
    with no entity id, a failed read-back or a mismatch may all follow a write
    that landed, so those are OUTCOME_UNCONFIRMED.
    """
    base_url = os.environ.get("NEOTOMA_BASE_URL", "").strip().rstrip("/")
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
    if not base_url:
        log.error("NEOTOMA_BASE_URL unset — cannot record stranding escalation")
        return OUTCOME_NOT_STORED

    body = json.dumps(
        {
            "entities": [entity],
            "idempotency_key": idempotency_key,
            "observation_source": "workflow_state",
        }
    ).encode("utf-8")
    headers = _neotoma_headers(bearer, base_url)

    def _store():
        req = urllib.request.Request(
            f"{base_url}/store", data=body, method="POST", headers=headers
        )
        req.add_header("User-Agent", NEOTOMA_USER_AGENT)
        with urllib.request.urlopen(req, timeout=NEOTOMA_TIMEOUT_SEC) as resp:
            return json.loads(resp.read() or b"{}")

    try:
        data = call_with_retry(_store, "stranding escalation write")
    except urllib.error.HTTPError as exc:
        log.error(f"stranding escalation write FAILED: HTTP {exc.code}")
        # 4xx: Neotoma answered and refused. 5xx (after retries) may have
        # committed before failing.
        return OUTCOME_NOT_STORED if 400 <= exc.code < 500 else OUTCOME_UNCONFIRMED
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.error(f"stranding escalation write FAILED: {type(exc).__name__}")
        return OUTCOME_UNCONFIRMED

    entity_id = ""
    if isinstance(data, dict):
        for e in data.get("entities") or []:
            if isinstance(e, dict) and e.get("entity_id"):
                entity_id = str(e["entity_id"])
                break
    if not entity_id:
        log.error("stranding escalation write returned no entity id — cannot read back")
        return OUTCOME_UNCONFIRMED

    def _read():
        # The id comes from the server response: quote it so it cannot alter
        # the path the bearer is sent to.
        req = urllib.request.Request(
            f"{base_url}/entities/{urllib.parse.quote(entity_id, safe='')}",
            method="GET",
            headers=headers,
        )
        req.add_header("User-Agent", NEOTOMA_USER_AGENT)
        with urllib.request.urlopen(req, timeout=NEOTOMA_TIMEOUT_SEC) as resp:
            return json.loads(resp.read() or b"{}")

    try:
        back = call_with_retry(_read, "stranding escalation read-back")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.error(f"stranding escalation read-back FAILED: {type(exc).__name__}")
        return OUTCOME_UNCONFIRMED
    snap = {}
    if isinstance(back, dict):
        snap = back.get("snapshot") or (back.get("entity") or {}).get("snapshot") or {}
    if not isinstance(snap, dict) or snap.get("title") != entity.get("title"):
        log.error("stranding escalation did not read back as written")
        return OUTCOME_UNCONFIRMED
    return OUTCOME_CONFIRMED


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
    if s.reason == REASON_FETCH_FAILED:
        title = "Monedula could not load payment profiles: Neotoma unreachable"
    elif s.reason == REASON_FETCH_REJECTED:
        title = "Monedula could not load payment profiles: Neotoma rejected the fetch"
    else:
        title = f"Monedula cannot act on payment profile: {s.label}"
    return {
        "entity_type": "escalation",
        "title": title,
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


def stranded_fingerprint(strandings: list[Stranding]) -> str:
    """Stable Notifier fingerprint of the active stranded set (sorted keys)."""
    keys = sorted(s.key for s in strandings)
    return "|".join(keys)


def _bump_fetch_streak(state_file: Path, failed: bool) -> int:
    """Consecutive ticks the whole-profile fetch has failed (0 when it did not).

    Kept in a sidecar beside the dedup state, like the notify-key sidecar, so
    it survives between ticks (each tick is a fresh process). Advisory: a lost
    file restarts the count, which only delays an escalation by a few ticks.
    """
    path = state_file.with_name(state_file.name + ".fetch_streak")
    if not failed:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        return 0
    try:
        streak = int(json.loads(path.read_text()).get("streak", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        streak = 0
    streak += 1
    try:
        path.write_text(json.dumps({"streak": streak}))
    except OSError as exc:
        log.warning(f"could not persist profile-fetch streak: {exc}")
    return streak


def _alert_text(fresh: list[Stranding], outcomes: dict[str, str], streak: int) -> str:
    """Operator-facing alert. States each escalation write outcome truthfully."""
    stories: list[str] = []
    unreachable = [s for s in fresh if s.reason == REASON_FETCH_FAILED]
    rejected = [s for s in fresh if s.reason == REASON_FETCH_REJECTED]
    profiles = [s for s in fresh if s.reason not in FETCH_REASONS]
    if unreachable:
        stories.append(
            f"monedula: Neotoma has been unreachable for about "
            f"{streak * TICK_MINUTES} minutes; payment profiles could not be "
            "loaded, so none can be proposed from Neotoma. Monedula will keep "
            "retrying; check Neotoma's status if this persists."
        )
    for s in rejected:
        stories.append(
            f"monedula: payment profiles could not be loaded; {s.detail} "
            "None can be proposed from Neotoma. This will not clear by itself."
        )
    if profiles:
        labels = ", ".join(s.label for s in profiles)
        stories.append(
            f"monedula: payment_profiles_stranded — {len(profiles)} profile(s) "
            f"STRANDED and cannot be paid — {labels}."
        )
    by = {k: [s for s in fresh if outcomes.get(s.key) == k] for k in (
        OUTCOME_CONFIRMED, OUTCOME_NOT_STORED, OUTCOME_UNCONFIRMED, OUTCOME_DEFERRED)}
    lines: list[str] = []
    if by[OUTCOME_CONFIRMED]:
        lines.append(
            f"Escalation record written to Neotoma and read back "
            f"({len(by[OUTCOME_CONFIRMED])} of {len(fresh)})."
        )
    if by[OUTCOME_NOT_STORED]:
        lines.append(
            f"Escalation write FAILED for {len(by[OUTCOME_NOT_STORED])} of "
            f"{len(fresh)}: Neotoma refused it or it was never sent, so no record "
            "was created and this alert is the only record. Retrying next tick."
        )
    if by[OUTCOME_UNCONFIRMED]:
        lines.append(
            f"Escalation write could not be CONFIRMED for "
            f"{len(by[OUTCOME_UNCONFIRMED])} of {len(fresh)}: the record may or may "
            "not exist in Neotoma. Retrying next tick."
        )
    if by[OUTCOME_DEFERRED]:
        lines.append(
            f"{len(by[OUTCOME_DEFERRED])} escalation write(s) not attempted this "
            "tick after a failure; retrying next tick."
        )
    if len(stories) > 1:
        stories = [f"• {x}" for x in stories]
    # Blank lines between stories and before the write outcomes, so two
    # different conditions in one message do not read as one.
    return "\n\n".join(stories + (["\n".join(lines)] if lines else []))


def escalate(
    strandings: list[Stranding],
    *,
    notify=None,
    state_file: Path = STATE_FILE,
    now: float | None = None,
    clear_dedupe=None,
    **notify_kwargs,
) -> list[Stranding]:
    """Escalate newly-observed strandings. Returns those actually escalated.

    Writes a durable Neotoma ``escalation`` per stranding (read back), then
    notifies best-effort with the write outcome. Deduplicated per (profile,
    reason) — see module docstring. A whole-profile-fetch failure is held back
    until it persists ``FETCH_FAILURE_ESCALATE_AFTER_TICKS`` consecutive ticks.

    Notifier ``dedupe_key`` is ``monedula:stranded:<sorted keys fp>`` so an
    unchanged stranded set does not re-fire every tick (ateles#1178). Pass
    ``clear_dedupe(key)`` to release a prior key when the set empties or
    changes. Extra ``notify_kwargs`` (e.g. ``email_eligible``) forward to
    ``notify``.
    """
    prior_key_path = state_file.with_name(state_file.name + ".notify_key")
    prior_notify_key = ""
    try:
        prior_notify_key = prior_key_path.read_text().strip()
    except OSError:
        prior_notify_key = ""

    streak = _bump_fetch_streak(
        state_file, any(s.reason == REASON_FETCH_FAILED for s in strandings)
    )
    if streak and streak < FETCH_FAILURE_ESCALATE_AFTER_TICKS:
        log.error(
            f"Neotoma unreachable; payment profiles not loaded this run; retrying "
            f"next tick (profile fetch failed {streak}/{FETCH_FAILURE_ESCALATE_AFTER_TICKS} "
            f"consecutive ticks; escalating if it persists)"
        )
        strandings = [s for s in strandings if s.reason != REASON_FETCH_FAILED]
        if not strandings:
            # Nothing else to report, and an outage says nothing about the
            # dedup state of earlier strandings: leave it untouched.
            return []

    if not strandings:
        # Clear state so a condition that returns later reports as new.
        _save_state({}, state_file)
        if prior_notify_key and clear_dedupe is not None:
            try:
                clear_dedupe(prior_notify_key)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"clear stranded dedupe failed: {exc}")
        try:
            if prior_key_path.exists():
                prior_key_path.unlink()
        except OSError:
            pass
        return []

    fp = stranded_fingerprint(strandings)
    dedupe_key = f"monedula:stranded:{fp}"
    prior_base = prior_notify_key.split("|outcome=", 1)[0]
    if prior_notify_key and prior_base != dedupe_key and clear_dedupe is not None:
        try:
            clear_dedupe(prior_notify_key)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"clear prior stranded dedupe failed: {exc}")

    state = _load_state(state_file)
    fresh, next_state = select_new_strandings(strandings, state=state, now=now)
    _save_state(next_state, state_file)

    if not fresh:
        log.info(
            f"{len(strandings)} stranded payment profile(s) — already escalated, "
            f"suppressed until {REESCALATE_AFTER_HOURS}h have passed"
        )
        # Nothing is sent, so the stamped key stays as last notified (it
        # carries the outcome class of that alert).
        return []

    observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time()))
    outcomes: dict[str, str] = {}
    failed_this_tick = False
    for s in fresh:
        if failed_this_tick:
            # One failed write usually means Neotoma is struggling; do not
            # spend another ~2 minutes of timeouts per remaining stranding.
            outcomes[s.key] = OUTCOME_DEFERRED
            continue
        entity = build_escalation_entity(s, observed_at=observed_at)
        # The key binds the condition, the day AND the content. The body
        # carries a per-tick timestamp, and Neotoma rejects a reused key whose
        # content differs (400), so a same-day retry of a write whose first
        # attempt landed must not reuse the first attempt's key. A different
        # key lands on the same entity (same title), not a duplicate.
        digest = hashlib.sha256(
            json.dumps(entity, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        day = observed_at[:10]
        result = _post_escalation(entity, f"monedula-stranding-{s.key}-{day}-{digest}")
        # Older callers/mocks return a bool.
        if result is True:
            result = OUTCOME_CONFIRMED
        elif result is False:
            result = OUTCOME_NOT_STORED
        outcomes[s.key] = result
        # Defer the rest only after a TRANSIENT outcome. A refused (4xx) write
        # is permanent for that entity and must not starve those behind it.
        failed_this_tick = result == OUTCOME_UNCONFIRMED
        log.error(
            f"STRANDED payment profile: {s.reason} — "
            + ("escalated" if result == OUTCOME_CONFIRMED else f"escalation {result}")
            + f" (ref {s.entity_id or s.reason})"
        )

    # An unconfirmed or failed write must not start the 24h quiet period: with
    # no confirmed record behind it, suppressing the retry would leave the
    # condition unrecorded.
    failed_keys = [k for k, r in outcomes.items() if r != OUTCOME_CONFIRMED]
    if failed_keys:
        for k in failed_keys:
            next_state.pop(k, None)
        _save_state(next_state, state_file)

    if notify is not None:
        message = _alert_text(fresh, outcomes, streak)
        # The Notifier suppresses a repeated key until cleared. The key must
        # say what the alert SAID, not just which profiles are stranded: an
        # alert that reported an unconfirmed write must not suppress the later
        # alert that reports it confirmed.
        outcome_class = "+".join(sorted(set(outcomes.values())))
        new_key = f"{dedupe_key}|outcome={outcome_class}"
        if (
            prior_notify_key
            and prior_notify_key != new_key
            and prior_notify_key.split("|outcome=", 1)[0] == dedupe_key
            and clear_dedupe is not None
        ):
            try:
                clear_dedupe(prior_notify_key)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"clear prior stranded dedupe failed: {exc}")
        dedupe_key = new_key
        try:
            notify(
                message,
                priority="blocker",
                dedupe_key=dedupe_key,
                email_eligible=True,
                **notify_kwargs,
            )
            try:
                prior_key_path.write_text(dedupe_key)
            except OSError:
                pass
        except TypeError:
            # Older notify callables that only accept (msg, priority=).
            try:
                notify(message, priority="blocker")
            except Exception as exc:  # noqa: BLE001
                log.warning(f"stranding notification failed: {exc}")
        except Exception as exc:  # noqa: BLE001 — notification must not fail the run
            log.warning(f"stranding notification failed: {exc}")

    return fresh
