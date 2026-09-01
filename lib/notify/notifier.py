"""
lib/notify/notifier.py — Apprise-backed notification router for Ateles daemons.

Reads a priority_rubric entity from Neotoma at startup.
Routes notifications by priority through Apprise (Telegram-primary).
Respects silence windows and digest collapse.

Priority levels:
    critical          — immediate, bypasses silence window
    blocker           — send now
    operator_decision — send now (operator must decide)
    info              — queued for digest

All times are in the rubric's configured timezone (default: Europe/Madrid).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
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


class Priority(str, Enum):
    CRITICAL = "critical"
    BLOCKER = "blocker"
    OPERATOR_DECISION = "operator_decision"
    # WARN: degraded-but-running conditions (dispatch failures, skipped work).
    # Delivers immediately outside the silence window, queues for digest inside
    # it. Added because daemons (formica, neotoma-agent, apis a2a) already
    # sent Priority.WARN, which crashed with AttributeError on the very paths
    # meant to report failures.
    WARN = "warn"
    INFO = "info"


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
        self._digest_path = Path(
            os.environ.get("ATELES_DIGEST_QUEUE_PATH", "").strip()
            or Path(tempfile.gettempdir()) / "ateles-notify-digest.json"
        )
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
    ) -> bool:
        """
        Route a notification by priority.

        Returns True if sent immediately, False if queued or dropped.
        """
        prio = Priority(priority) if isinstance(priority, str) else priority
        tag = f"[{handler}] " if handler else ""
        full_message = f"{tag}{message}"

        # Give the persisted queue a reader on every send (see _maybe_flush_digest).
        self._maybe_flush_digest()

        if prio == Priority.CRITICAL:
            # Critical always fires immediately, even in silence window
            return self._deliver(full_message, force=True)

        if prio == Priority.BLOCKER:
            if self._in_silence_window() and not bypass_silence:
                log.info(
                    "[notify] Blocker in silence window — delivering anyway (blocker policy)"
                )
            return self._deliver(full_message, force=True)

        if prio == Priority.OPERATOR_DECISION:
            if self._in_silence_window() and not bypass_silence:
                log.info(
                    "[notify] Operator decision in silence window — queuing for digest"
                )
                self._queue_digest(f"⚠️ {full_message}")
                return False
            return self._deliver(f"⚠️ {full_message}", force=False)

        if prio == Priority.WARN:
            if self._in_silence_window() and not bypass_silence:
                self._queue_digest(f"⚠ {full_message}")
                return False
            return self._deliver(f"⚠ {full_message}", force=False)

        # INFO — always digest
        self._queue_digest(full_message)
        log.debug(f"[notify] Queued for digest: {full_message!r}")
        return False

    # ── Digest queue (file-backed) ───────────────────────────────────────────

    @property
    def _digest_queue(self) -> list[str]:
        """Read the persisted queue. Fail-open: unreadable state => empty."""
        try:
            raw = json.loads(self._digest_path.read_text())
            return [str(x) for x in raw] if isinstance(raw, list) else []
        except FileNotFoundError:
            return []
        except Exception as exc:  # noqa: BLE001 — never crash a notification
            log.warning("[notify] digest queue unreadable (%s) — treating as empty", exc)
            return []

    def _queue_digest(self, message: str) -> None:
        """Append to the persisted queue, atomically. Never raises."""
        try:
            items = self._digest_queue
            items.append(message)
            self._digest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._digest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(items))
            tmp.replace(self._digest_path)
        except Exception as exc:  # noqa: BLE001
            # Losing the queue write must not lose the alert: say so loudly.
            log.error(
                "[notify] could not persist digest item (%s) — message not "
                "queued and will NOT be delivered: %r",
                exc,
                message[:200],
            )

    def flush_digest(self) -> bool:
        """Send all queued digest messages as a single message.

        Clears the persisted queue only after a successful delivery, so a
        failed send leaves the items for the next attempt instead of dropping
        them silently.
        """
        items = self._digest_queue
        if not items:
            return False
        body = "\n".join(f"• {m}" for m in items)
        header = f"📋 Digest ({len(items)} items)\n\n"
        ok = self._deliver(header + body, force=True)
        if ok:
            try:
                self._digest_path.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("[notify] digest sent but queue not cleared: %s", exc)
        else:
            log.warning(
                "[notify] digest delivery failed — keeping %d item(s) queued",
                len(items),
            )
        return ok

    def _maybe_flush_digest(self) -> None:
        """Drain the queue opportunistically, on any send.

        Nothing in the tree ever called `flush_digest()`, so queued items were
        immortal. Daemons do emit notifications continuously, so piggy-backing
        the drain on the next send gives the queue a real reader without
        introducing a scheduler. Also drains once the silence window has ended,
        which is when the operator can actually act on what was held back.
        """
        try:
            if not self._digest_queue:
                return
            if self.should_flush_digest() or not self._in_silence_window():
                self.flush_digest()
        except Exception as exc:  # noqa: BLE001 — a drain must never break a send
            log.warning("[notify] opportunistic digest flush failed: %s", exc)

    def should_flush_digest(self) -> bool:
        """True if current time matches a digest window (within 5 min)."""
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

    def _deliver(self, message: str, force: bool = False) -> bool:
        # E6: try email first when it's the configured primary transport; only
        # fall through to Telegram (break-glass) if email delivery fails.
        if self._email_primary:
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
