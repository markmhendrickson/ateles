"""
lib.activity — Structured activity-log emitter for swarm agents.

All daemons and agents report job-level lifecycle events ("started",
"finished", "failed", "skipped", "escalated") through this helper. The
helper:

1. Writes an `activity_log` entity to Neotoma for durability + reply
   routing later.
2. Sends a Telegram message to CyphorhinusBot (separate from
   OnychomysBot) so the operator can passively observe the swarm without
   getting paged.

Message format (fixed vocabulary, parser-friendly):

    🟢 anthus · started
    dispatching #412 to cicada
    job-id: anthus-2026-05-27-a3f2

    ✅ anthus · finished · 3m 12s
    PR opened: https://github.com/.../pull/418
    job-id: anthus-2026-05-27-a3f2

    🔴 anthus · failed · 1m 04s
    gh auth expired
    job-id: anthus-2026-05-27-a3f2

Status vocabulary: started | finished | failed | skipped | escalated.

Usage (synchronous):

    from lib.activity import ActivityLogger
    log = ActivityLogger(agent="anthus")
    job = log.started("dispatching #412 to cicada")
    try:
        ...do the work...
        job.finished("PR opened: https://...")
    except Exception as exc:
        job.failed(str(exc))

The `job` returns a JobHandle that carries the job_id forward so the
finish/fail line points back at the same start line.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
import urllib.error
import urllib.request

Status = Literal["started", "finished", "failed", "skipped", "escalated"]

_STATUS_EMOJI: dict[str, str] = {
    "started": "🟢",
    "finished": "✅",
    "failed": "🔴",
    "skipped": "⚪",
    "escalated": "🟠",
}

# lib/activity/__init__.py → parent=activity, parent.parent=lib, parent.parent.parent=ateles repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SEND_SCRIPT = _REPO_ROOT / "execution" / "lib" / "telegram" / "send.mjs"


def _maybe_load_env_file(path: Path) -> None:
    """Best-effort env loader. Does not overwrite already-set vars."""
    if not path.exists():
        return
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
    except Exception:
        pass


# Load ~/.config/neotoma/.env so daemons inherit CYPHORHINUS_* and other vars
# without each one needing its own bootstrap. Idempotent; safe under launchd.
_maybe_load_env_file(Path.home() / ".config" / "neotoma" / ".env")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_job_id(agent: str) -> str:
    """Generate a job ID like 'anthus-2026-05-27-a3f2'."""
    date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rand = secrets.token_hex(2)
    return f"{agent}-{date_part}-{rand}"


def _format_duration(seconds: float) -> str:
    """3m 12s style."""
    total = int(seconds)
    mins, secs = divmod(total, 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        return f"{hrs}h {mins}m {secs}s"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def _send_telegram(text: str) -> Optional[int]:
    """Best-effort send to CyphorhinusBot via send.mjs.

    Reads bot token + chat id from env (or the .env files send.mjs loads).
    Uses TELEGRAM_TOPIC_CYPHORHINUS as the thread id when set. Never raises.

    Returns the Telegram message_id when it can be parsed from send.mjs stdout
    (line "Message sent successfully: <id>"), else None. The message_id is the
    anchor for reply-routing: an operator reply carries reply_to_message.message_id,
    which the Cyphorhinus daemon maps back to the activity_log job via this value.
    """
    import re as _re
    import shutil

    node = shutil.which("node")
    if not node or not _SEND_SCRIPT.exists():
        return None

    # Cyphorhinus uses its OWN bot (CYPHORHINUS_TELEGRAM_BOT_TOKEN /
    # CYPHORHINUS_TELEGRAM_CHAT_ID) rather than OnychomysBot. send.mjs reads
    # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, so we override via env on
    # the subprocess.
    env = os.environ.copy()
    bot_token = os.environ.get("CYPHORHINUS_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CYPHORHINUS_TELEGRAM_CHAT_ID", "").strip()
    if bot_token:
        env["TELEGRAM_BOT_TOKEN"] = bot_token
    if chat_id:
        env["TELEGRAM_CHAT_ID"] = chat_id

    args = [node, str(_SEND_SCRIPT), "--text", text]
    thread_id = os.environ.get("TELEGRAM_TOPIC_CYPHORHINUS", "").strip()
    if thread_id:
        args += ["--thread-id", thread_id]

    try:
        proc = subprocess.run(args, env=env, timeout=10, capture_output=True, text=True)
        # send.mjs prints: "Message sent successfully: <message_id>"
        m = _re.search(r"sent successfully:\s*(\d+)", proc.stdout or "")
        if not m:
            m = _re.search(r"message_id[\"']?[:\s]+(\d+)", proc.stdout or "")
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def _store_activity_log(
    *,
    agent: str,
    job_id: str,
    status: Status,
    summary: str,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    telegram_message_id: Optional[int] = None,
) -> None:
    """Best-effort POST to Neotoma /store. Never raises."""
    base_url = os.environ.get("NEOTOMA_BASE_URL", "http://localhost:3180").rstrip("/")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not token:
        return

    payload: dict = {
        "idempotency_key": f"activity-{job_id}-{status}",
        "observation_source": "workflow_state",
        "entities": [
            {
                "entity_type": "activity_log",
                "canonical_name": f"activity:{job_id}:{status}",
                "agent": agent,
                "job_id": job_id,
                "status": status,
                "summary": summary,
            }
        ],
    }
    e = payload["entities"][0]
    if started_at:
        e["started_at"] = started_at
    if finished_at:
        e["finished_at"] = finished_at
    if duration_seconds is not None:
        e["duration_seconds"] = duration_seconds
    if telegram_message_id is not None:
        # Reply-routing anchor: Cyphorhinus maps an operator reply's
        # reply_to_message.message_id back to this job_id/agent.
        e["telegram_message_id"] = telegram_message_id

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url}/store",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0):
            pass
    except Exception:
        pass  # best effort — never block the caller


def _format_line(
    *,
    agent: str,
    status: Status,
    summary: str,
    job_id: str,
    duration_seconds: Optional[float] = None,
) -> str:
    emoji = _STATUS_EMOJI.get(status, "•")
    header = f"{emoji} {agent} · {status}"
    if status != "started" and duration_seconds is not None:
        header += f" · {_format_duration(duration_seconds)}"
    summary_line = summary.strip().splitlines()[0] if summary else ""
    return f"{header}\n{summary_line}\njob-id: {job_id}"


@dataclass
class JobHandle:
    agent: str
    job_id: str
    started_monotonic: float = field(default_factory=time.monotonic)
    started_iso: str = field(default_factory=_now_iso)
    _closed: bool = False

    def _emit(
        self,
        status: Status,
        summary: str,
        *,
        finished: bool = True,
    ) -> None:
        if self._closed:
            return
        duration = None
        finished_at = None
        if finished:
            duration = time.monotonic() - self.started_monotonic
            finished_at = _now_iso()
            self._closed = True
        line = _format_line(
            agent=self.agent,
            status=status,
            summary=summary,
            job_id=self.job_id,
            duration_seconds=duration,
        )
        msg_id = _send_telegram(line)
        _store_activity_log(
            agent=self.agent,
            job_id=self.job_id,
            status=status,
            summary=summary,
            started_at=self.started_iso,
            finished_at=finished_at,
            duration_seconds=duration,
            telegram_message_id=msg_id,
        )

    def finished(self, summary: str) -> None:
        self._emit("finished", summary, finished=True)

    def failed(self, summary: str) -> None:
        self._emit("failed", summary, finished=True)

    def skipped(self, summary: str) -> None:
        self._emit("skipped", summary, finished=True)

    def escalated(self, summary: str) -> None:
        self._emit("escalated", summary, finished=True)


class ActivityLogger:
    """Per-agent activity logger. Construct once at daemon startup."""

    def __init__(self, agent: str) -> None:
        self.agent = agent

    def started(self, summary: str, *, job_id: Optional[str] = None) -> JobHandle:
        jid = job_id or _new_job_id(self.agent)
        handle = JobHandle(agent=self.agent, job_id=jid)
        line = _format_line(
            agent=self.agent,
            status="started",
            summary=summary,
            job_id=jid,
        )
        msg_id = _send_telegram(line)
        _store_activity_log(
            agent=self.agent,
            job_id=jid,
            status="started",
            summary=summary,
            started_at=handle.started_iso,
            telegram_message_id=msg_id,
        )
        return handle
