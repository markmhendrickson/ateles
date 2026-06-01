"""
Fetch STX balances from Coinbase using the Coinbase API.

This script is designed to work with:
- Coinbase REST API (v2 `/v2/accounts`) for main Coinbase accounts
- Credentials stored in 1Password and accessed via the `op` CLI

It:
- Reads API key/secret from 1Password (or environment variables as fallback)
- Calls `/v2/accounts`
- Aggregates all accounts where `currency == 'STX'`
- Prints per-account and total STX balances

You must:
- Install `requests` and have `op` (1Password CLI) configured and signed in
- Adjust the `OP_VAULT`, `OP_ITEM`, and field names below to match your setup

Security:
- This script never writes credentials to disk
- All secrets are pulled at runtime from 1Password or env vars
"""

import hashlib
import hmac
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv

    # Load .env from repo root
    repo_root = Path(__file__).parent.parent
    dotenv_path = repo_root / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path)
        print(f"Loaded environment variables from {dotenv_path}")
except ImportError:
    print("python-dotenv not installed. Install with: pip install python-dotenv")
    print("Falling back to system environment variables only.")

try:
    import requests
except ImportError:
    print("ERROR: requests module not installed.")
    print("Install with: pip install requests")
    sys.exit(1)


COINBASE_API_BASE = "https://api.coinbase.com"  # Coinbase Consumer API

# 1Password item reference configuration.
# Using item ID instead of name to avoid issues with special characters.
OP_VAULT = "Wallets"
OP_ITEM = "gstd7jfxcadhjuwjo2enfwssiq"  # Item ID for "Wallet 9: Coinbase"

# Field names from the actual 1Password item structure
OP_FIELD_API_KEY = "API key name"
OP_FIELD_API_SECRET = "API private key"
OP_FIELD_API_PASSPHRASE = "Valor de inicialización secreto"


def op_read(field_ref: str) -> str:
    """
    Read a secret from 1Password using the op CLI.

    field_ref example: f\"op://{OP_VAULT}/{OP_ITEM}/{OP_FIELD_API_KEY}\"
    """
    try:
        result = subprocess.run(
            ["op", "read", field_ref],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"1Password CLI error for {field_ref}: {e}") from e

    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"Empty value returned for 1Password ref: {field_ref}")
    return value


def get_coinbase_creds() -> dict[str, str]:
    """
    Resolve Coinbase API credentials from environment variables or 1Password.

    Priority:
    1. Environment variables (from .env or system):
       - COINBASE_API_KEY
       - COINBASE_API_SECRET
       - COINBASE_API_PASSPHRASE (optional, for Advanced Trade API)
    2. 1Password via op CLI (if env vars not set)
    """
    key = os.getenv("COINBASE_API_KEY")
    secret = os.getenv("COINBASE_API_SECRET")
    passphrase = os.getenv("COINBASE_API_PASSPHRASE")

    if key and secret:
        print("Using Coinbase credentials from environment variables")
    elif not key or not secret:
        print("Coinbase credentials not found in environment, attempting 1Password...")
        # Pull from 1Password via op CLI
        try:
            key = op_read(f"op://{OP_VAULT}/{OP_ITEM}/{OP_FIELD_API_KEY}")
            secret = op_read(f"op://{OP_VAULT}/{OP_ITEM}/{OP_FIELD_API_SECRET}")
            print("Successfully retrieved credentials from 1Password")
        except Exception as e:
            print(f"ERROR: Could not retrieve credentials from 1Password: {e}")
            print("\nTo fix this, either:")
            print("1. Run: python execution/scripts/op_sync_env_from_1password.py")
            print(
                "2. Or set environment variables: COINBASE_API_KEY, COINBASE_API_SECRET"
            )
            sys.exit(1)

        if not passphrase:
            try:
                passphrase = op_read(
                    f"op://{OP_VAULT}/{OP_ITEM}/{OP_FIELD_API_PASSPHRASE}"
                )
            except Exception:  # noqa: BLE001
                passphrase = ""  # Optional for basic API

    return {
        "key": key,
        "secret": secret,
        "passphrase": passphrase or "",
    }


def sign_request(
    secret: str,
    timestamp: str,
    method: str,
    path: str,
    body: str = "",
) -> str:
    """
    Coinbase Consumer API v2 signature:
      message = timestamp + method + requestPath + body
      CB-ACCESS-SIGN = HMAC-SHA256(secret, message), hex
    """
    message = f"{timestamp}{method}{path}{body}".encode()
    sig = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return sig


def coinbase_get_accounts(api_key: str, api_secret: str, passphrase: str = "") -> dict:
    path = "/v2/accounts"  # Consumer API v2
    url = COINBASE_API_BASE + path
    ts = str(int(time.time()))  # Consumer API uses integer timestamp
    method = "GET"
    body = ""

    signature = sign_request(api_secret, ts, method, path, body)

    headers = {
        "CB-ACCESS-KEY": api_key,
        "CB-ACCESS-SIGN": signature,
        "CB-ACCESS-TIMESTAMP": ts,
        "CB-VERSION": "2021-01-01",
        "Accept": "application/json",
    }
    # Ignore passphrase for Consumer API

    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def parse_stx_balance(accounts_resp) -> float:
    total_stx = 0.0

    # Consumer API v2 returns {"data": [...]}
    accounts = accounts_resp.get("data", [])

    for acc in accounts:
        # v2 API: currency is nested or a string
        currency_obj = acc.get("currency")
        if isinstance(currency_obj, dict):
            currency = currency_obj.get("code", "")
        else:
            currency = currency_obj or ""

        if currency != "STX":
            continue

        # v2 API: balance is an object with "amount" and "currency"
        balance_obj = acc.get("balance")
        if isinstance(balance_obj, dict):
            balance_str = balance_obj.get("amount", "0")
        else:
            balance_str = str(balance_obj or "0")

        try:
            balance = float(balance_str)
        except (TypeError, ValueError):
            balance = 0.0

        total_stx += balance

        acc_name = acc.get("name", acc.get("id", "unknown"))
        print(f"Account: {acc_name}")
        print(f"  Currency: {currency}")
        print(f"  Balance: {balance:,.6f} STX")
        print()

    return total_stx


def main() -> int:
    print("Fetching Coinbase STX balances\n")
    creds = get_coinbase_creds()

    try:
        accounts = coinbase_get_accounts(
            creds["key"], creds["secret"], creds["passphrase"]
        )
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"Coinbase API error: {e}")

    total_stx = parse_stx_balance(accounts)

    print("=" * 72)
    print(f"TOTAL Coinbase STX: {total_stx:,.6f} STX")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
