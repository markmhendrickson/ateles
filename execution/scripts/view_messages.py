#!/usr/bin/env python3
"""
View SMS messages with human-readable dates and formatting.

Usage:
    python execution/scripts/view_messages.py                    # Show recent messages
    python execution/scripts/view_messages.py --limit 50          # Show last 50 messages
    python execution/scripts/view_messages.py --all              # Show all messages
    python execution/scripts/view_messages.py --from +1234567890  # Filter by sender
    python execution/scripts/view_messages.py --to +1234567890   # Filter by recipient
    python execution/scripts/view_messages.py --search "text"    # Search message body
    python execution/scripts/view_messages.py --days 7           # Last 7 days
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.config import get_data_dir

# Data file
MESSAGES_FILE = get_data_dir() / "messages" / "messages.parquet"


def format_timestamp(ts) -> str:
    """Format timestamp to human-readable string."""
    if pd.isna(ts):
        return "N/A"

    try:
        if isinstance(ts, int | float):
            # Unix timestamp - check if milliseconds or seconds
            if ts > 1e12:  # Likely milliseconds (timestamp > year 2001 in seconds)
                ts = ts / 1000.0
            dt = datetime.fromtimestamp(ts)
        elif isinstance(ts, pd.Timestamp):
            dt = ts.to_pydatetime()
        elif isinstance(ts, datetime):
            dt = ts
        else:
            # Try to convert string or other types
            dt = pd.to_datetime(ts).to_pydatetime()

        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return f"Invalid: {ts}"


def format_message(row: pd.Series) -> str:
    """Format a single message row for display."""
    direction_icon = "📥" if row["direction"] == "inbound" else "📤"
    status_icon = {
        "received": "✓",
        "delivered": "✓",
        "sent": "→",
        "queued": "⏳",
        "failed": "✗",
        "undelivered": "⚠",
    }.get(row["status"], "?")

    from_num = row["from_number"] if pd.notna(row["from_number"]) else "N/A"
    to_num = row["to_number"] if pd.notna(row["to_number"]) else "N/A"
    body = row["body"] if pd.notna(row["body"]) else "(no body)"

    date_sent = format_timestamp(row.get("date_sent"))

    # Truncate long messages
    if len(body) > 100:
        body = body[:100] + "..."

    return (
        f"{direction_icon} {status_icon} [{date_sent}] "
        f"From: {from_num} → To: {to_num}\n"
        f"   {body}"
    )


def view_messages(
    limit: int | None = None,
    all_messages: bool = False,
    from_number: str | None = None,
    to_number: str | None = None,
    search: str | None = None,
    days: int | None = None,
    reverse: bool = False,
):
    """View messages with filters."""
    if not MESSAGES_FILE.exists():
        print(f"❌ Messages file not found: {MESSAGES_FILE}")
        print("   No messages have been imported yet.")
        return

    # Read messages
    df = pd.read_parquet(MESSAGES_FILE)

    if df.empty:
        print("No messages found.")
        return

    print(f"Total messages in database: {len(df)}")
    print()

    # Apply filters
    if from_number:
        df = df[df["from_number"] == from_number]
        print(f"Filtered by sender: {from_number}")

    if to_number:
        df = df[df["to_number"] == to_number]
        print(f"Filtered by recipient: {to_number}")

    if search:
        df = df[df["body"].str.contains(search, case=False, na=False)]
        print(f"Filtered by search: '{search}'")

    if days:
        cutoff = datetime.now() - timedelta(days=days)
        # Convert date_sent to datetime for comparison
        # Handle both timestamp (int/float) and datetime types
        if df["date_sent"].dtype in ["int64", "float64"]:
            # Timestamps in milliseconds
            df["date_sent_dt"] = pd.to_datetime(
                df["date_sent"], unit="ms", errors="coerce"
            )
        else:
            df["date_sent_dt"] = pd.to_datetime(df["date_sent"], errors="coerce")
        df = df[df["date_sent_dt"] >= cutoff]
        print(f"Filtered to last {days} days")

    if df.empty:
        print("No messages match the filters.")
        return

    # Sort by date_sent (most recent first by default)
    if "date_sent" in df.columns:
        df = df.sort_values("date_sent", ascending=reverse)

    # Apply limit
    if limit and not all_messages:
        df = df.head(limit)

    print(f"Showing {len(df)} message(s):\n")
    print("=" * 80)

    # Display messages
    for idx, row in df.iterrows():
        print(format_message(row))
        print("-" * 80)

    # Summary stats
    if len(df) > 1:
        print("\nSummary:")
        print(f"  Inbound: {len(df[df['direction'] == 'inbound'])}")
        print(f"  Outbound: {len(df[df['direction'] == 'outbound'])}")
        print("  Status breakdown:")
        status_counts = df["status"].value_counts()
        for status, count in status_counts.items():
            print(f"    {status}: {count}")


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="View SMS messages with human-readable dates"
    )
    parser.add_argument(
        "--limit", type=int, default=20, help="Number of messages to show (default: 20)"
    )
    parser.add_argument("--all", action="store_true", help="Show all messages")
    parser.add_argument(
        "--from", dest="from_number", type=str, help="Filter by sender phone number"
    )
    parser.add_argument(
        "--to", dest="to_number", type=str, help="Filter by recipient phone number"
    )
    parser.add_argument("--search", type=str, help="Search message body text")
    parser.add_argument("--days", type=int, help="Show messages from last N days")
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Show oldest first (default: newest first)",
    )
    args = parser.parse_args()

    view_messages(
        limit=args.limit if not args.all else None,
        all_messages=args.all,
        from_number=args.from_number,
        to_number=args.to_number,
        search=args.search,
        days=args.days,
        reverse=args.reverse,
    )


if __name__ == "__main__":
    main()
