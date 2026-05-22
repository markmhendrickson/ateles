"""
handlers/btc_transfer.py — Generic BTC transfer handler for Monedula.

Executes a BTC payment for any PaymentProfile with payment_type="btc".
Uses claude --print with the btc-wallet MCP to execute the transfer.
All profile-specific values (address, amount, task ID) are loaded from env
via PaymentProfile — no hardcoded business data here.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:
    from ..handler_base import PaymentHandler
except ImportError:
    from handler_base import PaymentHandler  # type: ignore[no-redef]
from .payment_profile import PaymentProfile

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent  # ateles repo root


class BtcTransferHandler(PaymentHandler):
    """Generic BTC transfer handler parameterised by a PaymentProfile."""

    def __init__(self, profile: PaymentProfile) -> None:
        self.profile = profile

    @property
    def name(self) -> str:
        return self.profile.name

    def matches(self, events: list[dict]) -> list[dict]:
        matched = []
        for event in events:
            summary = event.get("summary", "") or ""
            low = summary.lower()
            if any(kw in low for kw in self.profile.calendar_keywords):
                log.info(f"[{self.name}] Matched event: {summary!r}")
                matched.append({"event": event, "summary": summary})
        return matched

    def preview(self, match: dict) -> str:
        summary = match.get("summary", self.profile.label)
        addr = self.profile.btc_address
        addr_preview = (addr[:16] + "…") if len(addr) > 16 else addr
        task_id = self.profile.neotoma_task_id or "(unknown)"
        return (
            f"₿ {self.profile.label}\n"
            f"  €{self.profile.amount_eur} BTC → {addr_preview}\n"
            f"  Task: {task_id}\n"
            f"  Event: {summary}"
        )

    def execute(self, match: dict) -> dict[str, Any]:
        log.info(f"[{self.name}] Executing BTC payment via claude --print...")

        if not self.profile.btc_address:
            return {
                "status": "failed",
                "handler": self.name,
                "error": f"{self.profile.prefix}_BTC_ADDRESS not set",
            }

        prompt = _build_claude_prompt(self.profile)
        claude_path = _find_claude()
        if not claude_path:
            return {
                "status": "failed",
                "handler": self.name,
                "error": "claude CLI not found in PATH",
            }

        try:
            result = subprocess.run(
                [claude_path, "--print", "--dangerously-skip-permissions", prompt],
                capture_output=True,
                text=True,
                timeout=300,
                env=os.environ,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "failed",
                "handler": self.name,
                "error": "claude subprocess timed out after 300s",
            }
        except Exception as exc:
            return {
                "status": "failed",
                "handler": self.name,
                "error": f"claude subprocess error: {exc}",
            }

        output = result.stdout or ""
        log.debug(
            f"[{self.name}] claude stdout ({len(output)} chars):\n{output[:2000]}"
        )

        payment_result = _parse_payment_result(output)
        if payment_result is None:
            log.error(
                f"[{self.name}] No PAYMENT_RESULT line found in output:\n{output[:1000]}"
            )
            return {
                "status": "failed",
                "handler": self.name,
                "error": "No PAYMENT_RESULT line in claude output",
                "raw_output": output[:500],
            }

        payment_result["handler"] = self.name

        if payment_result.get("status") == "sent":
            txid = payment_result.get("txid", "")
            mempool_url = f"https://mempool.space/tx/{txid}"
            copy_paste_line = f"{self.profile.amount_eur} € 📤 {mempool_url}"
            payment_result["copy_paste_line"] = copy_paste_line
            log.info(f"[{self.name}] Payment sent. txid={txid}")
            _update_task(self.profile, txid)

        return payment_result

    def format_confirmation(self, result: dict) -> str:
        if result.get("status") == "sent":
            txid = result.get("txid", "unknown")
            copy_paste = result.get(
                "copy_paste_line",
                f"{self.profile.amount_eur} € 📤 https://mempool.space/tx/{txid}",
            )
            return (
                f"✅ {self.profile.label} payment sent!\n"
                f"  txid: {txid}\n\n"
                f"Copy-paste line:\n"
                f"  {copy_paste}"
            )
        else:
            error = result.get("error", "unknown error")
            return f"❌ {self.profile.label} payment failed: {error}"


def _find_claude() -> str | None:
    import shutil

    return shutil.which("claude")


def _build_claude_prompt(profile: PaymentProfile) -> str:
    today_str = date.today().isoformat()
    address = profile.btc_address
    amount = profile.amount_eur
    label = profile.label
    return f"""You are executing a Bitcoin payment for {label}.

Today is {today_str}.

INSTRUCTIONS:
1. First call btc_wallet_preview_transfer with these arguments:
   {{ "to_address": "{address}", "amount_eur": {amount} }}

2. Review the preview. If it looks reasonable (correct address, amount ~€{amount}), proceed.

3. Call btc_wallet_send_transfer with these arguments (do NOT pass memo or OP_RETURN):
   {{ "to_address": "{address}", "amount_eur": {amount} }}

4. After sending, output exactly one line in this format (no other text after it):
PAYMENT_RESULT: {{"status": "sent", "txid": "<actual txid>", "amount_eur": {amount}}}

If anything fails, output:
PAYMENT_RESULT: {{"status": "failed", "txid": "", "amount_eur": {amount}, "error": "<description>"}}

Important constraints:
- Do NOT pass a memo field (no OP_RETURN on-chain)
- The PAYMENT_RESULT line must be the very last line of your response
- Do not include any extra text after the PAYMENT_RESULT line
"""


def _parse_payment_result(output: str) -> dict | None:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("PAYMENT_RESULT:"):
            json_str = line[len("PAYMENT_RESULT:") :].strip()
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as exc:
                log.error(
                    f"Failed to parse PAYMENT_RESULT JSON: {exc}\n  line={line!r}"
                )
                return None
    return None


def _update_task(profile: PaymentProfile, txid: str) -> None:
    """Update Neotoma task with payment note and rolled due_date."""
    import shutil

    neotoma = shutil.which("neotoma")
    if not neotoma:
        log.warning(f"[{profile.name}] neotoma CLI not found — skipping task update")
        return

    task_id = profile.neotoma_task_id
    if not task_id:
        log.warning(
            f"[{profile.name}] No neotoma_task_id configured — skipping task update"
        )
        return

    today = date.today()
    mempool_url = f"https://mempool.space/tx/{txid}"
    note = (
        f"Payment sent {today.isoformat()}: "
        f"{profile.amount_eur} EUR BTC txid={txid} {mempool_url}"
    )

    try:
        res = subprocess.run(
            [neotoma, "--api-only", "entities", "update", task_id, "--notes", note],
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ,
        )
        if res.returncode != 0:
            log.warning(
                f"[{profile.name}] neotoma notes update failed: {res.stderr.strip()[:200]}"
            )
        else:
            log.info(f"[{profile.name}] Neotoma task notes updated.")
    except Exception as exc:
        log.warning(f"[{profile.name}] neotoma update error: {exc}")

    next_due = _find_next_event_due_date(profile)
    if next_due:
        try:
            res = subprocess.run(
                [
                    neotoma,
                    "--api-only",
                    "entities",
                    "update",
                    task_id,
                    "--due-date",
                    next_due,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                env=os.environ,
            )
            if res.returncode != 0:
                log.warning(
                    f"[{profile.name}] neotoma due_date update failed: {res.stderr.strip()[:200]}"
                )
            else:
                log.info(f"[{profile.name}] Neotoma task due_date set to {next_due}.")
        except Exception as exc:
            log.warning(f"[{profile.name}] neotoma due_date update error: {exc}")
    else:
        log.warning(
            f"[{profile.name}] Could not find next event date — due_date not updated."
        )


def _find_next_event_due_date(profile: PaymentProfile) -> str | None:
    """Search Google Calendar for the next matching event. Returns due date ISO string."""
    import shutil

    gws = shutil.which("gws")
    if not gws:
        log.warning(
            f"[{profile.name}] gws CLI not found — cannot look up next event date"
        )
        return None

    today = date.today()
    time_min = today.strftime("%Y-%m-%dT00:00:00+02:00")
    time_max = (today + timedelta(days=92)).strftime("%Y-%m-%dT23:59:59+02:00")

    for query in profile.calendar_keywords:
        params = {
            "calendarId": "primary",
            "singleEvents": True,
            "orderBy": "startTime",
            "q": query,
            "timeMin": time_min,
            "timeMax": time_max,
        }
        try:
            result = subprocess.run(
                [gws, "calendar", "events", "list", "--params", json.dumps(params)],
                capture_output=True,
                text=True,
                timeout=30,
                env=os.environ,
            )
            if result.returncode != 0:
                continue
            data = json.loads(result.stdout)
            for item in data.get("items") or []:
                summary_low = (item.get("summary") or "").lower()
                if any(kw in summary_low for kw in profile.calendar_keywords):
                    start = item.get("start", {})
                    event_date_str = start.get("date") or start.get("dateTime", "")[:10]
                    if event_date_str:
                        event_date = date.fromisoformat(event_date_str)
                        due = event_date + timedelta(days=1)
                        log.info(
                            f"[{profile.name}] Next event: {event_date_str}, due: {due.isoformat()}"
                        )
                        return due.isoformat()
        except Exception as exc:
            log.warning(
                f"[{profile.name}] Calendar search error (query={query!r}): {exc}"
            )

    log.info(f"[{profile.name}] No upcoming events found in calendar.")
    return None
