"""
lib/notify/notifier.py — Apprise-backed notification router for Ateles daemons.

Reads a priority_rubric entity from Neotoma at startup.
Routes notifications by priority through Apprise (Telegram-primary).
Respects silence windows. Routine INFO events are never emailed and never
accumulate for a later flush; only actionable held notices (operator_decision /
warn inside the silence window) persist, and they drain as individual deliveries
— never as a bulk "Digest" message.

Held notices go out only by a route the raising send was allowed to use. The
queue is shared by every daemon, and a daemon that drains it is not the one
that raised the notice, so the route is decided when the notice is held:
    main queue   — never emailed (the raising daemon had no email, or the send
                   said ``email_eligible=False``, or it is legacy/unknown);
                   drained only through Apprise
    email queue  — may be emailed, capped at HELD_EMAIL_BUDGET_PER_HOUR across
                   all processes; over budget it waits or goes via Apprise
A notice with no route stays queued, untouched and unlogged per item; the
operator's harness session surfaces it via lib/notify/held_view.py.

Priority levels:
    critical          — immediate, bypasses silence window
    blocker           — send now
    operator_decision — send now (operator must decide); held across silence
    warn              — send now outside silence; held across silence
    info              — logged only (no digest, no queue)

Repeat-condition dedupe (ateles#1127): pass ``dedupe_key`` to ``send()`` for
any alert a daemon re-checks on a poll loop. The first send for a key
delivers; every later send for the same key is suppressed until the caller
calls ``clear_dedupe(key)`` once the condition resolves. Without a key, every
call is delivered independently, same as before.

All times are in the rubric's configured timezone (default: Europe/Madrid).

Retryable held-notice state table (journal x queue x queue lock):
* Journal absent, malformed, stale, or pending: a readable queue is checked
  before insertion; a matching notice repairs its held receipt without a copy.
* Journal held/delivered with a readable queue containing its notice and an
  acquired lock: suppress (the notice is deliverable or already delivered).
* Journal held with a readable queue missing its notice and an acquired lock:
  release the key and retry; a missing queue is the same case.
* Journal held with an unreadable queue and an acquired lock: release the key,
  preserve the queue bytes, and surface the recovery error in held_view.
* Any timed-out queue lock: do not mutate or drain the queue; retry the send.
  A previously held key is checked again when the lock becomes available.
* Failed receipt write: the queued notice survives; retry repairs the receipt.
* Sending is suppressing for the life of its key, including after restart.
  Missing, malformed, or future attempt times become delivered_unconfirmed.
* Explicit transport refusal or an exception before invoking transport restores
  pending/held. An exception inside transport is uncertain and suppresses retry.
* Transport success followed by a failed delivered receipt retains sending
  and returns DELIVERED_RECEIPT_UNKNOWN. A recovery record and direct notice
  make the uncertain outcome visible; only an operator clear permits resend.
* Operator clear of an uncertain key removes its recovery record, receipt, and
  queued copy; a window roll clears the old key and backs up its queued copy.
* Full queue: keep existing bytes and reject the new notice for retry.
Across send, retry, drain, clear, and restart: at most one queued copy per
notice; a held key has a queued notice unless queue recovery is visible; each
threshold has at most one successful transport send per window unless the
operator explicitly clears its uncertain key. A crash mid-send can lose an
alert, but its recovery record makes that loss loud. A window rollover backs
up an expired queued notice before removing it.
All public operations return without raising on recoverable file failures.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import subprocess
import tempfile
import threading
import time as time_mod
import uuid
from collections import Counter
from datetime import datetime, time
from enum import Enum
from pathlib import Path
from typing import Any, Callable

try:
    import apprise

    HAS_APPRISE = True
except ImportError:
    HAS_APPRISE = False

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo  # type: ignore[no-redef]

import httpx

from . import held_view, neotoma_sink

log = logging.getLogger(__name__)

# A drain delivers at most this many held emails per rolling hour, summed over
# every daemon sharing the queue. An overnight backlog drained the moment the
# silence window ended used to arrive as one email per notice, ~1.5s apart.
HELD_EMAIL_BUDGET_PER_HOUR = 5
_HELD_EMAIL_WINDOW_SECONDS = 3600
# Newest items kept per queue; a queue nobody can drain must not grow forever.
HELD_QUEUE_MAX = 500


class SendResult(str, Enum):
    DELIVERED_RECEIPT_UNKNOWN = "delivered_receipt_unknown"


class TransportNotStarted(Exception):
    """Message preparation failed before a transport was invoked."""

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
PRIORITY_RUBRIC_ENTITY_ID = os.environ.get(
    "PRIORITY_RUBRIC_ENTITY_ID", "ent_29ca079940c1e996a8c782f2"
)

# Default rubric if Neotoma is unavailable
_DEFAULT_RUBRIC: dict[str, Any] = {
    "silence_start": "22:00",
    "silence_end": "08:00",
    "timezone": "Europe/Madrid",
    "digest_times": "08:30,20:00",
    "critical_action": "immediate",
    "blocker_action": "30min",
    "operator_decision_action": "24h",
    "info_action": "digest",
}


# The actionable-prefix rule lives in held_view (one definition, shared with
# the stdlib-only viewer the hooks load by path).
_ACTIONABLE_HELD_PREFIXES = held_view.ACTIONABLE_HELD_PREFIXES


class Priority(str, Enum):
    CRITICAL = "critical"
    BLOCKER = "blocker"
    OPERATOR_DECISION = "operator_decision"
    # WARN: degraded-but-running conditions (dispatch failures, skipped work).
    # Delivers immediately outside the silence window; held for later individual
    # delivery inside it. Added because daemons (formica, neotoma-agent, apis
    # a2a) already sent Priority.WARN, which crashed with AttributeError on the
    # very paths meant to report failures.
    WARN = "warn"
    INFO = "info"


class UnreadableDedupeJournal(ValueError):
    """An existing receipt journal cannot establish whether a key was sent."""


def is_actionable_held_notice(message: str) -> bool:
    """True when a persisted queue item is an actionable held notice.

    OPERATOR_DECISION items are prefixed with ⚠️; WARN with ⚠. Routine INFO
    (and any other non-prefixed legacy digest residue) is not actionable.
    """
    return held_view.is_actionable(message)


class Notifier:
    """
    Apprise-backed notification router.

    Instantiate with Notifier.from_neotoma() to load the priority_rubric
    at startup, or Notifier(rubric=...) for testing.
    """

    def __init__(
        self,
        rubric: dict[str, Any] | None = None,
        telegram_bot_token: str | None = None,
        telegram_chat_id: str | None = None,
        telegram_topic_id: str | None = None,
    ) -> None:
        self._rubric = rubric or _DEFAULT_RUBRIC
        self._bot_token = telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._topic_id = telegram_topic_id or os.environ.get(
            "TELEGRAM_TOPIC_MONEDULA", ""
        )
        # E6 (docs/task_execution_loop.md): email is the preferred operator
        # transport; Telegram becomes break-glass. Flag-gated (ATELES_NOTIFY_EMAIL,
        # default off) so live behaviour is unchanged until enabled. Sends via the
        # dedicated swarm address using gws +send (the same address as the run-thread
        # emails); system notifications don't need threading, so the simple helper
        # suffices.
        self._email_primary = os.environ.get("ATELES_NOTIFY_EMAIL", "0") == "1"
        self._operator_email = os.environ.get("OPERATOR_EMAIL", "").strip()
        self._swarm_email = os.environ.get("ATELES_SWARM_EMAIL", "").strip()
        # Delivery target for system notifications. When From (the swarm alias)
        # and To are the *same* Gmail account, Gmail files the message under
        # SENT and never surfaces it in the inbox as UNREAD — so operator-facing
        # alerts silently pile up unseen. ATELES_NOTIFY_TO lets the operator
        # point notifications at a genuinely separate address (or a plus-alias
        # a filter forces to the inbox). Defaults to OPERATOR_EMAIL so behaviour
        # is unchanged until configured.
        self._notify_to = (
            os.environ.get("ATELES_NOTIFY_TO", "").strip() or self._operator_email
        )
        # The digest queue is PERSISTENT and self-flushing.
        #
        # It used to be a plain in-memory list with `flush_digest()` as its only
        # drain — and `flush_digest()` had zero non-test callers anywhere in the
        # tree. Every daemon is a long-lived process, so an OPERATOR_DECISION
        # raised inside the silence window was appended to that list and then
        # never delivered, never persisted, and lost entirely on restart.
        # Measured on apis.log 2026-09-01: 45 "queuing for digest" lines, 0
        # digests ever sent. That is the mechanism behind the "escalation was
        # written somewhere nobody reads" failure (ateles#565, #583) — an
        # auto-fix-exhausted PR pinged the operator into a list that had no
        # reader. Backing it with a file and draining it opportunistically on
        # the next send means a queued escalation survives a restart and leaves
        # the queue without needing a scheduler that does not exist.
        #
        # 2026-09-15 standing_rule: routine INFO digests are forbidden. The
        # file-backed queue remains only for actionable holds across silence;
        # drain classifies first, drops non-actionable residue, and never emits
        # a bulk "📋 Digest" message (those were the noisy [Ateles] Digest emails).
        self._digest_path = held_view.queue_path()
        # Repeat-condition dedupe journal (ateles#1127).
        #
        # A deterministic condition a daemon re-checks on every tick (a
        # consent-channel timeout still open, a gate still failing) used to
        # notify on every tick with no memory of having already said so.
        # Monedula's consent-channel-timeout alert fired 299 times over four
        # days at the launchd poll cadence (~17 min) — the same shape as
        # ateles#1083 (22,878 retries of one deterministic Tyto error), just
        # on the email channel instead of Telegram. Tyto's fix journaled
        # per-file permanent failures inside its own retry state; this is the
        # generalization into the shared notifier so every daemon that routes
        # through Notifier gets it for free, keyed by a caller-supplied
        # ``dedupe_key`` rather than a file path. A key notifies once, then is
        # suppressed until the caller calls ``clear_dedupe(key)`` when the
        # condition resolves — never on a timer, so a genuinely still-open
        # blocker is not rediscovered just because a day passed.
        self._dedupe_path = Path(
            os.environ.get("ATELES_DEDUPE_JOURNAL_PATH", "").strip()
            or Path(tempfile.gettempdir()) / "ateles-notify-dedupe.json"
        )
        # Tyto can complete independent recording watchers on worker threads.
        # Serialize the whole notification path: the persisted held queue is
        # a read-modify-write transaction, and delivery clients are shared too.
        # Per-process only. The queue files are shared across processes and
        # are guarded by flock sidecars (held_view.file_lock).
        self._notification_lock = threading.RLock()
        self._last_unrouted = 0
        self._apprise: Any = None
        if HAS_APPRISE:
            self._apprise = apprise.Apprise()
            if self._bot_token and self._chat_id:
                url = self._build_telegram_url()
                self._apprise.add(url)
                log.info("[notify] Apprise Telegram URL configured.")
        else:
            log.warning(
                "[notify] apprise not installed — notifications will be logged only."
            )
        try:
            self._recover_sending_entries()
        except UnreadableDedupeJournal:
            self._hold_unreadable_journal(None, "", "")

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_neotoma(cls, telegram_topic_env: str | None = None) -> Notifier:
        """Load priority_rubric from Neotoma and construct Notifier.

        ``telegram_topic_env`` names the environment variable holding this
        daemon's Telegram topic (thread) id — e.g. ``"TELEGRAM_TOPIC_TYTO"`` —
        so a daemon's alerts land in its own forum topic instead of the shared
        default. The constructor has always accepted ``telegram_topic_id``, but
        the factory every daemon actually calls did not expose it, so callers
        passing this kwarg crashed with TypeError on startup. Unset or empty
        env falls back to the constructor's default (TELEGRAM_TOPIC_MONEDULA),
        so daemons that call ``from_neotoma()`` with no argument are unchanged.
        """
        rubric = _load_rubric_from_neotoma()
        topic_id = (
            os.environ.get(telegram_topic_env, "").strip()
            if telegram_topic_env
            else ""
        )
        return cls(rubric=rubric, telegram_topic_id=topic_id or None)

    # ── Public API ────────────────────────────────────────────────────────────

    def send(
        self,
        message: str,
        priority: Priority | str = Priority.INFO,
        handler: str = "",
        bypass_silence: bool = False,
        dedupe_key: str | None = None,
        email_eligible: bool = True,
        retryable: bool = False,
        related_dedupe_keys: tuple[str, ...] = (),
    ) -> bool | SendResult:
        """
        Route a notification by priority.

        ``dedupe_key``, when given, identifies the *condition* being reported
        rather than this particular send. The first send for a key delivers
        normally; every later send for the same key while it is still open is
        suppressed (logged only) until the caller calls ``clear_dedupe(key)``
        once the condition resolves. Use a stable key derived from what is
        actually wrong (e.g. ``"monedula:consent_channel_failed"``), not from
        data that varies per tick (a timestamp, a retry count) — a key that
        changes every send defeats the dedupe entirely.

        ``related_dedupe_keys`` are other conditions covered by the same
        retryable notice. They share its pending, held, and delivered outcome.

        ``email_eligible`` (default True, unchanged behaviour) gates the
        email transport specifically, not delivery as a whole. Set it False
        for a notification the operator cannot act on from the message body
        alone — it still reaches Telegram/Apprise (and any durable record the
        caller separately writes, e.g. an escalation entity), it just never
        becomes an inbox item he has no way to resolve by reading it
        (ateles#1127). Something that halts payments or release and needs a
        human decision should stay ``email_eligible=True`` with what he needs
        to decide written into the body — this flag is for "something
        happened, go look elsewhere", not for genuine blockers.

        Returns True if sent immediately, False if held, dropped, suppressed
        as a duplicate, or undelivered.
        """
        try:
            if retryable and dedupe_key:
                # The sender thread owns this lock through the transport outcome.
                # A timed-out caller cannot make a live pending send retryable.
                with held_view.file_lock(
                    self._dedupe_owner_lock_path(dedupe_key), blocking=False,
                    fail_open=False,
                ) as owned:
                    if not owned:
                        return False
                    with self._notification_lock:
                        return self._send_locked(
                            message, priority, handler, bypass_silence, dedupe_key,
                            email_eligible, retryable, related_dedupe_keys,
                        )
            with self._notification_lock:
                return self._send_locked(
                    message, priority, handler, bypass_silence, dedupe_key, email_eligible,
                    retryable, related_dedupe_keys,
                )
        except UnreadableDedupeJournal:
            if dedupe_key:
                self._hold_unreadable_journal(dedupe_key, message, handler)
            return False
        except Exception as exc:  # noqa: BLE001 — a notifier must not break its caller
            log.error("[notify] send failed; notice remains retryable (%s)", exc)
            return False

    def _dedupe_owner_lock_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._dedupe_path.with_name(f"{self._dedupe_path.name}.{digest}.lock")

    def _send_locked(
        self,
        message: str,
        priority: Priority | str,
        handler: str,
        bypass_silence: bool,
        dedupe_key: str | None = None,
        email_eligible: bool = True,
        retryable: bool = False,
        related_dedupe_keys: tuple[str, ...] = (),
    ) -> bool:
        prio = Priority(priority) if isinstance(priority, str) else priority
        tag = f"[{handler}] " if handler else ""
        full_message = f"{tag}{message}"

        # Also store the alert in Neotoma for the operator's harness session
        # (lib/notify/neotoma_sink.py). Additive and fail-open: it never
        # changes routing or the return value and never raises or blocks.
        try:
            neotoma_sink.record(
                message=message,
                level=prio.value,
                handler=handler,
                dedupe_key=dedupe_key,
            )
        except Exception:  # noqa: BLE001
            pass

        replacement_message = (
            f"⚠ {full_message}" if prio == Priority.WARN else
            f"⚠️ {full_message}" if prio == Priority.OPERATOR_DECISION else None
        )
        if dedupe_key and self._is_duplicate(
            dedupe_key, pending_owned=retryable,
            replacement_message=replacement_message,
        ):
            log.info(
                "[notify] suppressing repeat notification for open condition "
                "%r (call clear_dedupe() once it resolves): %r",
                dedupe_key,
                full_message[:200],
            )
            return False
        if retryable and any(self._is_duplicate(key, pending_owned=True)
                             for key in related_dedupe_keys):
            return False

        # Record the key as reported BEFORE routing, not after. The dedupe
        # contract is "have I already told the operator about this open
        # condition", which is true the moment this call is let through —
        # regardless of whether the priority ends up held, queued, or fails
        # to deliver. Recording only after a successful _deliver() would let
        # a held OPERATOR_DECISION/WARN re-queue on every tick while it sits
        # in the digest, reproducing the same storm this exists to stop.
        #
        # INFO is the one exception, and it is not a hedge: INFO is dropped
        # unconditionally below — never delivered, never held, never queued.
        # Marking the key for an INFO send would suppress every later send of
        # that condition, including a BLOCKER, on the strength of a report the
        # operator never received. No current caller pairs dedupe_key with
        # INFO; this keeps that from becoming a silent footgun.
        if dedupe_key and prio != Priority.INFO:
            if retryable:
                for key in (dedupe_key, *related_dedupe_keys):
                    if not self._mark_dedupe_pending(key, handler):
                        log.error("[notify] could not reserve dedupe key %r; delivery deferred", key)
                        return False
            else:
                self._mark_dedupe_notified(dedupe_key, handler)

        def delivered(result: bool) -> bool | SendResult:
            if retryable and dedupe_key:
                keys = (dedupe_key, *related_dedupe_keys)
                if result:
                    if not self._mark_dedupe_delivered_many(keys, handler):
                        log.error("[notify] delivered, receipt unknown for %r; recovery required", dedupe_key)
                        for key in keys:
                            self._raise_recovery(key, "delivered receipt write failed")
                        return SendResult.DELIVERED_RECEIPT_UNKNOWN
                else:
                    self._mark_dedupe_pending_many(keys, handler)
            return result

        def deliver_now(text: str, force: bool) -> bool | SendResult:
            if retryable and dedupe_key:
                if not self._mark_dedupe_sending((dedupe_key, *related_dedupe_keys), handler):
                    log.error("[notify] could not record sending state for %r; delivery deferred", dedupe_key)
                    return False
            try:
                accepted = self._deliver(text, force=force, email_eligible=email_eligible)
            except TransportNotStarted as exc:
                log.warning("[notify] delivery not started: %s", exc)
                accepted = False
            except Exception as exc:  # noqa: BLE001 — transport may have accepted
                if retryable and dedupe_key:
                    self._mark_delivery_uncertain(
                        (dedupe_key, *related_dedupe_keys),
                        f"transport outcome unknown: {type(exc).__name__}",
                    )
                    return False
                log.warning("[notify] delivery outcome unknown: %s", exc)
                return False
            if (not accepted and retryable and dedupe_key
                    and dedupe_key.startswith(held_view.UNREADABLE_USAGE_KEY_PREFIX)
                    and prio == Priority.WARN):
                held = f"{'⚠' if prio == Priority.WARN else '⚠️'} {full_message}"
                self._queue_digest(
                    held, email_eligible, (dedupe_key, *related_dedupe_keys), handler,
                )
                return False
            return delivered(accepted)

        # Drain any prior held actionable notices before handling this send.
        self._maybe_flush_digest()

        if prio == Priority.CRITICAL:
            # Critical always fires immediately, even in silence window
            return deliver_now(full_message, True)

        if prio == Priority.BLOCKER:
            if self._in_silence_window() and not bypass_silence:
                log.info(
                    "[notify] Blocker in silence window — delivering anyway (blocker policy)"
                )
            return deliver_now(full_message, True)

        if prio == Priority.OPERATOR_DECISION:
            if self._in_silence_window() and not bypass_silence:
                log.info(
                    "[notify] Operator decision in silence window — holding for later delivery"
                )
                held_message = f"⚠️ {full_message}"
                queued = self._queue_digest(
                    held_message, email_eligible,
                    (dedupe_key, *related_dedupe_keys) if retryable and dedupe_key else (),
                    handler,
                )
                if not queued and retryable and dedupe_key:
                    for key in (dedupe_key, *related_dedupe_keys):
                        self.release_dedupe(key)
                return False
            return deliver_now(f"⚠️ {full_message}", False)

        if prio == Priority.WARN:
            if self._in_silence_window() and not bypass_silence:
                held_message = f"⚠ {full_message}"
                queued = self._queue_digest(
                    held_message, email_eligible,
                    (dedupe_key, *related_dedupe_keys) if retryable and dedupe_key else (),
                    handler,
                )
                if not queued and retryable and dedupe_key:
                    for key in (dedupe_key, *related_dedupe_keys):
                        self.release_dedupe(key)
                return False
            return deliver_now(f"⚠ {full_message}", False)

        # INFO — never email, never queue, never flush later as a digest.
        log.debug("[notify] Dropping routine INFO (no digest): %r", full_message)
        return False

    # ── Held-notice queue (file-backed; actionable only) ─────────────────────

    @property
    def _email_path(self) -> Path:
        # Derived on each use: callers (and tests) repoint _digest_path.
        return held_view.email_queue_path(self._digest_path)

    def _read(self, path: Path) -> list[str | dict]:
        """Read a queue file. Fail-open: unreadable state => empty."""
        try:
            return held_view.read_queue(path)
        except Exception as exc:  # noqa: BLE001 — never crash a notification
            log.warning("[notify] held queue %s unreadable (%s) — treating as empty", path.name, exc)
            return []

    @property
    def _digest_queue(self) -> list[str | dict]:
        """The main (never-emailed) queue. Property name kept for compat callers."""
        with self._notification_lock:
            return self._read(self._digest_path)

    @property
    def _email_queue(self) -> list[str | dict]:
        with self._notification_lock:
            return self._read(self._email_path)

    def _persist_queue(self, items: list[str | dict], path: Path | None = None) -> bool:
        """Atomically rewrite a queue file. Caller must hold the file lock."""
        path = path or self._digest_path
        try:
            held_view.write_queue(path, items)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("[notify] could not persist held queue %s (%s)", path.name, exc)
            return False

    def _queue_lock(self):
        return held_view.file_lock(held_view.queue_lock_path(self._digest_path), fail_open=False)

    def _queue_digest(
        self, message: str, email_eligible: bool = True,
        dedupe_keys: tuple[str, ...] = (), handler: str = "",
        record_receipt: bool = True,
        unreadable_source: bool = False,
    ) -> bool:
        """Append an actionable held notice. Never raises.

        The route is fixed here, by the send that raised the notice: only a
        daemon with email armed, for a send that allowed email, may put a
        notice on the email queue. Everything else is never emailed by
        whichever daemon happens to drain it.
        """
        path = (
            self._email_path
            if self._email_primary and email_eligible
            else self._digest_path
        )
        with self._notification_lock:
            try:
                with self._queue_lock() as locked:
                    if not locked:
                        log.error("[notify] held queue lock unavailable; notice remains retryable")
                        return False
                    # A failed read must never turn an existing queue into an
                    # empty one. Missing files alone mean an empty queue.
                    # A symlink is not a queue file; atomic_write safely
                    # replaces it without following its target.
                    items = [] if path.is_symlink() else held_view.read_queue(path)
                    other_path = self._email_path if path == self._digest_path else self._digest_path
                    other_items = held_view.read_queue(other_path)
                    # Only a KEYED notice has an identity that survives its text: a
                    # keyless notice is an independent event, so a recurrence after
                    # its condition cleared queues again (main's contract, pinned by
                    # test_ownership_warning_lifecycle).
                    identity = tuple(sorted(dedupe_keys)) if dedupe_keys else None
                    for existing_path, queue in (
                        ((path, items), (other_path, other_items)) if dedupe_keys else ()
                    ):
                        for index, item in enumerate(queue):
                            if held_view.notice_identity(item) != identity:
                                continue
                            # A changed reading replaces its predecessor under
                            # the queue lock, even if its receipt never landed.
                            replacement = {"keys": list(identity), "message": message} if dedupe_keys else message
                            if isinstance(replacement, dict) and unreadable_source:
                                replacement["unreadable_source"] = True
                            if item != replacement:
                                queue[index] = replacement
                                if not self._persist_queue(queue, existing_path):
                                    return False
                            return not dedupe_keys or not record_receipt or self._mark_dedupe_held(
                                dedupe_keys, handler, message
                            )
                    if len(items) >= HELD_QUEUE_MAX:
                        log.error("[notify] held queue %s full; preserving existing notices", path.name)
                        return False
                    notice = {"keys": list(identity), "message": message} if dedupe_keys else message
                    if isinstance(notice, dict) and unreadable_source:
                        notice["unreadable_source"] = True
                    items.append(notice)
                    if not self._persist_queue(items, path):
                        return False
                    # The queue lock excludes drainers until the held receipt
                    # is recorded. A failed journal write leaves a visible
                    # notice, never a held key without one. Evicted receipts
                    # are reconciled even when this new receipt cannot be made.
                    if dedupe_keys and record_receipt and not self._mark_dedupe_held(
                        dedupe_keys, handler, message
                    ):
                        return False
                    return True
            except Exception as exc:  # noqa: BLE001
                # Losing the queue write must not lose the alert: say so loudly.
                log.error(
                    "[notify] could not persist held notice (%s) — message not "
                    "queued and will NOT be delivered: %r",
                    exc,
                    message[:200],
                )
                return False

    def classify_queue(
        self, items: list[str] | None = None
    ) -> tuple[list[str], list[str]]:
        """Split queue items into (actionable, routine).

        Actionable held notices are preserved for individual delivery. Routine
        INFO / activity listings are discarded so they cannot form a digest.
        """
        actionable: list[str] = []
        routine: list[str] = []
        for item in items if items is not None else self._digest_queue:
            if is_actionable_held_notice(item):
                actionable.append(item)
            else:
                routine.append(item)
        return actionable, routine

    # Held-email budget: one JSON object {"window_start": epoch, "count": n}.
    # Persisted beside the queue so it binds every daemon, not each process.

    def _wall_clock(self) -> float:
        return time_mod.time()

    def _budget_path(self) -> Path:
        return held_view.budget_path(self._digest_path)

    def _budget_state(self) -> tuple[float, int]:
        """Current (window_start, count), rolled over if the window has passed."""
        now = self._wall_clock()
        try:
            raw = json.loads(held_view.read_text_nofollow(self._budget_path()))
            start, count = float(raw["window_start"]), int(raw["count"])
        except FileNotFoundError:
            return now, 0
        except Exception as exc:  # noqa: BLE001 — corrupt state => fresh window
            log.warning("[notify] held-email budget unreadable (%s) — resetting", exc)
            return now, 0
        # NaN never compares as elapsed and a negative count would raise the
        # cap: neither is a state this code writes, so start over.
        if not math.isfinite(start) or count < 0:
            log.warning("[notify] held-email budget invalid — resetting")
            return now, 0
        if now - start >= _HELD_EMAIL_WINDOW_SECONDS or start > now:
            return now, 0
        return start, count

    def _budget_remaining(self) -> int:
        with self._queue_lock() as locked:
            if not locked:
                return 0
            return max(0, HELD_EMAIL_BUDGET_PER_HOUR - self._budget_state()[1])

    def _budget_consume(self) -> None:
        with self._queue_lock() as locked:
            if not locked:
                log.error("[notify] held-email budget lock unavailable")
                return
            start, count = self._budget_state()
            try:
                held_view.atomic_write(
                    self._budget_path(),
                    json.dumps({"window_start": start, "count": count + 1}),
                )
            except Exception as exc:  # noqa: BLE001
                log.error("[notify] could not persist held-email budget (%s)", exc)

    def _remove_delivered(self, path: Path, delivered: list[str]) -> None:
        """Drop delivered items from a queue, by multiset, from a fresh read.

        Re-reading under the lock is what lets items another process queued
        during the delivery survive, and keeps delivered ones from returning.
        """
        if not delivered:
            return
        with self._queue_lock() as locked:
            if not locked:
                log.error("[notify] held queue lock unavailable; delivered item retained")
                return
            gone = Counter(held_view.notice_identity(item) for item in delivered)
            kept: list[str] = []
            removed: list[str] = []
            try:
                items = held_view.read_queue(path)
            except Exception as exc:  # noqa: BLE001
                log.warning("[notify] held queue %s unreadable (%s)", path.name, exc)
                return
            for item in items:
                identity = held_view.notice_identity(item)
                if gone[identity] > 0:
                    gone[identity] -= 1
                    removed.append(item)
                else:
                    kept.append(item)
            if not removed or self._persist_queue(kept, path):
                for item in delivered:
                    self._mark_held_message_delivered(item)

    def _drop_routine(self, path: Path) -> list[str] | None:
        """Remove non-actionable residue from a queue; return what remains."""
        with self._queue_lock() as locked:
            if not locked:
                log.error("[notify] held queue lock unavailable; drain deferred")
                return None
            try:
                items = held_view.read_queue(path)
            except Exception as exc:  # noqa: BLE001
                log.warning("[notify] held queue %s unreadable (%s)", path.name, exc)
                return None
            actionable, routine = self.classify_queue(items)
            if routine:
                log.info(
                    "[notify] dropping %d routine digest item(s); preserving %d actionable",
                    len(routine),
                    len(actionable),
                )
                if self._persist_queue(actionable, path):
                    self._release_held_messages(
                        item for item in routine if item not in actionable
                    )
            return actionable

    def flush_digest(self) -> bool:
        """Drain the held queues without sending a bulk digest.

        Routine INFO residue is dropped; actionable held notices are delivered
        one-by-one, each only by a route its raising send allowed (see module
        docstring). Delivery happens outside the file lock; afterwards only the
        delivered items are removed, so concurrent writers lose nothing. A
        second process already draining makes this one skip rather than
        double-send. Never emits a ``📋 Digest`` body.
        """
        try:
            with self._notification_lock:
                with held_view.file_lock(
                    held_view.drain_lock_path(self._digest_path), blocking=False,
                    fail_open=False,
                ) as free:
                    if not free:
                        return False
                    return self._drain()
        except UnreadableDedupeJournal:
            self._hold_unreadable_journal(None, "", "")
            return False
        except Exception as exc:  # noqa: BLE001
            log.error("[notify] held drain failed; queue preserved (%s)", exc)
            return False

    def _drain(self) -> bool:
        main_items = self._drop_routine(self._digest_path)
        email_items = self._drop_routine(self._email_path)
        if main_items is None or email_items is None:
            return False
        if not main_items and not email_items:
            self._last_unrouted = 0
            return False

        delivered_main: list[str] = []
        delivered_email: list[str] = []
        unrouted = 0
        failed = 0
        try:
            self._dedupe_journal
            journal_readable = True
        except UnreadableDedupeJournal:
            journal_readable = False
            self._hold_unreadable_journal(None, "", "")

        for item in main_items:
            if isinstance(item, dict) and not journal_readable:
                continue
            if self._held_already_delivered(item):
                delivered_main.append(item)
                continue
            # Main-queue items are never emailed, whatever this daemon's flags.
            if not self._apprise:
                unrouted += 1
            elif not self._begin_held_delivery(item):
                continue
            else:
                outcome = self._try_deliver(held_view.notice_text(item), email_eligible=False)
                if outcome is None:
                    self._mark_held_uncertain(item)
                elif outcome:
                    if self._finish_held_delivery(item):
                        delivered_main.append(item)
                else:
                    self._restore_held_delivery(item)
                    failed += 1

        budget = self._budget_remaining() if self._email_primary and self._notify_to else 0
        email_down = False
        for item in email_items:
            if isinstance(item, dict) and not journal_readable:
                continue
            if self._held_already_delivered(item):
                delivered_email.append(item)
                continue
            if not (self._email_primary and self._notify_to and budget > 0 or self._apprise):
                unrouted += 1
                continue
            if not self._begin_held_delivery(item):
                continue
            if budget > 0 and not email_down:
                try:
                    email_accepted = self._deliver_email(held_view.notice_text(item))
                except TransportNotStarted:
                    email_accepted = False
                except Exception as exc:  # noqa: BLE001 — acceptance is unknown
                    log.warning("[notify] held email outcome unknown: %s", exc)
                    self._mark_held_uncertain(item)
                    continue
                if email_accepted:
                    # Only a sent email spends budget.
                    self._budget_consume()
                    budget -= 1
                    if self._finish_held_delivery(item):
                        delivered_email.append(item)
                    continue
                email_down = True  # don't hammer a failing transport per item
            if self._apprise:
                outcome = self._try_deliver(held_view.notice_text(item), email_eligible=False)
                if outcome is None:
                    self._mark_held_uncertain(item)
                elif outcome:
                    if self._finish_held_delivery(item):
                        delivered_email.append(item)
                else:
                    self._restore_held_delivery(item)
                    failed += 1
            elif email_down:
                self._restore_held_delivery(item)
                failed += 1
            else:
                self._restore_held_delivery(item)
                unrouted += 1  # over budget, nothing else to send it by

        self._remove_delivered(self._digest_path, delivered_main)
        self._remove_delivered(self._email_path, delivered_email)

        # One line, and only when the picture changed: a daemon with no
        # transport re-evaluates the queue on every send.
        if unrouted != self._last_unrouted:
            log.info("[notify] %d held notice(s) have no delivery route here", unrouted)
        self._last_unrouted = unrouted
        if failed:
            log.warning("[notify] held-notice delivery incomplete — %d item(s) kept", failed)
        return bool(delivered_main or delivered_email)

    def _begin_held_delivery(self, item: str | dict) -> bool:
        if not isinstance(item, dict):
            return True
        keys = tuple(item["keys"])
        entries = self._dedupe_journal
        for key in keys:
            existing = entries.get(key)
            if key in entries and held_view.receipt_problem(existing):
                self._raise_recovery(key, "malformed held receipt")
                return False
            if existing is None and self._is_duplicate(key):
                return False
        entry = entries.get(keys[0], {})
        if isinstance(entry, dict) and entry.get("state") == "delivered":
            return False
        if isinstance(entry, dict) and entry.get("state") in ("sending", "delivered_unconfirmed"):
            self._is_duplicate(keys[0])
            return False
        return self._mark_dedupe_sending(keys, entry.get("handler", "") if isinstance(entry, dict) else "")

    def _held_already_delivered(self, item: str | dict) -> bool:
        if not isinstance(item, dict):
            return False
        entry = self._dedupe_journal.get(item["keys"][0], {})
        return isinstance(entry, dict) and not held_view.receipt_problem(entry) \
            and entry.get("state") == "delivered"

    def _finish_held_delivery(self, item: str | dict) -> bool:
        if not isinstance(item, dict):
            return True
        keys = tuple(item["keys"])
        entry = self._dedupe_journal.get(keys[0], {})
        if not self._mark_dedupe_delivered_many(keys, entry.get("handler", "") if isinstance(entry, dict) else ""):
            log.error("[notify] held notice delivered, receipt unknown for %r; recovery required", keys[0])
            for key in keys:
                self._raise_recovery(key, "held delivered receipt write failed")
            return False
        return True

    def _restore_held_delivery(self, item: str | dict) -> None:
        if isinstance(item, dict):
            keys = tuple(item["keys"])
            entry = self._dedupe_journal.get(keys[0], {})
            self._mark_dedupe_held(keys, entry.get("handler", "") if isinstance(entry, dict) else "",
                                   item["message"])

    def _mark_held_uncertain(self, item: str | dict) -> None:
        if isinstance(item, dict):
            self._mark_delivery_uncertain(tuple(item["keys"]), "held transport outcome unknown")

    def _mark_delivery_uncertain(self, keys: tuple[str, ...], cause: str) -> None:
        def change(journal: dict[str, Any]) -> bool:
            for key in keys:
                current = journal.get(key)
                if isinstance(current, dict) and current.get("state") == "sending":
                    journal[key] = {**current, "state": "delivered_unconfirmed"}
            return True

        self._update_dedupe_journal(change)
        for key in keys:
            self._raise_recovery(key, cause)

    def _try_deliver(self, item: str, email_eligible: bool) -> bool | None:
        try:
            return bool(self._deliver(item, force=True, email_eligible=email_eligible))
        except TransportNotStarted as exc:
            log.warning("[notify] held delivery not started: %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001 — a drain must never raise
            log.warning("[notify] held-notice delivery error: %s", exc)
            return None

    def _maybe_flush_digest(self) -> None:
        """Drain held actionable notices opportunistically.

        Outside the silence window (or at legacy digest_times windows used as
        release moments), deliver held actionable items individually. Never
        self-flushes a bulk digest of accumulated INFO.
        """
        try:
            if not self._digest_queue and not self._email_queue:
                return
            if self.should_flush_digest() or not self._in_silence_window():
                self.flush_digest()
        except Exception as exc:  # noqa: BLE001 — a drain must never break a send
            log.warning("[notify] opportunistic held-notice flush failed: %s", exc)

    def should_flush_digest(self) -> bool:
        """True if current time matches a release window (within 5 min).

        Named for historical callers; windows release held actionable notices,
        not routine digests.
        """
        now = self._now_local()
        for t_str in self._rubric.get("digest_times", "08:30,20:00").split(","):
            t_str = t_str.strip()
            if not t_str:
                continue
            try:
                h, m = map(int, t_str.split(":"))
                digest_time = time(h, m)
                delta_minutes = abs(
                    (now.hour * 60 + now.minute)
                    - (digest_time.hour * 60 + digest_time.minute)
                )
                if delta_minutes <= 5:
                    return True
            except ValueError:
                continue
        return False

    # ── Repeat-condition dedupe journal (ateles#1127) ─────────────────────────

    @property
    def _dedupe_journal(self) -> dict[str, Any]:
        """Read the persisted dedupe journal; unreadable denies keyed sends.

        Shape: ``{key: {"notified_at": iso8601, "handler": str}}``. A key
        present here has already been reported and is suppressed on every
        later ``send(dedupe_key=key)`` until ``clear_dedupe(key)`` removes it.
        """
        with self._notification_lock:
            try:
                raw = json.loads(held_view.read_text_nofollow(self._dedupe_path))
                if not isinstance(raw, dict):
                    raise ValueError("journal is not an object")
                return raw
            except FileNotFoundError:
                return {}
            except Exception as exc:  # noqa: BLE001
                self._preserve_unreadable_journal()
                log.warning("[notify] dedupe journal unreadable (%s); keyed sends held", type(exc).__name__)
                raise UnreadableDedupeJournal("dedupe journal unreadable") from exc

    def _preserve_unreadable_journal(self) -> None:
        """Keep bounded forensic copies without replacing the first version."""
        try:
            fd = os.open(str(self._dedupe_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as source:
                raw = source.read()
            held_view.preserve_unreadable(self._dedupe_path, raw)
        except OSError:
            pass  # The original remains in place even if copying is denied.

    def _hold_unreadable_journal(self, key: str | None, message: str, handler: str) -> None:
        """Surface repair once and keep each keyed notice until reconciliation."""
        repair = ("Dedupe journal unreadable. Inspect the preserved journal, reconcile "
                  "unknown deliveries, then run python3 lib/notify/held_view.py "
                  "--clear-unreadable-journal.")
        self._raise_recovery("dedupe-journal-unreadable", repair, notify=False)
        self._queue_digest(
            f"⚠ [notify] {repair}", False, ("dedupe-journal-unreadable",),
            "notify", record_receipt=False,
        )
        if key:
            self._queue_digest(
                f"⚠ [{handler or 'notify'}] {message}", False, (key,),
                handler, record_receipt=False, unreadable_source=True,
            )

    def _dedupe_journal_lock_path(self) -> Path:
        return self._dedupe_path.with_name(f"{self._dedupe_path.name}.lock")

    def _update_dedupe_journal(
        self, change: Callable[[dict[str, Any]], bool]
    ) -> bool:
        """Apply one change to a fresh disk read under the shared journal lock."""
        with self._notification_lock:
            with held_view.file_lock(
                self._dedupe_journal_lock_path(), fail_open=False
            ) as locked:
                if not locked:
                    return False
                journal = self._dedupe_journal
                if not change(journal):
                    return True
                return self._persist_dedupe_journal(journal)

    def _persist_dedupe_journal(self, journal: dict[str, Any]) -> bool:
        """Atomically rewrite the dedupe journal. Caller holds its sidecar lock."""
        try:
            self._dedupe_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._dedupe_path.with_suffix(".tmp")
            if journal:
                tmp.write_text(json.dumps(journal))
                tmp.replace(self._dedupe_path)
                return True
            else:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._dedupe_path.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "[notify] could not clear empty dedupe journal: %s", exc
                    )
                    return False
                return True
        except Exception as exc:  # noqa: BLE001
            # The caller must keep a keyed notice held or retryable; a failed
            # receipt write is never evidence that transport may run again.
            log.error("[notify] could not persist dedupe journal (%s)", exc)
            return False

    def _raise_recovery(self, key: str, cause: str, *, notify: bool = True) -> None:
        """Persist uncertain delivery independently of the dedupe receipt."""
        try:
            with held_view.file_lock(held_view.recovery_lock_path(self._digest_path),
                                     fail_open=False) as locked:
                if not locked:
                    log.error("[notify] recovery lock unavailable for %r", key)
                    return
                records = held_view.read_recovery(self._digest_path)
                row = next((row for row in records if row["key"] == key), None)
                if row is None:
                    parts = key.split(":")
                    window = ":".join(parts[1:-1]) if len(parts) >= 4 else key
                    row = {"key": key, "window": window,
                           "time": self._now_local().isoformat(), "cause": cause}
                    records.append(row)
                    held_view.write_recovery(self._digest_path, records)
                if not notify or row.get("notice_started"):
                    return
                # This route never uses the held queue or email. The recovery
                # file remains visible even if this direct transport refuses.
                message = (f"Unconfirmed alert {key}: {cause}. Review with python3 "
                           "lib/notify/held_view.py before deciding whether to resend.")
                row["notice_started"] = True
                held_view.write_recovery(self._digest_path, records)
                if self._try_deliver(message, email_eligible=False):
                    row["noticed"] = True
                    held_view.write_recovery(self._digest_path, records)
        except Exception as exc:  # noqa: BLE001
            log.error("[notify] could not record uncertain alert %r: %s", key, exc)

    def _recover_sending_entries(self) -> None:
        try:
            entries = self._dedupe_journal.items()
        except UnreadableDedupeJournal:
            self._hold_unreadable_journal(None, "", "")
            return
        for key, entry in entries:
            if key == held_view.RECOVERED_AT:
                continue
            if held_view.receipt_problem(entry):
                if isinstance(entry, dict) and entry.get("state") in (
                    "sending", "delivered_unconfirmed"
                ):
                    self._recover_sending(key, entry, "malformed sending receipt")
                else:
                    self._raise_recovery(key, "malformed receipt")
                continue
            if isinstance(entry, dict) and entry.get("state") in (
                "sending", "delivered_unconfirmed"
            ):
                with held_view.file_lock(self._dedupe_owner_lock_path(key),
                                         blocking=False, fail_open=False) as available:
                    if available:
                        self._recover_sending(key, entry, "send interrupted before receipt")

    def _recover_sending(self, key: str, entry: dict, cause: str) -> None:
        attempt = entry.get("attempt_at")
        try:
            parsed = datetime.fromisoformat(attempt) if isinstance(attempt, str) else None
            invalid = parsed is None or parsed > self._now_local()
        except (TypeError, ValueError, OverflowError):
            invalid = True
        if invalid:
            def normalize(journal: dict[str, Any]) -> bool:
                current = journal.get(key)
                if not isinstance(current, dict) or current.get("state") not in (
                    "sending", "delivered_unconfirmed"
                ):
                    return False
                journal[key] = {**current, "state": "delivered_unconfirmed",
                                "attempt_at": self._now_local().isoformat()}
                return True
            self._update_dedupe_journal(normalize)
            cause = "unreadable or future send attempt time"
        self._raise_recovery(key, cause)

    def _is_duplicate(self, key: str, *, pending_owned: bool = False,
                      replacement_message: str | None = None) -> bool:
        journal = self._dedupe_journal
        entry = journal.get(key)
        try:
            recovery = held_view.read_recovery(self._digest_path)
        except Exception:
            log.error("[notify] recovery file unreadable; key %r held", key)
            return True
        if any(row["key"] == key for row in recovery):
            return True
        if key in journal and held_view.receipt_problem(entry):
            if isinstance(entry, dict) and entry.get("state") in (
                "sending", "delivered_unconfirmed"
            ):
                self._recover_sending(key, entry, "malformed sending receipt")
            else:
                self._raise_recovery(key, "malformed receipt")
            return True
        epoch = journal.get(held_view.RECOVERED_AT)
        if entry is None and held_view.RECOVERED_AT in journal and (
            not isinstance(epoch, str) or not held_view.recovered_window_is_new(key, epoch)
        ):
            attempt_at = epoch if isinstance(epoch, str) and not held_view.receipt_problem(
                {"state": "delivered_unconfirmed", "attempt_at": epoch}
            ) else self._now_local().isoformat()
            def quarantine(current: dict[str, Any]) -> bool:
                if key in current:
                    return False
                current[key] = {"state": "delivered_unconfirmed", "attempt_at": attempt_at,
                                "handler": ""}
                return True
            self._update_dedupe_journal(quarantine)
            self._raise_recovery(key, "key absent after journal recovery")
            return True
        if pending_owned and (entry is None or (
            isinstance(entry, dict) and entry.get("state") == "pending"
        )):
            # A queue write can succeed while its held receipt fails. The
            # pending owner must adopt that notice before a direct resend or
            # an opportunistic drain can produce a second delivery.
            with self._queue_lock() as locked:
                if not locked:
                    return True
                try:
                    queues = {path: held_view.read_queue(path)
                              for path in (self._digest_path, self._email_path)}
                except Exception as exc:  # noqa: BLE001
                    log.error("[notify] held queue unreadable for %r: %s", key, exc)
                    return True
                for path, items in queues.items():
                    for index, item in enumerate(items):
                        if isinstance(item, dict) and key in item["keys"]:
                            if replacement_message and item["message"] != replacement_message:
                                replacement = {**item, "message": replacement_message}
                                items[index] = replacement
                                if not self._persist_queue(items, path):
                                    return True
                                item = replacement
                            self._mark_dedupe_held(tuple(item["keys"]),
                                                   entry.get("handler", "") if isinstance(entry, dict) else "",
                                                   item["message"])
                            return True
        if entry is None:
            return False
        if isinstance(entry, dict) and entry.get("state") in ("sending", "delivered_unconfirmed"):
            with held_view.file_lock(self._dedupe_owner_lock_path(key),
                                     blocking=False, fail_open=False) as available:
                if available:
                    self._recover_sending(key, entry, "send outcome unknown")
            return True
        if isinstance(entry, dict) and entry.get("state") == "held" \
                and isinstance(entry.get("held_message"), str):
            # A held key suppresses only while its notice survives in either
            # queue. Lock order on every shared path: this instance's
            # notification lock, then the queue lock, then the journal lock.
            with self._notification_lock, self._queue_lock() as locked:
                if not locked:
                    self._raise_recovery(key, "held queue lock unavailable")
                    return True
                try:
                    queued = (
                        held_view.read_queue(self._digest_path)
                        + held_view.read_queue(self._email_path)
                    )
                except Exception as exc:  # noqa: BLE001
                    log.error("[notify] cannot verify held notice (%s); recovery required", exc)
                    self._raise_recovery(key, "held queue unreadable")
                    return True
                message = entry.get("held_message")
                if any((key in item["keys"] if isinstance(item, dict)
                        else item == message) for item in queued):
                    return True

                def quarantine_missing(journal: dict[str, Any]) -> bool:
                    current = journal.get(key)
                    if isinstance(current, dict) and current.get("state") == "held" \
                            and current.get("held_message") == message:
                        journal[key] = {"state": "delivered_unconfirmed",
                                        "attempt_at": self._now_local().isoformat(),
                                        "handler": current.get("handler", "")}
                        return True
                    return False

                self._update_dedupe_journal(quarantine_missing)
                self._raise_recovery(key, "held notice missing from queue")
                return True
        state = entry.get("state")
        if state not in ("held", "pending", "delivered"):
            self._raise_recovery(key, "unexpected receipt state")
            return True
        if state != "pending":
            return True
        if pending_owned:
            return False
        # Owner first, journal second: never acquire an owner lock while
        # holding the journal lock (or this instance's notification lock).
        with held_view.file_lock(
            self._dedupe_owner_lock_path(key), blocking=False,
            fail_open=False,
        ) as available:
            return not available

    def _mark_dedupe_pending(self, key: str, handler: str = "") -> bool:
        def change(journal: dict[str, Any]) -> bool:
            journal[key] = {
                "state": "pending", "pending_at": self._now_local().isoformat(),
                "handler": handler,
            }
            return True
        return self._update_dedupe_journal(change)

    def _mark_dedupe_pending_many(self, keys: tuple[str, ...], handler: str) -> bool:
        def change(journal: dict[str, Any]) -> bool:
            for key in keys:
                journal[key] = {"state": "pending", "pending_at": self._now_local().isoformat(),
                                "handler": handler}
            return bool(keys)
        return self._update_dedupe_journal(change)

    def _mark_dedupe_sending(self, keys: tuple[str, ...], handler: str) -> bool:
        attempt = {"state": "sending", "attempt_at": self._now_local().isoformat(),
                   "attempt_id": uuid.uuid4().hex, "handler": handler}
        def change(journal: dict[str, Any]) -> bool:
            for key in keys:
                journal[key] = attempt.copy()
            return bool(keys)
        return self._update_dedupe_journal(change)

    def _mark_dedupe_delivered_many(self, keys: tuple[str, ...], handler: str) -> bool:
        def change(journal: dict[str, Any]) -> bool:
            for key in keys:
                journal[key] = {"state": "delivered", "notified_at": self._now_local().isoformat(),
                                "handler": handler}
            return bool(keys)
        return self._update_dedupe_journal(change)

    def _mark_dedupe_delivered(self, key: str, handler: str = "") -> None:
        def change(journal: dict[str, Any]) -> bool:
            journal[key] = {
                "state": "delivered", "notified_at": self._now_local().isoformat(),
                "handler": handler,
            }
            return True
        self._update_dedupe_journal(change)

    def _mark_dedupe_held(
        self, keys: tuple[str, ...], handler: str, message: str
    ) -> bool:
        def change(journal: dict[str, Any]) -> bool:
            for key in keys:
                journal[key] = {
                    "state": "held", "held_message": message, "handler": handler,
                }
            return bool(keys)
        return self._update_dedupe_journal(change)

    def _release_held_messages(self, messages) -> None:
        """Release keys for notices removed without delivery (queue lock held)."""
        gone = {held_view.notice_identity(item) for item in messages}
        if not gone:
            return

        def change(journal: dict[str, Any]) -> bool:
            keys = [key for key, entry in journal.items()
                    if isinstance(entry, dict) and entry.get("state") == "held"
                    and not held_view.receipt_problem(entry)
                    and (any(isinstance(identity, tuple) and key in identity for identity in gone)
                         or entry.get("held_message") in gone)]
            for key in keys:
                del journal[key]
            return bool(keys)

        self._update_dedupe_journal(change)

    def _mark_held_message_delivered(self, message: str | dict) -> None:
        def change(journal: dict[str, Any]) -> bool:
            keys = [key for key, entry in journal.items()
                    if isinstance(entry, dict) and entry.get("state") == "held"
                    and not held_view.receipt_problem(entry)
                    and (key in message["keys"] if isinstance(message, dict)
                         else entry.get("held_message") == message)]
            for key in keys:
                journal[key] = {
                    "state": "delivered", "notified_at": self._now_local().isoformat(),
                    "handler": journal[key].get("handler", ""),
                }
            return bool(keys)
        self._update_dedupe_journal(change)

    def _mark_dedupe_notified(self, key: str, handler: str = "") -> None:
        """Record that ``key`` has been reported. Never raises."""
        try:
            def change(journal: dict[str, Any]) -> bool:
                if key not in journal:
                    journal[key] = {
                        "state": "delivered",
                        "notified_at": self._now_local().isoformat(),
                        "handler": handler,
                    }
                    return True
                return False
            self._update_dedupe_journal(change)
        except Exception as exc:  # noqa: BLE001
            log.error("[notify] could not record dedupe key %r (%s)", key, exc)

    def release_dedupe(self, key: str) -> None:
        """Forget ``key`` in the journal only, after a send that did not deliver.

        ``send(dedupe_key=key)`` records the key before routing (see its
        comment), so a send that was then held or failed still reads as
        reported. A caller that must retry until the operator is actually told
        calls this when ``send`` returned False. Unlike ``clear_dedupe`` it
        does not resolve the alert's Neotoma sink record: the condition is
        still open and still unreported. Never raises.
        """
        try:
            entry = self._dedupe_journal.get(key)
            if key in self._dedupe_journal and held_view.receipt_problem(entry):
                self._raise_recovery(key, "malformed receipt")
                return
            if any(row["key"] == key for row in held_view.read_recovery(self._digest_path)):
                return
            def change(journal: dict[str, Any]) -> bool:
                if key in journal:
                    del journal[key]
                    return True
                return False
            self._update_dedupe_journal(change)
        except Exception as exc:  # noqa: BLE001
            log.warning("[notify] could not release dedupe key %r (%s)", key, exc)

    def dedupe_journal_path(self) -> Path:
        """Where this notifier's dedupe journal lives (for a sidecar lock)."""
        return self._dedupe_path

    def migrate_dedupe(self, old_key: str, new_key: str) -> bool:
        """Rename a receipt and its queued notice as one local transition."""
        if old_key == new_key:
            return True
        try:
            with self._notification_lock, self._queue_lock() as queue_locked:
                if not queue_locked:
                    return False
                with held_view.file_lock(self._dedupe_journal_lock_path(),
                                         fail_open=False) as journal_locked:
                    if not journal_locked:
                        return False
                    journal = self._dedupe_journal
                    marker = next((row for row in held_view.read_recovery(self._digest_path)
                                   if row["key"] == old_key and row.get("migration_to") == new_key), None)
                    if old_key not in journal:
                        if marker is None:
                            return True
                        if not neotoma_sink.migrate_key(old_key, new_key):
                            return False
                        with held_view.file_lock(held_view.recovery_lock_path(self._digest_path),
                                                 fail_open=False) as recovered:
                            if not recovered:
                                return False
                            held_view.write_recovery(self._digest_path, [row for row in
                                held_view.read_recovery(self._digest_path) if row != marker])
                        return True
                    if held_view.receipt_problem(journal[old_key]):
                        self._raise_recovery(old_key, "malformed receipt during migration")
                        return False
                    if new_key in journal:
                        if held_view.receipt_problem(journal[new_key]):
                            self._raise_recovery(new_key, "malformed receipt during migration")
                        return False
                    if marker is None:
                        with held_view.file_lock(held_view.recovery_lock_path(self._digest_path),
                                                 fail_open=False) as recovered:
                            if not recovered:
                                return False
                            records = held_view.read_recovery(self._digest_path)
                            marker = {"key": old_key, "time": self._now_local().isoformat(),
                                      "cause": "sink key migration pending", "migration_to": new_key}
                            held_view.write_recovery(self._digest_path, records + [marker])
                    queues = {path: held_view.read_queue(path)
                              for path in (self._digest_path, self._email_path)}
                    changed: dict[Path, list[str | dict]] = {}
                    for path, items in queues.items():
                        replacements = []
                        for item in items:
                            if isinstance(item, dict) and old_key in item["keys"]:
                                replacement = {**item, "keys": [
                                    new_key if key == old_key else key for key in item["keys"]
                                ]}
                                replacements.append(replacement)
                            else:
                                replacements.append(item)
                        if replacements != items:
                            changed[path] = replacements
                    written: list[Path] = []
                    for path, items in changed.items():
                        if not self._persist_queue(items, path):
                            for previous in written:
                                self._persist_queue(queues[previous], previous)
                            self._raise_recovery(old_key, "queue key migration failed")
                            return False
                        written.append(path)
                    renamed = {**journal, new_key: journal[old_key]}
                    del renamed[old_key]
                    if not self._persist_dedupe_journal(renamed):
                        for previous in written:
                            self._persist_queue(queues[previous], previous)
                        self._raise_recovery(old_key, "journal key migration failed")
                        return False
                    try:
                        with held_view.file_lock(
                            held_view.recovery_lock_path(self._digest_path),
                            fail_open=False,
                        ) as recovery_locked:
                            if not recovery_locked:
                                raise OSError("recovery lock unavailable")
                            records = held_view.read_recovery(self._digest_path)
                            migrated = [{**row, "key": new_key} if row["key"] == old_key
                                        and not row.get("migration_to")
                                        else row for row in records]
                            if migrated != records:
                                held_view.write_recovery(self._digest_path, migrated)
                    except Exception as exc:  # noqa: BLE001
                        log.error("[notify] recovery key migration failed: %s", exc)
                        self._persist_dedupe_journal(journal)
                        for previous in written:
                            self._persist_queue(queues[previous], previous)
                        self._raise_recovery(old_key, "recovery key migration failed")
                        return False
                    if not neotoma_sink.migrate_key(old_key, new_key):
                        return False
                    with held_view.file_lock(held_view.recovery_lock_path(self._digest_path),
                                             fail_open=False) as recovered:
                        if not recovered:
                            return False
                        held_view.write_recovery(self._digest_path, [row for row in
                            held_view.read_recovery(self._digest_path) if row != marker])
                    return True
        except Exception as exc:  # noqa: BLE001
            log.error("[notify] key migration failed for %r: %s", old_key, exc)
            self._raise_recovery(old_key, "key migration failed")
            return False

    def clear_dedupe(self, key: str) -> None:
        """Clear a dedupe key once its condition has resolved.

        Call this the moment the underlying condition is no longer true (a
        consent-channel reply arrives, a gate starts passing again) so a
        genuine recurrence — the condition clearing and then coming back — is
        reported again rather than staying suppressed forever. Never raises.
        """
        try:
            journal = self._dedupe_journal
            records = held_view.read_recovery(self._digest_path)
            marker = next((row for row in records if row["key"] == key and row.get("migration_to")), None)
            projection = next((row for row in records if row["key"] == key
                               and row.get("cause") == "sink resolution pending"), None)
            if key not in journal and marker:
                self.clear_dedupe(marker["migration_to"])
                if neotoma_sink.resolve(key) is False:
                    return
                with held_view.file_lock(held_view.recovery_lock_path(self._digest_path),
                                         fail_open=False) as recovered:
                    if recovered:
                        held_view.write_recovery(self._digest_path, [row for row in
                            held_view.read_recovery(self._digest_path) if row != marker])
                return
            if key not in journal and projection:
                if neotoma_sink.resolve(key) is False:
                    return
                with held_view.file_lock(held_view.recovery_lock_path(self._digest_path),
                                         fail_open=False) as recovered:
                    if recovered:
                        held_view.write_recovery(self._digest_path, [row for row in
                            held_view.read_recovery(self._digest_path) if row != projection])
                return
            if key in journal and held_view.receipt_problem(journal[key]):
                self._raise_recovery(key, "malformed receipt")
                return
            if any(row["key"] == key and not row.get("migration_to")
                   and row.get("cause") != "sink resolution pending" for row in records):
                return
            with self._notification_lock, self._queue_lock() as queue_locked:
                if not queue_locked:
                    return
                # Establish that the receipt source is readable before any
                # held queue item can be removed.
                self._dedupe_journal
                if projection is None and marker is None:
                    self._raise_recovery(key, "sink resolution pending", notify=False)
                    projection = next((row for row in held_view.read_recovery(self._digest_path)
                                       if row["key"] == key
                                       and row.get("cause") == "sink resolution pending"), None)
                    if projection is None:
                        return
                queues = {path: held_view.read_queue(path)
                          for path in (self._digest_path, self._email_path)}
                kept = {path: [item for item in items if not (
                    isinstance(item, dict) and key in item["keys"]
                )] for path, items in queues.items()}
                removed = [item for path, items in queues.items() for item in items
                           if item not in kept[path]]
                if removed:
                    backup = held_view.backup_path(self._digest_path)
                    try:
                        previous_backup = json.loads(held_view.read_text_nofollow(backup))
                        if not isinstance(previous_backup, list):
                            previous_backup = []
                    except FileNotFoundError:
                        previous_backup = []
                    held_view.atomic_write(
                        backup, json.dumps((previous_backup + removed)[-held_view.BACKUP_MAX:])
                    )
                written = []
                for path, items in kept.items():
                    if items != queues[path]:
                        if not self._persist_queue(items, path):
                            for previous in written:
                                self._persist_queue(queues[previous], previous)
                            return
                        written.append(path)
                with held_view.file_lock(self._dedupe_journal_lock_path(),
                                         fail_open=False) as journal_locked:
                    if not journal_locked:
                        for previous in written:
                            self._persist_queue(queues[previous], previous)
                        return
                    journal = self._dedupe_journal
                    if key in journal:
                        without_key = {k: v for k, v in journal.items() if k != key}
                        if not self._persist_dedupe_journal(without_key):
                            for previous in written:
                                self._persist_queue(queues[previous], previous)
                            return
            if neotoma_sink.resolve(key) is False:
                return
            with held_view.file_lock(held_view.recovery_lock_path(self._digest_path),
                                     fail_open=False) as recovered:
                if recovered:
                    held_view.write_recovery(self._digest_path, [row for row in
                        held_view.read_recovery(self._digest_path)
                        if row != projection and row != marker])
        except UnreadableDedupeJournal:
            self._hold_unreadable_journal(None, "", "")
        except Exception as exc:  # noqa: BLE001
            log.warning("[notify] could not clear dedupe key %r (%s)", key, exc)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _deliver_email(self, message: str) -> bool:
        """Deliver one notification via the swarm address (gws +send). Fail-open.

        Subject = a short prefix + the message's first line; body = the full
        message. Uses an argv list (no shell) so arbitrary notification text can't
        be misinterpreted. Returns False on any failure so the caller can fall
        back to Telegram."""
        if not self._notify_to:
            return False
        try:
            first = (message.strip().splitlines() or ["notification"])[0]
        except Exception as exc:  # noqa: BLE001 — no transport was invoked
            raise TransportNotStarted("could not build email message") from exc
        # Refuse to mint a digest-shaped subject even if a caller bypasses flush.
        if first.startswith("📋 Digest") or "Digest (" in first[:40]:
            log.warning(
                "[notify] refusing digest-shaped email subject (routine digests disabled): %r",
                first[:80],
            )
            return False
        subject = f"[Ateles] {first[:80]}"
        cmd = ["gws", "gmail", "+send", "--to", self._notify_to,
               "--subject", subject, "--body", message]
        if self._swarm_email:
            cmd += ["--from", self._swarm_email]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                log.warning("[notify] gws +send failed (rc=%s): %s",
                            r.returncode, (r.stderr or "").strip()[:200])
                return False
            return True
        except Exception as exc:  # noqa: BLE001 — caller must quarantine an uncertain send
            log.warning("[notify] email send error: %s", exc)
            raise

    def _deliver(
        self, message: str, force: bool = False, email_eligible: bool = True
    ) -> bool:
        # Hard stop: never deliver a bulk digest payload on any channel.
        try:
            first = (message.strip().splitlines() or [""])[0]
        except Exception as exc:  # noqa: BLE001 — no transport was invoked
            raise TransportNotStarted("could not build notification") from exc
        if first.startswith("📋 Digest") or message.lstrip().startswith("📋 Digest"):
            log.warning(
                "[notify] refusing bulk digest delivery (routine digests disabled): %r",
                first[:80],
            )
            return False
        # E6: try email first when it's the configured primary transport; only
        # fall through to Telegram (break-glass) if email delivery fails. A
        # caller that marked this send email_eligible=False skips straight to
        # Telegram — the notification still reaches the operator, it just
        # never becomes an inbox item he has no way to act on by reading it
        # (ateles#1127).
        if self._email_primary and email_eligible:
            if self._deliver_email(message):
                return True
            log.warning("[notify] email delivery failed — falling back to Telegram")
        if not self._apprise:
            log.info(f"[notify] (no apprise) Would send: {message!r}")
            return False
        try:
            ok = self._apprise.notify(body=message)
            if ok:
                log.debug(f"[notify] Sent: {message[:80]!r}")
            else:
                log.warning(f"[notify] Apprise returned False for: {message[:80]!r}")
            return bool(ok)
        except Exception as exc:
            log.error(f"[notify] Delivery error: {exc}")
            raise

    def is_dedupe_open(self, key: str) -> bool:
        """True when ``key`` was already reported and not yet cleared."""
        try:
            return self._is_duplicate(key)
        except UnreadableDedupeJournal:
            self._hold_unreadable_journal(None, "", "")
            return False
        except Exception as exc:  # noqa: BLE001
            log.error("[notify] dedupe check failed; retry required (%s)", exc)
            return False

    def dedupe_keys_with_prefix(self, prefix: str) -> tuple[str, ...]:
        """Journal keys beginning with ``prefix``, for condition cleanup."""
        with self._notification_lock:
            try:
                return tuple(key for key in self._dedupe_journal if key.startswith(prefix))
            except UnreadableDedupeJournal:
                self._hold_unreadable_journal(None, "", "")
                return ()

    def has_transport(self, email_eligible: bool = True) -> bool:
        """True when at least one delivery route is configured.

        A Notifier built without Apprise or Telegram credentials (and with no
        email route) accepts ``send()`` and delivers nothing. Email counts only
        for an email-eligible send. Callers that
        must not lose an alert silently ask this first.
        """
        if self._apprise is not None and len(self._apprise) > 0:
            return True
        return bool(email_eligible and self._email_primary and self._notify_to)

    def in_silence_window(self) -> bool:
        """True while warn and operator-decision notices are held, not sent.

        ``send()`` returns False for a held notice and for a failed delivery
        alike. A caller that has to tell them apart asks here.
        """
        return self._in_silence_window()

    def _in_silence_window(self) -> bool:
        now = self._now_local()
        try:
            start_h, start_m = map(int, self._rubric["silence_start"].split(":"))
            end_h, end_m = map(int, self._rubric["silence_end"].split(":"))
        except (KeyError, ValueError):
            return False
        start = time(start_h, start_m)
        end = time(end_h, end_m)
        current = time(now.hour, now.minute)
        if start > end:
            # Spans midnight: silence if after start OR before end
            return current >= start or current < end
        return start <= current < end

    def _now_local(self) -> datetime:
        tz_name = self._rubric.get("timezone", "Europe/Madrid")
        try:
            tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            tz = zoneinfo.ZoneInfo("UTC")
        return datetime.now(tz=tz)

    def _build_telegram_url(self) -> str:
        """Build Apprise Telegram URL."""
        # Format: tgram://<bot_token>/<chat_id>/
        # Thread ID: apprise supports ?thread_id= parameter
        url = f"tgram://{self._bot_token}/{self._chat_id}/"
        if self._topic_id:
            url += f"?thread_id={self._topic_id}"
        return url


# ── Neotoma loader ───────────────────────────────────────────────────────────


def _load_rubric_from_neotoma() -> dict[str, Any]:
    """Fetch priority_rubric entity from Neotoma. Falls back to defaults."""
    if not NEOTOMA_BEARER_TOKEN or not NEOTOMA_BASE_URL:
        log.warning("[notify] NEOTOMA_BEARER_TOKEN not set — using default rubric")
        return _DEFAULT_RUBRIC

    entity_id = PRIORITY_RUBRIC_ENTITY_ID
    url = f"{NEOTOMA_BASE_URL}/entities/{entity_id}"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        snapshot = data.get("snapshot") or data.get("entity", {}).get("snapshot", {})
        if snapshot:
            log.info(f"[notify] Loaded priority_rubric from Neotoma: {entity_id}")
            return {**_DEFAULT_RUBRIC, **snapshot}
    except Exception as exc:
        log.warning(
            f"[notify] Could not load priority_rubric from Neotoma: {exc} — using defaults"
        )
    return _DEFAULT_RUBRIC
