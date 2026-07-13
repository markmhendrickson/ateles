#!/usr/bin/env python3
"""
btc_send_runner.py — Minimal, deterministic BTC send invoked by btc_transfer.py.

Run with the BITCOIN WALLET's own venv python (it has bip_utils etc.), NOT the
daemon venv. Reads a JSON request on argv[1], calls the wallet library directly,
prints exactly one JSON result line to stdout. No LLM, no agent — a pure function
call, so there is nothing to "refuse".

Request  (argv[1]): {"address": str, "amount_eur": number, "dry_run": bool}
Response (stdout):  {"status": "sent"|"dry_run"|"failed", "txid": str,
                     "amount_sats": int, "error": str?}

The wallet module path comes from BTC_WALLET_MODULE_PATH (default
~/repos/mcp-server-bitcoin). Wallet secrets (BTC_MNEMONIC, …) must be in env.
Never attaches a memo / OP_RETURN.
"""

import json
import os
import sys
from decimal import Decimal
from pathlib import Path


def _out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def main() -> int:
    try:
        req = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    except (ValueError, IndexError) as exc:
        _out({"status": "failed", "error": f"bad request: {exc}"})
        return 1

    address = str(req.get("address") or "").strip()
    amount_eur = req.get("amount_eur")
    dry_run = bool(req.get("dry_run", True))
    if not address or amount_eur is None:
        _out({"status": "failed", "error": "missing address or amount_eur"})
        return 1

    wallet_path = Path(
        os.environ.get("BTC_WALLET_MODULE_PATH",
                       str(Path.home() / "repos" / "mcp-server-bitcoin"))
    ).expanduser()
    if str(wallet_path) not in sys.path:
        sys.path.insert(0, str(wallet_path))

    try:
        import bitcoin_wallet as wallet
    except Exception as exc:  # noqa: BLE001
        _out({"status": "failed", "error": f"wallet import failed: {exc}"})
        return 1

    try:
        cfg = wallet.BTCConfig.from_env()
    except Exception as exc:  # noqa: BLE001
        _out({"status": "failed", "error": f"wallet config unavailable: {exc}"})
        return 1

    try:
        _usd, eur_price = wallet._fetch_btc_prices()
        if eur_price <= 0:
            raise RuntimeError("eur_price <= 0")
        amount_btc = Decimal(str(amount_eur)) / eur_price
        amount_sats = int((amount_btc * Decimal("1e8")).to_integral_value())
    except Exception as exc:  # noqa: BLE001
        _out({"status": "failed", "error": f"eur->sats failed: {exc}"})
        return 1

    if amount_sats <= 0:
        _out({"status": "failed", "error": f"non-positive amount_sats={amount_sats}"})
        return 1

    try:
        txid = wallet.send_transfer_multi(
            cfg,
            recipients=[{"address": address, "amount_sats": amount_sats}],
            memo=None,  # never a memo / OP_RETURN
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        _out({"status": "failed", "error": f"send failed: {exc}",
              "amount_sats": amount_sats})
        return 1

    _out({
        "status": "dry_run" if dry_run else "sent",
        "txid": str(txid),
        "amount_sats": amount_sats,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
