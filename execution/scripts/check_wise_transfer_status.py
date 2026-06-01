#!/usr/bin/env python3
"""
Check Wise Transfer Status

Query Wise API to check the status of transfers.

Usage:
    python check_wise_transfer_status.py <transfer_id> [<transfer_id> ...]
    python check_wise_transfer_status.py --from-transactions --date 2025-12-18
"""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load environment variables
load_dotenv(PROJECT_ROOT / ".env")

# Configuration
WISE_API_TOKEN = os.getenv("WISE_API_TOKEN")
WISE_API_BASE = "https://api.transferwise.com"


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


def get_transfer_status(
    transfer_id: str, api_token: str, profile_id: str
) -> dict | None:
    """Get transfer status from Wise API."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    response = requests.get(
        f"{WISE_API_BASE}/v1/transfers/{transfer_id}",
        headers=headers,
        params={"profile": profile_id},
    )

    if response.status_code != 200:
        print(
            f"Error getting transfer {transfer_id}: {response.status_code} - {response.text}"
        )
        return None

    return response.json()


def extract_transfer_id_from_source_file(source_file: str) -> str | None:
    """Extract transfer ID from import_source_file format 'wise_transfer_<id>'."""
    if source_file.startswith("wise_transfer_"):
        return source_file.replace("wise_transfer_", "")
    return None


def get_transfers_from_transactions(date: str | None = None) -> list[str]:
    """Get transfer IDs from transactions parquet file."""
    from scripts.config import DATA_DIR

    transactions_file = DATA_DIR / "transactions" / "transactions.parquet"

    if not transactions_file.exists():
        print(f"Transactions file not found: {transactions_file}")
        return []

    df = pd.read_parquet(transactions_file)

    # Filter for Wise transfers
    wise_transfers = df[df["bank_provider"] == "wise"].copy()

    # Filter by date if provided
    if date:
        # Convert date string to match transaction_date format
        wise_transfers = wise_transfers[
            wise_transfers["transaction_date"].astype(str) == date
        ]

    # Extract transfer IDs from import_source_file
    transfer_ids = []
    for source_file in wise_transfers["import_source_file"]:
        transfer_id = extract_transfer_id_from_source_file(source_file)
        if transfer_id:
            transfer_ids.append(transfer_id)

    return transfer_ids


def main():
    parser = argparse.ArgumentParser(description="Check Wise transfer status")
    parser.add_argument("transfer_ids", nargs="*", help="Transfer IDs to check")
    parser.add_argument(
        "--from-transactions",
        action="store_true",
        help="Get transfer IDs from transactions file",
    )
    parser.add_argument(
        "--date", type=str, help="Filter transactions by date (YYYY-MM-DD)"
    )

    args = parser.parse_args()

    if not WISE_API_TOKEN:
        print(
            "Error: WISE_API_TOKEN not found in environment variables", file=sys.stderr
        )
        print("Set it in .env file or export WISE_API_TOKEN", file=sys.stderr)
        sys.exit(1)

    # Get transfer IDs
    transfer_ids = list(args.transfer_ids) if args.transfer_ids else []

    if args.from_transactions:
        transaction_transfer_ids = get_transfers_from_transactions(args.date)
        transfer_ids.extend(transaction_transfer_ids)

    if not transfer_ids:
        print(
            "No transfer IDs provided. Use --from-transactions or provide transfer IDs as arguments."
        )
        sys.exit(1)

    # Remove duplicates
    transfer_ids = list(set(transfer_ids))

    print(f"Checking {len(transfer_ids)} transfer(s)...\n")

    # Get profile ID
    profile_id = get_profile_id(WISE_API_TOKEN)
    if not profile_id:
        print("Failed to get profile ID", file=sys.stderr)
        sys.exit(1)

    # Check each transfer
    for transfer_id in transfer_ids:
        print(f"Transfer ID: {transfer_id}")
        transfer = get_transfer_status(transfer_id, WISE_API_TOKEN, profile_id)

        if transfer:
            status = transfer.get("status")
            current_route = transfer.get("currentRoute")
            source_amount = transfer.get("sourceAmount")
            target_amount = transfer.get("targetAmount")
            source_currency = transfer.get("sourceCurrency")
            target_currency = transfer.get("targetCurrency")
            rate = transfer.get("rate")
            fee = transfer.get("fee")
            reference = transfer.get("details", {}).get("reference", "N/A")
            created = transfer.get("created")

            print(f"  Status: {status}")
            print(f"  Reference: {reference}")
            print(
                f"  Amount: {source_amount} {source_currency} → {target_amount} {target_currency}"
            )
            if rate:
                print(f"  Rate: {rate}")
            if fee:
                print(f"  Fee: {fee} {source_currency}")
            if created:
                print(f"  Created: {created}")
            if current_route:
                route_status = current_route.get("status")
                print(f"  Route Status: {route_status}")

            print(f"  URL: https://wise.com/app#/transfers/{transfer_id}")
        else:
            print("  Failed to retrieve transfer status")

        print()


if __name__ == "__main__":
    main()
