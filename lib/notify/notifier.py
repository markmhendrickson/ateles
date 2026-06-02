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

import logging
import os
from datetime import datetime, time
from enum import Enum
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
            "TELEGRAM_TOPIC_DAEMON", ""
        )
        self._digest_queue: list[str] = []
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
    def from_neotoma(
        cls,
        *,
        telegram_topic_env: str | None = None,
        telegram_topic_id: str | None = None,
    ) -> "Notifier":
        """
        Load priority_rubric from Neotoma and construct Notifier.

        Args:
            telegram_topic_env: Name of the env var holding the topic ID for
                this daemon (e.g. "TELEGRAM_TOPIC_TYTO"). Takes precedence over
                the generic TELEGRAM_TOPIC_DAEMON fallback.
            telegram_topic_id: Explicit topic ID string. Takes precedence over
                telegram_topic_env.
        """
        rubric = _load_rubric_from_neotoma()
        resolved_topic = (
            telegram_topic_id
            or (os.environ.get(telegram_topic_env, "") if telegram_topic_env else "")
            or None
        )
        return cls(rubric=rubric, telegram_topic_id=resolved_topic)

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
                self._digest_queue.append(f"⚠️ {full_message}")
                return False
            return self._deliver(f"⚠️ {full_message}", force=False)

        # INFO — always digest
        self._digest_queue.append(full_message)
        log.debug(f"[notify] Queued for digest: {full_message!r}")
        return False

    def flush_digest(self) -> bool:
        """Send all queued digest messages as a single Telegram message."""
        if not self._digest_queue:
            return False
        body = "\n".join(f"• {m}" for m in self._digest_queue)
        header = f"📋 Digest ({len(self._digest_queue)} items)\n\n"
        self._digest_queue.clear()
        return self._deliver(header + body, force=True)

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

    def _deliver(self, message: str, force: bool = False) -> bool:
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
