"""
lib/notify/notifier.py — Apprise-backed notification router for Ateles daemons.

Reads a priority_rubric entity from Neotoma at startup.
Routes notifications by priority through Apprise (Telegram-primary).
Respects silence windows. Routine INFO events are never emailed and never
accumulate for a later flush; only actionable held notices (operator_decision /
warn inside the silence window) persist, and they drain as individual deliveries
— never as a bulk "Digest" message.

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
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
from datetime import datetime, time
from enum import Enum
from pathlib import Path
from typing import Any

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

log = logging.getLogger(__name__)

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


# Held-queue prefixes applied when operator_decision / warn are deferred
# across the silence window. Used to classify legacy digest-queue files so
# actionable notices survive while routine INFO listings are dropped.
_ACTIONABLE_HELD_PREFIXES = ("⚠️", "⚠")


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


def is_actionable_held_notice(message: str) -> bool:
    """True when a persisted queue item is an actionable held notice.

    OPERATOR_DECISION items are prefixed with ⚠️; WARN with ⚠. Routine INFO
    (and any other non-prefixed legacy digest residue) is not actionable.
    """
    text = (message or "").lstrip()
    return text.startswith(_ACTIONABLE_HELD_PREFIXES)


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
        self._digest_path = Path(
            os.environ.get("ATELES_DIGEST_QUEUE_PATH", "").strip()
            or Path(tempfile.gettempdir()) / "ateles-notify-digest.json"
        )
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
        self._notification_lock = threading.RLock()
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
    ) -> bool:
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
        with self._notification_lock:
            return self._send_locked(
                message, priority, handler, bypass_silence, dedupe_key, email_eligible
            )

    def _send_locked(
        self,
        message: str,
        priority: Priority | str,
        handler: str,
        bypass_silence: bool,
        dedupe_key: str | None = None,
        email_eligible: bool = True,
    ) -> bool:
        prio = Priority(priority) if isinstance(priority, str) else priority
        tag = f"[{handler}] " if handler else ""
        full_message = f"{tag}{message}"

        if dedupe_key and self._is_duplicate(dedupe_key):
            log.info(
                "[notify] suppressing repeat notification for open condition "
                "%r (call clear_dedupe() once it resolves): %r",
                dedupe_key,
                full_message[:200],
            )
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
            self._mark_dedupe_notified(dedupe_key)

        # Drain any prior held actionable notices before handling this send.
        self._maybe_flush_digest()

        if prio == Priority.CRITICAL:
            # Critical always fires immediately, even in silence window
            return self._deliver(full_message, force=True, email_eligible=email_eligible)

        if prio == Priority.BLOCKER:
            if self._in_silence_window() and not bypass_silence:
                log.info(
                    "[notify] Blocker in silence window — delivering anyway (blocker policy)"
                )
            return self._deliver(full_message, force=True, email_eligible=email_eligible)

        if prio == Priority.OPERATOR_DECISION:
            if self._in_silence_window() and not bypass_silence:
                log.info(
                    "[notify] Operator decision in silence window — holding for later delivery"
                )
                self._queue_digest(f"⚠️ {full_message}")
                return False
            return self._deliver(
                f"⚠️ {full_message}", force=False, email_eligible=email_eligible
            )

        if prio == Priority.WARN:
            if self._in_silence_window() and not bypass_silence:
                self._queue_digest(f"⚠ {full_message}")
                return False
            return self._deliver(
                f"⚠ {full_message}", force=False, email_eligible=email_eligible
            )

        # INFO — never email, never queue, never flush later as a digest.
        log.debug("[notify] Dropping routine INFO (no digest): %r", full_message)
        return False

    # ── Held-notice queue (file-backed; actionable only) ─────────────────────

    @property
    def _digest_queue(self) -> list[str]:
        """Read the persisted queue. Fail-open: unreadable state => empty.

        Property name kept for test/compat callers; contents are held notices
        (and any legacy digest residue awaiting classification).
        """
        with self._notification_lock:
            try:
                raw = json.loads(self._digest_path.read_text())
                return [str(x) for x in raw] if isinstance(raw, list) else []
            except FileNotFoundError:
                return []
            except Exception as exc:  # noqa: BLE001 — never crash a notification
                log.warning(
                    "[notify] digest queue unreadable (%s) — treating as empty", exc
                )
                return []

    def _persist_queue(self, items: list[str]) -> None:
        """Atomically rewrite the queue file. Caller must hold the lock."""
        try:
            self._digest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._digest_path.with_suffix(".tmp")
            if items:
                tmp.write_text(json.dumps(items))
                tmp.replace(self._digest_path)
            else:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._digest_path.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[notify] could not clear empty held queue: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.error("[notify] could not persist held queue (%s)", exc)

    def _queue_digest(self, message: str) -> None:
        """Append an actionable held notice. Never raises."""
        with self._notification_lock:
            try:
                items = self._digest_queue
                items.append(message)
                self._persist_queue(items)
            except Exception as exc:  # noqa: BLE001
                # Losing the queue write must not lose the alert: say so loudly.
                log.error(
                    "[notify] could not persist held notice (%s) — message not "
                    "queued and will NOT be delivered: %r",
                    exc,
                    message[:200],
                )

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

    def flush_digest(self) -> bool:
        """Drain the held queue without sending a bulk digest.

        Classifies first: routine INFO residue is dropped; actionable held
        notices are delivered one-by-one. The queue is rewritten to only the
        actionable items that failed to deliver. Never emits a ``📋 Digest``
        body (those were the routine [Ateles] Digest emails).
        """
        with self._notification_lock:
            items = self._digest_queue
            if not items:
                return False

            actionable, routine = self.classify_queue(items)
            if routine:
                log.info(
                    "[notify] dropping %d routine digest item(s); preserving %d actionable",
                    len(routine),
                    len(actionable),
                )

            if not actionable:
                self._persist_queue([])
                return False

            remaining: list[str] = []
            any_ok = False
            for item in actionable:
                if self._deliver(item, force=True):
                    any_ok = True
                else:
                    remaining.append(item)

            self._persist_queue(remaining)
            if remaining:
                log.warning(
                    "[notify] held-notice delivery incomplete — keeping %d actionable item(s)",
                    len(remaining),
                )
            return any_ok

    def _maybe_flush_digest(self) -> None:
        """Drain held actionable notices opportunistically.

        Outside the silence window (or at legacy digest_times windows used as
        release moments), deliver held actionable items individually. Never
        self-flushes a bulk digest of accumulated INFO.
        """
        try:
            if not self._digest_queue:
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
        """Read the persisted dedupe journal. Fail-open: unreadable => empty.

        Shape: ``{key: {"notified_at": iso8601, "handler": str}}``. A key
        present here has already been reported and is suppressed on every
        later ``send(dedupe_key=key)`` until ``clear_dedupe(key)`` removes it.
        """
        with self._notification_lock:
            try:
                raw = json.loads(self._dedupe_path.read_text())
                return raw if isinstance(raw, dict) else {}
            except FileNotFoundError:
                return {}
            except Exception as exc:  # noqa: BLE001 — never crash a notification
                log.warning(
                    "[notify] dedupe journal unreadable (%s) — treating as empty",
                    exc,
                )
                return {}

    def _persist_dedupe_journal(self, journal: dict[str, Any]) -> None:
        """Atomically rewrite the dedupe journal. Caller must hold the lock."""
        try:
            self._dedupe_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._dedupe_path.with_suffix(".tmp")
            if journal:
                tmp.write_text(json.dumps(journal))
                tmp.replace(self._dedupe_path)
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
        except Exception as exc:  # noqa: BLE001
            # Fail open toward SENDING rather than silently losing an alert:
            # if the journal can't be written, the key is simply not recorded
            # and the next tick will (harmlessly) re-notify. That is the
            # pre-existing behaviour, not a new failure mode.
            log.error("[notify] could not persist dedupe journal (%s)", exc)

    def _is_duplicate(self, key: str) -> bool:
        with self._notification_lock:
            return key in self._dedupe_journal

    def is_dedupe_duplicate(self, key: str) -> bool:
        """Public query: has ``key`` already been reported and not cleared?

        For a caller whose condition has a side effect beyond the
        notification itself — e.g. a durable GitHub comment posted alongside
        the ``send()`` call — that also needs to be gated on the SAME dedupe
        decision. ``send()``'s own return value is not enough for this: it
        is ``False`` both when suppressed as a duplicate and when a message
        is legitimately held (silence window, digest queue), and those two
        cases must not be conflated by a caller deciding whether to also
        post a comment. Check this BEFORE calling ``send(dedupe_key=key)``
        — that call marks the key, so checking after would always read
        True. Never raises.
        """
        try:
            return self._is_duplicate(key)
        except Exception as exc:  # noqa: BLE001 — never crash a notification
            log.warning(
                "[notify] could not check dedupe key %r (%s) — treating as "
                "not-duplicate (fail open toward sending)",
                key,
                exc,
            )
            return False

    def _mark_dedupe_notified(self, key: str, handler: str = "") -> None:
        """Record that ``key`` has been reported. Never raises."""
        with self._notification_lock:
            try:
                journal = self._dedupe_journal
                if key not in journal:
                    journal[key] = {
                        "notified_at": self._now_local().isoformat(),
                        "handler": handler,
                    }
                    self._persist_dedupe_journal(journal)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "[notify] could not record dedupe key %r (%s) — condition "
                    "may re-notify on the next tick",
                    key,
                    exc,
                )

    def clear_dedupe(self, key: str) -> None:
        """Clear a dedupe key once its condition has resolved.

        Call this the moment the underlying condition is no longer true (a
        consent-channel reply arrives, a gate starts passing again) so a
        genuine recurrence — the condition clearing and then coming back — is
        reported again rather than staying suppressed forever. Never raises.
        """
        with self._notification_lock:
            try:
                journal = self._dedupe_journal
                if key in journal:
                    del journal[key]
                    self._persist_dedupe_journal(journal)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[notify] could not clear dedupe key %r (%s)", key, exc
                )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _deliver_email(self, message: str) -> bool:
        """Deliver one notification via the swarm address (gws +send). Fail-open.

        Subject = a short prefix + the message's first line; body = the full
        message. Uses an argv list (no shell) so arbitrary notification text can't
        be misinterpreted. Returns False on any failure so the caller can fall
        back to Telegram."""
        if not self._notify_to:
            return False
        first = (message.strip().splitlines() or ["notification"])[0]
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
        except Exception as exc:  # noqa: BLE001 — never crash the caller
            log.warning("[notify] email send error: %s", exc)
            return False

    def _deliver(
        self, message: str, force: bool = False, email_eligible: bool = True
    ) -> bool:
        # Hard stop: never deliver a bulk digest payload on any channel.
        first = (message.strip().splitlines() or [""])[0]
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
            return False

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
