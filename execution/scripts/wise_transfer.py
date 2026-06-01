#!/usr/bin/env python3
"""
Wise Transfer Script

Make transfers via Wise API. Amount specified is the target amount (what recipient receives).
Fees are added on top, not deducted from the principal.

Usage:
    python wise_transfer.py <target_amount> <recipient_iban> [--reference <reference>] [--dry-run]

Examples:
    python wise_transfer.py 500 ES1234567890123456789012 --reference "Donación AVVAAPSJ" --dry-run
    python wise_transfer.py 500 ES1234567890123456789012 --reference "Donación AVVAAPSJ"

Note: The amount specified is what the recipient will receive. Fees are calculated and added on top.
"""

import argparse
import hashlib
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# Import currency converter from import_transactions
sys.path.insert(0, str(Path(__file__).parent))
try:
    from import_transactions import CurrencyConverter
except ImportError:
    CurrencyConverter = None

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load environment variables
load_dotenv(PROJECT_ROOT / ".env")

from scripts.config import DATA_DIR

# Configuration
WISE_API_TOKEN = os.getenv("WISE_API_TOKEN")
WISE_API_BASE = "https://api.transferwise.com"

# Source account details (Ibercaja)
# IBAN stored in $DATA_DIR/accounts/accounts.parquet - query for account with wallet_name='Ibercaja'
# Use environment variable or query parquet file via MCP
SOURCE_IBAN = os.getenv(
    "IBERCAJA_IBAN"
)  # Must be set in .env or query accounts.parquet via MCP
if not SOURCE_IBAN:
    print("Error: IBERCAJA_IBAN not found in environment variables", file=sys.stderr)
    print(
        "Set it in .env file or query $DATA_DIR/accounts/accounts.parquet via MCP",
        file=sys.stderr,
    )
    sys.exit(1)

# Data file paths
TRANSACTIONS_FILE = DATA_DIR / "transactions" / "transactions.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"


def get_profile_id(api_token: str) -> str | None:
    """Get Wise profile ID from API token."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    response = requests.get(f"{WISE_API_BASE}/v1/profiles", headers=headers)

    if response.status_code != 200:
        print(f"Error getting profile: {response.status_code} - {response.text}")
        return None

    profiles = response.json()
    if not profiles:
        print("No profiles found")
        return None

    # Return first personal profile or business profile
    profile = profiles[0]
    return profile.get("id")


def get_quote(
    source_currency: str,
    target_currency: str,
    amount: float,
    profile_id: str,
    api_token: str,
    use_target_amount: bool = True,
) -> dict | None:
    """Get a quote for the transfer.

    Args:
        use_target_amount: If True, amount is the target amount (recipient receives this amount, fees added on top).
                          If False, amount is the source amount (fees deducted from this amount).
    """
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "sourceCurrency": source_currency,
        "targetCurrency": target_currency,
        "profile": profile_id,
    }

    if use_target_amount:
        # Specify target amount so recipient receives exact amount, fees added on top
        payload["targetAmount"] = amount
    else:
        # Specify source amount, fees deducted from amount
        payload["sourceAmount"] = amount

    response = requests.post(
        f"{WISE_API_BASE}/v2/quotes", headers=headers, json=payload
    )

    if response.status_code != 200:
        print(f"Error getting quote: {response.status_code} - {response.text}")
        return None

    return response.json()


def _normalize_iban(iban: str) -> str:
    """Remove spaces; API expects IBAN without spaces."""
    return (iban or "").replace(" ", "").strip()


def get_recipient_account(
    iban: str,
    currency: str,
    profile_id: str,
    api_token: str,
    account_holder_name: str | None = None,
    legal_type: str = "PRIVATE",
) -> str | None:
    """Create or get recipient account ID. Use legal_type='BUSINESS' for companies."""
    iban_clean = _normalize_iban(iban)
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    # Check existing accounts
    response = requests.get(
        f"{WISE_API_BASE}/v1/accounts", headers=headers, params={"profile": profile_id}
    )

    if response.status_code == 200:
        accounts = response.json()
        for account in accounts:
            existing_iban = account.get("details", {}).get("iban") or ""
            if _normalize_iban(existing_iban) == iban_clean:
                return account.get("id")

    # Create new recipient account (Wise expects IBAN without spaces)
    payload = {
        "currency": currency,
        "type": "iban",
        "profile": profile_id,
        "ownedByCustomer": False,
        "details": {
            "legalType": legal_type,
            "iban": iban_clean,
        },
    }

    if account_holder_name:
        payload["details"]["accountHolderName"] = account_holder_name

    response = requests.post(
        f"{WISE_API_BASE}/v1/accounts", headers=headers, json=payload
    )

    if response.status_code not in [200, 201]:
        print(
            f"Error creating recipient account: {response.status_code} - {response.text}"
        )
        return None

    account_data = response.json()
    return account_data.get("id")


def create_transfer(
    quote_id: str, recipient_account_id: str, reference: str, api_token: str
) -> dict | None:
    """Create a transfer."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    import uuid

    customer_transaction_id = str(uuid.uuid4())

    payload = {
        "targetAccount": recipient_account_id,
        "quoteUuid": quote_id,
        "customerTransactionId": customer_transaction_id,
        "details": {"reference": reference},
    }

    response = requests.post(
        f"{WISE_API_BASE}/v1/transfers", headers=headers, json=payload
    )

    if response.status_code not in [200, 201]:
        print(f"Error creating transfer: {response.status_code} - {response.text}")
        return None

    return response.json()


def save_transfer_as_transaction(
    transfer_id: str,
    amount: float,
    currency: str,
    recipient_iban: str,
    recipient_name: str | None,
    reference: str,
    fee: float | None = None,
) -> bool:
    """Save Wise transfer as a transaction in the transactions database."""
    if not TRANSACTIONS_FILE.parent.exists():
        TRANSACTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Load existing transactions
    if TRANSACTIONS_FILE.exists():
        df = pd.read_parquet(TRANSACTIONS_FILE)
    else:
        df = pd.DataFrame()

    # Generate transaction ID from transfer details
    hash_string = (
        f"wise_transfer_{transfer_id}_{amount}_{currency}_{datetime.now().isoformat()}"
    )
    transaction_id = hashlib.sha256(hash_string.encode()).hexdigest()[:16]

    # Check if transaction already exists
    if not df.empty and "transaction_id" in df.columns:
        existing = df[df["transaction_id"] == transaction_id]
        if not existing.empty:
            print(f"Transaction already exists for transfer {transfer_id}")
            return True

    # Calculate total amount (negative for outgoing transfer, include fees)
    # Amount already includes fees when using targetAmount, so use it directly
    total_amount = -(amount + (fee if fee is not None else 0))

    # Convert to USD if converter available
    try:
        if CurrencyConverter:
            converter = CurrencyConverter()
            amount_usd = converter.convert_to_usd(
                abs(total_amount), currency, date.today().isoformat()
            )
            if total_amount < 0:
                amount_usd = -amount_usd
        else:
            # Fallback: use amount as USD if same currency or approximate
            amount_usd = (
                total_amount if currency == "USD" else total_amount * 1.0
            )  # Placeholder
    except Exception as e:
        print(f"Warning: Could not convert to USD: {e}, using original amount")
        amount_usd = total_amount

    # Build description
    description_parts = [f"Wise transfer to {recipient_name or recipient_iban}"]
    if reference:
        description_parts.append(f"Ref: {reference}")
    if fee:
        description_parts.append(f"Fee: {fee} {currency}")
    description = " | ".join(description_parts)

    today_str = date.today().isoformat()
    transaction_record = {
        "transaction_id": transaction_id,
        "transaction_date": today_str,
        "posting_date": today_str,
        "amount_usd": amount_usd,
        "amount_original": total_amount,
        "currency_original": currency,
        "description": description,
        "category": "transfer",  # Default; can be overridden for specific cases like therapy, donations
        "account_id": "wise",
        "bank_provider": "wise",
        "import_date": today_str,
        "import_source_file": f"wise_transfer_{transfer_id}",
    }

    # Create snapshot before modification
    if not df.empty:
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        snapshot_path = SNAPSHOTS_DIR / f"transactions-{timestamp}.parquet"
        df.to_parquet(snapshot_path, index=False)
        print(f"Created snapshot: {snapshot_path}")

    # Add new transaction
    new_df = pd.DataFrame([transaction_record])
    combined_df = pd.concat([df, new_df], ignore_index=True)

    # Save to parquet
    combined_df.to_parquet(TRANSACTIONS_FILE, index=False)
    print(f"Saved transaction to {TRANSACTIONS_FILE}")

    return True


def fund_transfer(
    transfer_id: str, api_token: str, profile_id: str, currency: str = "EUR"
) -> bool:
    """Fund the transfer from Wise balance using v3 API."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    # Fund using v3 profiles endpoint with BALANCE type
    payment_payload = {"type": "BALANCE"}

    payment_response = requests.post(
        f"{WISE_API_BASE}/v3/profiles/{profile_id}/transfers/{transfer_id}/payments",
        headers=headers,
        json=payment_payload,
    )

    if payment_response.status_code in [200, 201]:
        payment_data = payment_response.json()
        payment_status = payment_data.get("status", "UNKNOWN")
        balance_transaction_id = payment_data.get("balanceTransactionId")
        print(f"  Payment status: {payment_status}")
        if balance_transaction_id:
            print(f"  Balance transaction ID: {balance_transaction_id}")
        return True
    else:
        print(
            f"Error funding transfer: {payment_response.status_code} - {payment_response.text}"
        )
        return False


def main():
    parser = argparse.ArgumentParser(description="Make a transfer via Wise API")
    parser.add_argument(
        "amount",
        type=float,
        help="Target amount (what recipient will receive; fees added on top)",
    )
    parser.add_argument("recipient_iban", type=str, help="Recipient IBAN")
    parser.add_argument(
        "--reference", type=str, default="", help="Transfer reference/description"
    )
    parser.add_argument(
        "--account-holder-name",
        type=str,
        default=None,
        help="Account holder name (required for some accounts)",
    )
    parser.add_argument(
        "--business",
        action="store_true",
        help="Recipient is a business (use BUSINESS legal type for Wise)",
    )
    parser.add_argument(
        "--source-currency",
        type=str,
        default="EUR",
        help="Source currency (default: EUR)",
    )
    parser.add_argument(
        "--target-currency",
        type=str,
        default="EUR",
        help="Target currency (default: EUR)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Dry run (do not execute)"
    )

    args = parser.parse_args()

    if not WISE_API_TOKEN:
        print(
            "Error: WISE_API_TOKEN not found in environment variables", file=sys.stderr
        )
        print("Set it in .env file or export WISE_API_TOKEN", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("DRY RUN MODE - No transfer will be executed")
        print()

    print("Creating transfer:")
    print(f"  Target amount (recipient receives): {args.amount} {args.target_currency}")
    print(f"  Recipient IBAN: {args.recipient_iban}")
    print(f"  Reference: {args.reference}")
    print("  Note: Fees will be added on top of the target amount")
    print()

    if args.dry_run:
        print("Would execute:")
        print("1. Get profile ID")
        print("2. Get quote")
        print("3. Create/get recipient account")
        print("4. Create transfer")
        print("5. Fund transfer")
        return

    # Get profile ID
    print("Getting profile ID...")
    profile_id = get_profile_id(WISE_API_TOKEN)
    if not profile_id:
        print("Failed to get profile ID", file=sys.stderr)
        sys.exit(1)
    print(f"  Profile ID: {profile_id}")

    # Get quote (using targetAmount so recipient receives exact amount, fees added on top)
    print(
        f"\nGetting quote (target amount: {args.amount} {args.target_currency}, fees will be added on top)..."
    )
    quote = get_quote(
        args.source_currency,
        args.target_currency,
        args.amount,
        profile_id,
        WISE_API_TOKEN,
        use_target_amount=True,
    )
    if not quote:
        print("Failed to get quote", file=sys.stderr)
        sys.exit(1)

    quote_id = quote.get("id")
    fee = quote.get("fee")
    rate = quote.get("rate")
    source_amount = quote.get("sourceAmount")
    target_amount = (
        quote.get("paymentOptions", [{}])[0].get("targetAmount")
        if quote.get("paymentOptions")
        else quote.get("targetAmount")
    )

    print(f"  Quote ID: {quote_id}")
    print(f"  Fee: {fee} {args.source_currency}")
    print(f"  Exchange rate: {rate}")
    print(f"  You'll send: {source_amount} {args.source_currency} (including fees)")
    print(f"  Recipient will receive: {target_amount} {args.target_currency}")

    # Get or create recipient account
    legal_type = "BUSINESS" if args.business else "PRIVATE"
    print("\nSetting up recipient account...")
    recipient_account_id = get_recipient_account(
        args.recipient_iban,
        args.target_currency,
        profile_id,
        WISE_API_TOKEN,
        args.account_holder_name,
        legal_type=legal_type,
    )
    if not recipient_account_id:
        print("Failed to set up recipient account", file=sys.stderr)
        sys.exit(1)
    print(f"  Recipient account ID: {recipient_account_id}")

    # Create transfer
    print("\nCreating transfer...")
    transfer = create_transfer(
        quote_id, recipient_account_id, args.reference or "Transfer", WISE_API_TOKEN
    )
    if not transfer:
        print("Failed to create transfer", file=sys.stderr)
        sys.exit(1)

    transfer_id = transfer.get("id")
    transfer_status = transfer.get("status")

    print(f"  Transfer ID: {transfer_id}")
    print(f"  Status: {transfer_status}")

    # Save as transaction
    print("\nSaving transfer as transaction...")
    save_transfer_as_transaction(
        transfer_id=transfer_id,
        amount=source_amount or args.amount,  # Use actual source amount if available
        currency=args.source_currency,
        recipient_iban=args.recipient_iban,
        recipient_name=args.account_holder_name,
        reference=args.reference or "Transfer",
        fee=fee,
    )

    # Fund transfer from Wise balance
    print(f"\nFunding transfer from Wise {args.source_currency} balance...")
    funded = fund_transfer(
        transfer_id, WISE_API_TOKEN, profile_id, args.source_currency
    )
    if funded:
        print("  Transfer funded from Wise balance")
    else:
        print("  Warning: Funding failed, may require manual action")

    print(f"\n✓ Transfer created: {transfer_id}")
    print(f"  Check status at: https://wise.com/app#/transfers/{transfer_id}")


if __name__ == "__main__":
    main()
