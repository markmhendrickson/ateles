"""
handlers/btc_transfer.py — Generic BTC transfer handler for Monedula.

Executes a BTC payment for any PaymentProfile with payment_type="btc" by calling
the bitcoin wallet library's functions DIRECTLY (BTCConfig.from_env +
send_transfer_multi), mirroring how wise_transfer.py calls the Wise REST API.

This deliberately does NOT shell out to `claude --print` with a scripted payment
prompt: a headless sub-agent correctly refuses to move funds from a context-free
"send X to address Y" script (it reads as prompt-injection), so that path never
executes. A direct library call has no such ambiguity and honours dry_run.

The wallet module is imported from BTC_WALLET_MODULE_PATH (default
~/repos/mcp-server-bitcoin). Wallet secrets (BTC_MNEMONIC, BTC_NETWORK, …) must
be present in the daemon env. All profile-specific values (address, amount, task
ID) come from PaymentProfile — no hardcoded business data here.
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
from .neotoma_cli import correct_field
from .payment_profile import PaymentProfile, effective_amount_eur
from .share_message import build_share_message

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
        # Delegates to the shared matcher (recurring-event-id first, all-keywords
        # fallback, deduped to one payment per obligation). See handler_base.
        return self.match_events(events)

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
        dry_run = os.environ.get("MONEDULA_DRYRUN", "1") != "0"
        amount_eur = effective_amount_eur(self.profile, match)
        log.info(f"[{self.name}] Executing BTC payment via wallet lib "
                 f"(€{amount_eur}, dry_run={dry_run})...")

        if not self.profile.btc_address:
            return {
                "status": "failed",
                "handler": self.name,
                "error": f"{self.profile.prefix}_BTC_ADDRESS not set",
            }

        missing = _key_material_missing()
        if missing:
            # Surface as a CONFIG error, not a failed payment: nothing was
            # attempted, and the operator's approval is still valid once the
            # wallet is reachable. Reported before any broadcast is possible.
            log.error(f"[{self.name}] {missing}")
            return {
                "status": "manual_required",
                "handler": self.name,
                "error": missing,
                "amount_eur": amount_eur,
            }

        # Invoke the deterministic runner using the WALLET's own venv python
        # (it has bip_utils etc., which the daemon venv does not). The runner is
        # a pure function call — no LLM, nothing to refuse — and honours dry_run.
        py = _wallet_python()
        runner = Path(__file__).parent / "btc_send_runner.py"
        req = json.dumps({
            "address": self.profile.btc_address,
            "amount_eur": amount_eur,
            "dry_run": dry_run,
        })
        try:
            proc = subprocess.run(
                [py, str(runner), req],
                capture_output=True, text=True, timeout=180, env=os.environ,
            )
        except subprocess.TimeoutExpired:
            return {"status": "failed", "handler": self.name,
                    "error": "btc_send_runner timed out after 180s"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "handler": self.name,
                    "error": f"btc_send_runner invocation error: {exc}"}

        out = (proc.stdout or "").strip()
        try:
            payment_result = json.loads(out) if out else {}
        except json.JSONDecodeError:
            log.error(f"[{self.name}] runner stdout not JSON: {out[:300]!r} "
                      f"stderr={(proc.stderr or '')[:300]!r}")
            return {"status": "failed", "handler": self.name,
                    "error": "btc_send_runner returned non-JSON",
                    "raw_output": out[:300]}

        payment_result["handler"] = self.name
        payment_result.setdefault("amount_eur", amount_eur)

        if payment_result.get("status") == "dry_run":
            log.info(f"[{self.name}] DRY-RUN built tx (not broadcast): "
                     f"{payment_result.get('txid')}")
            return payment_result

        if payment_result.get("status") == "sent":
            txid = payment_result.get("txid", "")
            network = str(payment_result.get("network")
                          or os.environ.get("BTC_NETWORK", "mainnet"))
            explorer_url = _explorer_url(txid, network)
            payment_result["explorer_url"] = explorer_url
            # receipt_kind mirrors the Wise handler's proof-of-payment surface:
            # for BTC the on-chain explorer page IS the receipt.
            payment_result["receipt_kind"] = "btc_explorer"
            # Payee copy-paste line — "[AMOUNT] € [LINK TO EXPLORER]".
            payment_result["copy_paste_line"] = build_share_message(
                amount_eur=amount_eur,
                rail="btc",
                explorer_url=explorer_url,
            )
            log.info(f"[{self.name}] Payment sent. txid={txid} {explorer_url}")
            _update_task(self.profile, txid)

        return payment_result

    def format_confirmation(self, result: dict) -> str:
        if result.get("status") == "sent":
            txid = result.get("txid", "unknown")
            explorer = result.get("explorer_url") or _explorer_url(
                txid, str(result.get("network") or "mainnet"))
            # Prefer the amount actually charged (carries any one-off override);
            # the profile's standing rate is only a last-resort fallback.
            copy_paste = result.get("copy_paste_line") or build_share_message(
                amount_eur=result.get("amount_eur", self.profile.amount_eur),
                rail="btc", explorer_url=explorer)
            return (
                f"✅ {self.profile.label} payment sent!\n"
                f"  txid: {txid}\n"
                f"  Blockchain explorer: {explorer}\n\n"
                f"Copy-paste line:\n"
                f"  {copy_paste}"
            )
        else:
            error = result.get("error", "unknown error")
            return f"❌ {self.profile.label} payment failed: {error}"


def _key_material_missing() -> str:
    """Return a diagnostic if BTC key material is unreachable, else "".

    Checks the same two sources the runner does — the process environment and
    the wallet checkout's own `.env` — WITHOUT reading any value. Lets a
    misconfigured wallet be reported as a config problem up front rather than
    as a failed payment after the operator has already approved it.
    """
    if os.environ.get("BTC_MNEMONIC") or os.environ.get("BTC_PRIVATE_KEY"):
        return ""
    wallet_path = Path(
        os.environ.get("BTC_WALLET_MODULE_PATH",
                       str(Path.home() / "repos" / "mcp-server-bitcoin"))
    ).expanduser()
    env_file = wallet_path / ".env"
    try:
        if env_file.is_file():
            for raw in env_file.read_text().splitlines():
                stripped = raw.strip()
                if stripped.startswith("export "):
                    stripped = stripped[len("export "):].lstrip()
                if stripped.startswith(("BTC_MNEMONIC=", "BTC_PRIVATE_KEY=")):
                    return ""
    except OSError:
        pass
    return (f"No BTC key material: neither BTC_MNEMONIC nor BTC_PRIVATE_KEY in "
            f"the environment, and none found in {env_file}. Payment not attempted.")


def _wallet_python() -> str:
    """Path to the bitcoin wallet's own venv python (has bip_utils etc.).

    BTC_WALLET_PYTHON overrides; otherwise <BTC_WALLET_MODULE_PATH>/venv13/bin/
    python3, else the wallet module dir's venv, else plain 'python3'.
    """
    override = os.environ.get("BTC_WALLET_PYTHON", "").strip()
    if override:
        return override
    module_path = Path(
        os.environ.get("BTC_WALLET_MODULE_PATH",
                       str(Path.home() / "repos" / "mcp-server-bitcoin"))
    ).expanduser()
    for cand in (module_path / "venv13" / "bin" / "python3",
                 module_path / "venv" / "bin" / "python3"):
        if cand.exists():
            return str(cand)
    return "python3"


def _explorer_url(txid: str, network: str = "mainnet") -> str:
    """Return a mempool.space explorer URL for a txid, network-aware.

    BTC_EXPLORER_BASE overrides the base (default https://mempool.space). Testnet
    / signet get their path prefix so a non-mainnet txid never links to a wrong
    mainnet page.
    """
    base = os.environ.get("BTC_EXPLORER_BASE", "https://mempool.space").rstrip("/")
    net = (network or "mainnet").lower()
    prefix = {
        "mainnet": "",
        "testnet": "/testnet",
        "testnet3": "/testnet",
        "signet": "/signet",
    }.get(net, "")
    return f"{base}{prefix}/tx/{txid}"


def _update_task(profile: PaymentProfile, txid: str) -> None:
    """Append a payment note to the Neotoma task and roll its due_date."""
    task_id = profile.neotoma_task_id
    if not task_id:
        log.warning(
            f"[{profile.name}] No neotoma_task_id configured — skipping task update"
        )
        return

    today = date.today()
    explorer = _explorer_url(txid, os.environ.get("BTC_NETWORK", "mainnet"))
    note = (
        f"Payment sent {today.isoformat()}: "
        f"{profile.amount_eur} EUR BTC txid={txid} {explorer}"
    )
    correct_field(task_id, "notes", note, label=profile.name)

    next_due = _find_next_event_due_date(profile)
    if next_due:
        correct_field(task_id, "due_date", next_due, label=profile.name)
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
