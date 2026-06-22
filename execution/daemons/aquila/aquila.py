#!/usr/bin/env python3
"""
Aquila — Monthly Cofounder Report Daemon
Named after Aquila chrysaetos (the golden eagle — apex vision from altitude).

Runs once per month via launchd StartCalendarInterval (1st of the month).
Invokes the `aquila` skill (a T4 agent, via the Apis skill_runner) to produce an
adversarial cofounder report grounded in the operator's Neotoma record, stores a
`cofounder_report` entity, and delivers the report to the operator over Telegram.

The daemon itself does no reasoning. It is a thin scheduler:
  1. monthly idempotency guard
  2. dispatch the `aquila` skill with a monthly-report directive
  3. deliver the resulting markdown to Telegram (chunked)
  4. emit a daemon_report and mark the run complete

Usage:
  python3 aquila.py            # run for the current month (idempotent)
  python3 aquila.py --force    # ignore the monthly idempotency guard
  python3 aquila.py --period 2026-06
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: load env from ~/.config/neotoma/.env before anything else.
# (launchd does not source shell profiles)
# ---------------------------------------------------------------------------

_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# Path bootstrap (repo root + apis daemon dir for skill_runner import)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_APIS_DIR = _REPO_ROOT / "execution" / "daemons" / "apis"
if str(_APIS_DIR) not in sys.path:
    sys.path.insert(0, str(_APIS_DIR))

try:
    from lib.notify import Notifier  # noqa: E402

    _notifier: "Notifier | None" = Notifier.from_neotoma()
except Exception:  # lib unavailable or Neotoma unreachable at import time
    _notifier = None


def _notify(message: str, priority: str = "info") -> None:
    """Send via lib/notify if available; silently skip if not."""
    if _notifier is None:
        return
    try:
        from lib.notify import Priority

        p = getattr(Priority, priority.upper(), Priority.INFO)
        _notifier.send(message, priority=p, handler="aquila")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_FILE = LOG_DIR / "aquila.log"
STATE_FILE = Path(__file__).parent / ".aquila_last_run"  # stores YYYY-MM

NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)

# A cofounder report is a deep pass over the full Neotoma corpus; give the
# skill room to run. Default 30 min, overridable via env.
DISPATCH_TIMEOUT_SECONDS = int(os.environ.get("AQUILA_TIMEOUT_SECONDS", "1800"))

# Telegram hard-caps messages at 4096 chars; chunk below that with headroom.
_TELEGRAM_CHUNK = 3500

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("aquila")


# ---------------------------------------------------------------------------
# Idempotency (monthly)
# ---------------------------------------------------------------------------


def _current_period() -> str:
    return date.today().strftime("%Y-%m")


def _already_ran(period: str) -> bool:
    return STATE_FILE.exists() and STATE_FILE.read_text().strip() == period


def _mark_ran(period: str) -> None:
    STATE_FILE.write_text(period)


# ---------------------------------------------------------------------------
# Neotoma daemon_report (paper trail; Anthus surfaces error/critical to Ateles)
# ---------------------------------------------------------------------------


def _emit_daemon_report(severity: str, message: str, details: dict | None = None) -> None:
    if not (NEOTOMA_BEARER_TOKEN and NEOTOMA_BASE_URL):
        return
    payload: dict = {
        "entity_type": "daemon_report",
        "daemon_name": "aquila",
        "aauth_sub": "aquila@ateles-swarm",
        "severity": severity,
        "message": message,
        "report_at": datetime.now(timezone.utc).isoformat(),
    }
    if details:
        payload["details"] = json.dumps(details)
    body = json.dumps(
        {
            "entities": [payload],
            "idempotency_key": f"aquila-report-{_current_period()}-{severity}",
        }
    ).encode()
    try:
        req = urllib.request.Request(
            f"{NEOTOMA_BASE_URL.rstrip('/')}/api/store",
            data=body,
            headers={
                "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30):
            pass
    except Exception as exc:  # non-fatal
        log.debug(f"daemon_report write failed: {exc}")


# ---------------------------------------------------------------------------
# Telegram delivery (chunked)
# ---------------------------------------------------------------------------


def _deliver(report_md: str, period: str) -> None:
    """Deliver the report to the operator over Telegram, chunked to fit limits."""
    header = f"🦅 Cofounder report — {period}"
    if not report_md.strip():
        _notify(f"{header}\n\n(empty report — see logs)", priority="warn")
        return
    _notify(header, priority="info")
    text = report_md.strip()
    for i in range(0, len(text), _TELEGRAM_CHUNK):
        _notify(text[i : i + _TELEGRAM_CHUNK], priority="info")


# ---------------------------------------------------------------------------
# Dispatch the aquila skill
# ---------------------------------------------------------------------------


def _directive(period: str) -> str:
    return (
        f"Run your monthly cofounder report for period {period}. "
        "Follow your SKILL.md exactly: read the operator's Neotoma corpus first "
        "(plan decisions, feedback, meeting analyses, task velocity, daemon "
        "reports, usage, and your prior cofounder_report entities), then produce "
        "all nine sections under the evidence-or-silence rule. Store one "
        f"cofounder_report entity (report_key `monthly-{period}`, mode `monthly`) "
        "linked PART_OF the plan ent_99ace4dd6673aa36ed08b1fe, and output the "
        "complete report markdown as your final message — that text is delivered "
        "verbatim to the operator."
    )


async def _run() -> "object":
    from skill_runner import run_skill  # imported from execution/daemons/apis

    return await run_skill(
        "aquila",
        _directive(_current_period()),
        role="aquila",
        timeout=DISPATCH_TIMEOUT_SECONDS,
        notifier=_notifier,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Aquila monthly cofounder report")
    parser.add_argument("--force", action="store_true", help="ignore monthly guard")
    parser.add_argument("--period", help="override period (YYYY-MM)")
    args = parser.parse_args()

    period = args.period or _current_period()
    log.info(f"Aquila starting — period {period}.")

    if not args.force and _already_ran(period):
        log.info(f"Already ran for {period} — exiting.")
        return

    # Mark immediately to prevent concurrent launchd re-launches; the report is
    # idempotent on the skill side (idempotency_key aquila-monthly-<period>).
    _mark_ran(period)

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        log.exception("Aquila dispatch crashed.")
        _emit_daemon_report("error", f"dispatch crashed: {exc}")
        _notify(f"🦅 Cofounder report ({period}) failed to run: {exc}", priority="error")
        return

    if not getattr(result, "ok", False):
        err = getattr(result, "error", "") or getattr(result, "stderr", "")
        log.error(f"Aquila skill dispatch failed: {err}")
        _emit_daemon_report("error", f"skill dispatch failed: {err[:300]}")
        _notify(f"🦅 Cofounder report ({period}) failed: {err[:300]}", priority="error")
        return

    report_md = getattr(result, "stdout", "") or ""
    _deliver(report_md, period)
    _emit_daemon_report(
        "info",
        f"cofounder report delivered for {period}",
        {"chars": len(report_md)},
    )
    log.info(f"Aquila complete — {len(report_md)} chars delivered for {period}.")


if __name__ == "__main__":
    main()
