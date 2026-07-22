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
~/repos/mcp-server-bitcoin).

Key material (BTC_MNEMONIC / BTC_PRIVATE_KEY) is read from the WALLET's own
`.env`, loaded here into this short-lived subprocess only — see _load_wallet_env().
The seed is deliberately NOT placed in the daemon's launchd environment, where
it would sit in a long-lived process and be readable via `launchctl print`.
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


def _load_wallet_env(wallet_path: Path) -> None:
    """Load the wallet's own .env into this process, without overriding real env.

    The wallet library calls load_dotenv(Path(__file__).parent.parent.parent /
    ".env"), which is three levels up from the module — `~/.env` for a checkout
    at ~/repos/mcp-server-bitcoin. That file does not exist, so the wallet's own
    .env is never loaded and BTCConfig.from_env() raises "No key material
    configured". Its MCP server works only because it loads SERVER_DIR/".env"
    explicitly before importing. This runner does the same.

    Existing environment variables always win, so an operator or CI can override
    without editing the file. Parsing is deliberately minimal (KEY=VALUE, `#`
    comments, optional `export`, surrounding quotes stripped) so this has no
    dependency on python-dotenv being installed in the wallet's venv.

    Values are NOT logged, echoed, or returned — they exist only in this
    subprocess, which exits within seconds.
    """
    env_file = wallet_path / ".env"
    try:
        if not env_file.is_file():
            return
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            if not key or key in os.environ:
                continue  # real environment wins
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ[key] = value
    except OSError:
        # Unreadable .env is not fatal here: from_env() raises a clear
        # "No key material configured" error, which the caller surfaces.
        return


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

    # Must precede the import: the wallet reads key material at config time, and
    # its own load_dotenv() targets a path that does not exist (see docstring).
    _load_wallet_env(wallet_path)

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
        "network": str(getattr(cfg, "network", "mainnet")),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
