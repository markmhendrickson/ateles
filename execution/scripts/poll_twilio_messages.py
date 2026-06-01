#!/usr/bin/env python3
"""
Poll Twilio API for SMS messages and store in local parquet files.

Fetches historical messages from Twilio and stores them in data/messages/messages.parquet.
Skips messages that already exist (based on twilio_message_sid).

Usage:
    python scripts/poll_twilio_messages.py [--hours 24] [--phone-number +16503198857]
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

try:
    from twilio.base.exceptions import TwilioException, TwilioRestException
    from twilio.rest import Client
except ImportError:
    print("ERROR: Twilio Python SDK not installed.")
    print("Install it with: pip install twilio")
    sys.exit(1)

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load environment variables
load_dotenv(PROJECT_ROOT / ".env")

from scripts.config import DATA_DIR

# Data directory
MESSAGES_DIR = DATA_DIR / "messages"
MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
MESSAGES_FILE = MESSAGES_DIR / "messages.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def create_snapshot(file_path: Path) -> Path:
    """Create a timestamped snapshot of a parquet file."""
    if not file_path.exists():
        return None

    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    filename = file_path.stem
    snapshot_path = SNAPSHOTS_DIR / f"{filename}-{timestamp}.parquet"

    # Copy file
    import shutil

    shutil.copy2(file_path, snapshot_path)

    return snapshot_path


def get_twilio_client() -> Client | None:
    """Get Twilio client from environment variables."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        print("ERROR: Twilio credentials not found in environment variables.")
        print("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env file")
        return None

    try:
        client = Client(account_sid, auth_token)
        # Test connection
        account = client.api.accounts(account_sid).fetch()
        print(f"✓ Connected to Twilio account: {account.friendly_name}")
        return client
    except TwilioException as e:
        print(f"ERROR: Failed to connect to Twilio: {e}")
        return None


def fetch_messages(
    client: Client,
    phone_number: str | None = None,
    hours: int | None = None,
    days: int | None = None,
    all_history: bool = False,
) -> list[dict]:
    """Fetch messages from Twilio API."""
    messages = []

    try:
        # Calculate date range
        if all_history:
            since = None  # Fetch all messages
            print("Fetching ALL historical messages (this may take a while)...")
        elif days:
            since = datetime.utcnow() - timedelta(days=days)
            print(f"Fetching messages from last {days} days...")
        elif hours:
            since = datetime.utcnow() - timedelta(hours=hours)
            print(f"Fetching messages from last {hours} hours...")
        else:
            since = datetime.utcnow() - timedelta(hours=24)  # Default: 24 hours
            print("Fetching messages from last 24 hours...")

        # Fetch messages with pagination
        all_messages = []

        if phone_number:
            # Messages to/from specific number
            if since:
                incoming = client.messages.list(to=phone_number, date_sent_after=since)
                outgoing = client.messages.list(
                    from_=phone_number, date_sent_after=since
                )
            else:
                incoming = client.messages.list(to=phone_number)
                outgoing = client.messages.list(from_=phone_number)

            # Collect all pages
            for msg in incoming:
                all_messages.append(msg)
            for msg in outgoing:
                all_messages.append(msg)
        else:
            # All messages
            if since:
                message_list = client.messages.list(date_sent_after=since)
            else:
                message_list = client.messages.list()

            # Collect all pages
            for msg in message_list:
                all_messages.append(msg)

        # Convert to dictionaries
        for msg in all_messages:
            messages.append(
                {
                    "twilio_message_sid": msg.sid,
                    "direction": (
                        "inbound" if msg.direction == "inbound" else "outbound"
                    ),
                    "from_number": msg.from_,
                    "to_number": msg.to,
                    "body": msg.body or "",
                    "status": msg.status,
                    "error_code": str(msg.error_code) if msg.error_code else "",
                    "error_message": msg.error_message or "",
                    "num_media": int(msg.num_media) if msg.num_media else 0,
                    "price": str(msg.price) if msg.price else "",
                    "price_unit": msg.price_unit or "",
                    "date_sent": msg.date_sent,
                    "date_created": msg.date_created,
                    "date_updated": msg.date_updated,
                    "account_sid": msg.account_sid,
                }
            )

        print(f"✓ Fetched {len(messages)} message(s) from Twilio")
        return messages

    except TwilioRestException as e:
        print(f"ERROR: Failed to fetch messages: {e}")
        return []


def save_messages_to_parquet(new_messages: list[dict]) -> tuple[int, int]:
    """Save messages to parquet file, skipping duplicates."""
    # Create snapshot before modification
    if MESSAGES_FILE.exists():
        snapshot_path = create_snapshot(MESSAGES_FILE)
        if snapshot_path:
            print(f"✓ Created snapshot: {snapshot_path}")

    # Read existing data or create new
    if MESSAGES_FILE.exists():
        df_existing = pd.read_parquet(MESSAGES_FILE)
        existing_sids = set(df_existing["twilio_message_sid"].values)
    else:
        df_existing = pd.DataFrame()
        existing_sids = set()

    # Filter out duplicates
    new_messages_filtered = [
        msg for msg in new_messages if msg["twilio_message_sid"] not in existing_sids
    ]

    if not new_messages_filtered:
        print("✓ No new messages to add (all already exist)")
        return 0, len(new_messages)

    # Prepare new rows
    rows = []
    for msg in new_messages_filtered:
        import uuid

        row = {
            "message_id": str(uuid.uuid4())[:16],
            "twilio_message_sid": msg["twilio_message_sid"],
            "direction": msg["direction"],
            "from_number": msg["from_number"],
            "to_number": msg["to_number"],
            "body": msg["body"],
            "status": msg["status"],
            "error_code": msg["error_code"],
            "error_message": msg["error_message"],
            "num_media": msg["num_media"],
            "price": msg["price"],
            "price_unit": msg["price_unit"],
            "date_sent": (
                pd.to_datetime(msg["date_sent"], errors="coerce")
                if msg["date_sent"]
                else None
            ),
            "date_created": (
                pd.to_datetime(msg["date_created"], errors="coerce")
                if msg["date_created"]
                else datetime.utcnow()
            ),
            "date_updated": (
                pd.to_datetime(msg["date_updated"], errors="coerce")
                if msg["date_updated"]
                else datetime.utcnow()
            ),
            "account_sid": msg["account_sid"],
            "import_date": date.today(),
            "import_source": "polling",
        }
        rows.append(row)

    # Create DataFrame from new rows
    df_new = pd.DataFrame(rows)

    # Combine with existing
    if not df_existing.empty:
        df = pd.concat([df_existing, df_new], ignore_index=True)
    else:
        df = df_new

    # Write back
    df.to_parquet(MESSAGES_FILE, index=False)

    print(f"✓ Added {len(new_messages_filtered)} new message(s)")
    print(f"✓ Skipped {len(new_messages) - len(new_messages_filtered)} duplicate(s)")
    print(f"✓ Total messages in database: {len(df)}")

    return len(new_messages_filtered), len(new_messages) - len(new_messages_filtered)


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Poll Twilio for SMS messages")
    parser.add_argument("--hours", type=int, help="Hours of history to fetch")
    parser.add_argument("--days", type=int, help="Days of history to fetch")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch ALL historical messages (may take a long time)",
    )
    parser.add_argument(
        "--phone-number",
        type=str,
        help="Phone number to filter by (e.g., +16503198857)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Twilio SMS Message Polling")
    print("=" * 60)

    # Get Twilio client
    client = get_twilio_client()
    if not client:
        return 1

    # Fetch messages
    if args.phone_number:
        print(f"Filtering by phone number: {args.phone_number}")

    messages = fetch_messages(
        client,
        args.phone_number,
        hours=args.hours,
        days=args.days,
        all_history=args.all,
    )

    if not messages:
        print("\nNo messages found")
        return 0

    # Save to parquet
    print(f"\nSaving messages to {MESSAGES_FILE}...")
    added, skipped = save_messages_to_parquet(messages)

    print("\n" + "=" * 60)
    print("Polling complete")
    print("=" * 60)
    print(f"Added: {added}")
    print(f"Skipped (duplicates): {skipped}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
