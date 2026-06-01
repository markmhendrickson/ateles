"""
Fetch historical STX-USDC orders from Coinbase Advanced Trade API.

This script:
- Reads API credentials from environment or 1Password
- Fetches all historical orders for STX-USDC pair
- Stores orders in parquet via MCP

Requirements:
- requests, python-dotenv
- Coinbase Advanced Trade API credentials
- MCP parquet server access
"""

import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False
    print(
        "WARNING: jwt and cryptography not installed. Install with: pip install pyjwt cryptography"
    )

try:
    from dotenv import load_dotenv

    repo_root = Path(__file__).parent.parent
    dotenv_path = repo_root / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path)
        print(f"Loaded environment variables from {dotenv_path}")
except ImportError:
    print("python-dotenv not installed. Install with: pip install python-dotenv")

try:
    import requests
except ImportError:
    print("ERROR: requests module not installed.")
    print("Install with: pip install requests")
    sys.exit(1)


# Coinbase Cloud API base URL
COINBASE_CLOUD_API_BASE = "https://api.coinbase.com/api/v3/brokerage"

# 1Password item reference configuration
OP_VAULT = "Wallets"
OP_ITEM = "gstd7jfxcadhjuwjo2enfwssiq"  # Item ID for "Wallet 9: Coinbase"
OP_FIELD_API_KEY = "API key name"
OP_FIELD_API_SECRET = "API private key"
OP_FIELD_API_PASSPHRASE = "Valor de inicialización secreto"


def op_read(field_ref: str) -> str:
    """Read a secret from 1Password using the op CLI."""
    try:
        result = subprocess.run(
            ["op", "read", field_ref],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as e:
        raise RuntimeError(f"1Password CLI error for {field_ref}: {e}") from e

    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"Empty value returned for 1Password ref: {field_ref}")
    return value


def get_coinbase_creds() -> dict[str, str]:
    """Resolve Coinbase Advanced Trade API credentials from environment or 1Password."""
    # Try Advanced Trade credentials first
    key = os.getenv("COINBASE_API_KEY_ADVANCED")
    secret = os.getenv("COINBASE_API_SECRET_ADVANCED")
    passphrase = os.getenv("COINBASE_API_PASSPHRASE")

    if key and secret:
        print("Using Coinbase Advanced Trade credentials from environment variables")
    else:
        # Fallback to regular credentials
        key = os.getenv("COINBASE_API_KEY")
        secret = os.getenv("COINBASE_API_SECRET")
        if key and secret:
            print("Using Coinbase credentials from environment variables (fallback)")
        else:
            print(
                "Coinbase credentials not found in environment, attempting 1Password..."
            )
            try:
                key = op_read(f"op://{OP_VAULT}/{OP_ITEM}/{OP_FIELD_API_KEY}")
                secret = op_read(f"op://{OP_VAULT}/{OP_ITEM}/{OP_FIELD_API_SECRET}")
                print("Successfully retrieved credentials from 1Password")
            except Exception as e:
                print(f"ERROR: Could not retrieve credentials from 1Password: {e}")
                print("\nTo fix this, either:")
                print("1. Run: python execution/scripts/op_sync_env_from_1password.py")
                print(
                    "2. Or set environment variables: COINBASE_API_KEY_ADVANCED, COINBASE_API_SECRET_ADVANCED"
                )
                sys.exit(1)

        if not passphrase:
            try:
                passphrase = op_read(
                    f"op://{OP_VAULT}/{OP_ITEM}/{OP_FIELD_API_PASSPHRASE}"
                )
            except Exception:
                passphrase = ""

    if not key or not secret:
        print("ERROR: API key or secret not found")
        sys.exit(1)

    return {
        "key": key,
        "secret": secret,
        "passphrase": passphrase or "",
    }


def sign_request_advanced_trade_ed25519(
    secret: str,
    timestamp: str,
    method: str,
    path: str,
    body: str = "",
) -> str:
    """
    Coinbase Advanced Trade API uses Ed25519 signing.

    Signature format:
      message = timestamp + method + requestPath + body
      signature = Ed25519_sign(private_key, message), base64

    Note: secret is base64-encoded Ed25519 private key
    """
    if not JWT_AVAILABLE:
        raise RuntimeError(
            "cryptography library required for Ed25519. Install: pip install cryptography"
        )

    # Decode base64 secret to get Ed25519 private key
    try:
        secret_bytes = base64.b64decode(secret)
        # Ed25519 private key is 32 bytes
        # If secret is 64 bytes, use first 32 bytes (private key)
        # If secret is 32 bytes, use as-is
        if len(secret_bytes) == 64:
            private_key_bytes = secret_bytes[:32]
        elif len(secret_bytes) == 32:
            private_key_bytes = secret_bytes
        else:
            raise ValueError(
                f"Unexpected secret length: {len(secret_bytes)} bytes (expected 32 or 64)"
            )

        # Load Ed25519 private key
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    except Exception as e:
        raise RuntimeError(f"Failed to load Ed25519 private key: {e}") from e

    # Build message: timestamp + method + path + body
    message = f"{timestamp}{method}{path}{body}".encode()

    # Sign with Ed25519
    signature_bytes = private_key.sign(message)

    # Return base64-encoded signature
    return base64.b64encode(signature_bytes).decode("utf-8")


def coinbase_list_orders(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    product_id: str = "STX-USDC",
    limit: int = 250,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """
    Fetch orders from Coinbase Cloud API (Advanced Trade).

    Args:
        product_id: Trading pair (default: STX-USDC)
        limit: Max orders per page (default: 250, max: 250)
        start_date: ISO 8601 start date (optional)
        end_date: ISO 8601 end date (optional)
    """
    # Full path for JWT signing
    api_path = "/api/v3/brokerage/orders/historical/batch"
    base_url = "https://api.coinbase.com" + api_path

    # Build query parameters
    params = {
        "product_id": product_id,
        "limit": str(limit),
        "order_status": "ALL",  # FILLED, OPEN, CANCELLED, EXPIRED, FAILED
    }

    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date

    method = "GET"
    body = ""

    # Headers for Advanced Trade API (HMAC signing)
    timestamp = str(int(time.time()))
    method = "GET"
    body = ""

    # Path for signing (without query string - query goes in URL params)
    # Advanced Trade API signs just the endpoint path
    sign_path = api_path

    # Sign request (path without query string) - Ed25519
    signature = sign_request_advanced_trade_ed25519(
        api_secret, timestamp, method, sign_path, body
    )

    headers = {
        "CB-ACCESS-KEY": api_key,
        "CB-ACCESS-SIGN": signature,
        "CB-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }

    # Passphrase not used for Advanced Trade API
    # if api_passphrase:
    #     headers["CB-ACCESS-PASSPHRASE"] = api_passphrase

    all_orders = []
    cursor = None

    while True:
        current_params = params.copy()
        if cursor:
            current_params["cursor"] = cursor

        # Regenerate timestamp for each request
        timestamp = str(int(time.time()))

        # Sign request with Ed25519 (path without query string)
        signature = sign_request_advanced_trade_ed25519(
            api_secret, timestamp, method, sign_path, body
        )

        # Update headers
        headers["CB-ACCESS-TIMESTAMP"] = timestamp
        headers["CB-ACCESS-SIGN"] = signature

        # Use base URL without query for requests (query goes in params)
        url = base_url

        # Debug: Print first request details
        if len(all_orders) == 0:
            print(f"Sign path: {sign_path}")
            print(f"Query params: {current_params}")
            print(f"Signature (first 30 chars): {signature[:30]}...")
            print(f"Headers: {list(headers.keys())}")

        # Make request - params will be added as query string by requests
        resp = requests.get(url, headers=headers, params=current_params, timeout=30)

        if resp.status_code != 200:
            print(f"Error response: {resp.status_code}")
            print(f"Response body: {resp.text[:500]}")
            resp.raise_for_status()

        data = resp.json()

        orders = data.get("orders", [])
        if not orders:
            break

        all_orders.extend(orders)
        print(f"Fetched {len(orders)} orders (total: {len(all_orders)})")

        # Check for pagination
        has_next = data.get("has_next", False)
        cursor = data.get("cursor")

        if not has_next or not cursor:
            break

        time.sleep(0.5)  # Rate limit protection

    return all_orders


def parse_order_for_parquet(order: dict) -> dict:
    """Convert Coinbase order to parquet schema format."""
    order_id = order.get("order_id", "")
    created_time = order.get("created_time", "")
    product_id = order.get("product_id", "")
    side = order.get("side", "")
    order_type = order.get("order_type", "")
    order_status = order.get("order_status", "")

    # Parse price and size
    price_str = order.get("average_filled_price") or order.get("limit_price", "0")
    size_str = order.get("filled_size") or order.get("order_size", "0")

    try:
        price = float(price_str)
    except (ValueError, TypeError):
        price = 0.0

    try:
        amount = float(size_str)
    except (ValueError, TypeError):
        amount = 0.0

    # Parse date
    try:
        if created_time:
            dt = datetime.fromisoformat(created_time.replace("Z", "+00:00"))
            date_str = dt.date().isoformat()
        else:
            date_str = datetime.now().date().isoformat()
    except Exception:
        date_str = datetime.now().date().isoformat()

    # Build notes with full context
    notes_parts = [
        "Coinbase Advanced Trade order",
        f"Side: {side}",
        f"Type: {order_type}",
        f"Status: {order_status}",
        f"Created: {created_time}",
    ]

    if order.get("filled_size"):
        notes_parts.append(f"Filled: {order.get('filled_size')} STX")
    if order.get("average_filled_price"):
        notes_parts.append(f"Avg Price: {order.get('average_filled_price')} USDC")

    notes = " | ".join(notes_parts)

    return {
        "order_id": order_id,
        "name": product_id,
        "status": order_status.lower(),
        "accounts": "Coinbase",
        "amount": amount,
        "asset_type": "STX",
        "order_type": f"{order_type}, {side}",
        "price": price,
        "url": "",
        "date": date_str,
        "notes": notes,
        "import_date": datetime.now().date().isoformat(),
        "import_source_file": "coinbase_api_historical",
    }


def main() -> int:
    print("Fetching historical STX-USDC orders from Coinbase Cloud API\n")

    creds = get_coinbase_creds()

    if not creds["passphrase"]:
        print("Note: No passphrase provided. Proceeding without passphrase.")

    try:
        # First test authentication with a simple endpoint
        print("Testing authentication...")
        test_path = "/api/v3/brokerage/products/STX-USDC"
        test_url = "https://api.coinbase.com" + test_path
        method = "GET"
        test_timestamp = str(int(time.time()))
        test_body = ""

        # Sign with just the path (no query string) - Ed25519
        test_signature = sign_request_advanced_trade_ed25519(
            creds["secret"], test_timestamp, method, test_path, test_body
        )

        test_headers = {
            "CB-ACCESS-KEY": creds["key"],
            "CB-ACCESS-SIGN": test_signature,
            "CB-ACCESS-TIMESTAMP": test_timestamp,
            "Content-Type": "application/json",
        }

        # Passphrase not used
        # if creds["passphrase"]:
        #     test_headers["CB-ACCESS-PASSPHRASE"] = creds["passphrase"]

        test_resp = requests.get(test_url, headers=test_headers, timeout=10)
        print(f"Test endpoint status: {test_resp.status_code}")
        if test_resp.status_code == 200:
            print("✓ Authentication successful!")
        else:
            print(f"Test response: {test_resp.text[:200]}")

        print("\nFetching all historical orders...")
        orders = coinbase_list_orders(
            creds["key"],
            creds["secret"],
            creds["passphrase"],
            product_id="STX-USDC",
        )

        print(f"\nTotal orders fetched: {len(orders)}")

        if not orders:
            print("No orders found.")
            return 0

        # Convert to parquet format
        print("\nConverting orders to parquet format...")
        parquet_orders = [parse_order_for_parquet(order) for order in orders]

        print(f"\nOrders to store: {len(parquet_orders)}")
        print("\nFirst few orders:")
        for i, order in enumerate(parquet_orders[:5], 1):
            print(
                f"{i}. {order['order_id']}: {order['amount']} STX @ {order['price']} USDC ({order['status']})"
            )

        # Store via MCP (this would be done by the agent, not the script)
        print("\n" + "=" * 72)
        print("Orders ready for storage via MCP parquet.")
        print("Run this script and then use MCP to store the orders.")
        print("=" * 72)

        # Output JSON for manual review or MCP import
        from scripts.config import get_data_dir

        output_file = get_data_dir() / "imports" / "coinbase_orders_historical.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "w") as f:
            json.dump(parquet_orders, f, indent=2)

        print(f"\nOrders saved to: {output_file}")
        print(f"Total orders: {len(parquet_orders)}")

        return 0

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
