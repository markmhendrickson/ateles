#!/usr/bin/env python3
"""
Purchase Query Script

Query and manage purchase tracking data.

Usage:
    python query_purchases.py [--status STATUS] [--location LOCATION] [--category CATEGORY] [--priority PRIORITY] [--summary]

Examples:
    python query_purchases.py --summary
    python query_purchases.py --status pending
    python query_purchases.py --location Castellón --status pending
    python query_purchases.py --category "Outdoor/BBQ"
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.config import get_data_dir

PURCHASES_FILE = get_data_dir() / "purchases" / "purchases.parquet"


def load_purchases() -> pd.DataFrame:
    """Load purchases from Parquet file."""
    if not PURCHASES_FILE.exists():
        print(f"Error: Purchases file not found: {PURCHASES_FILE}")
        print("Run init_purchases.py first to initialize purchases.")
        sys.exit(1)

    df = pd.read_parquet(PURCHASES_FILE)
    if "created_date" in df.columns:
        df["created_date"] = pd.to_datetime(df["created_date"])
    if "completed_date" in df.columns:
        df["completed_date"] = pd.to_datetime(df["completed_date"])
    return df


def filter_purchases(
    df: pd.DataFrame,
    status: str = None,
    location: str = None,
    category: str = None,
    priority: str = None,
) -> pd.DataFrame:
    """Filter purchases by criteria."""
    filtered = df.copy()

    if status:
        filtered = filtered[filtered["status"].str.lower() == status.lower()]

    if location:
        filtered = filtered[
            filtered["location"].str.contains(location, case=False, na=False)
        ]

    if category:
        filtered = filtered[
            filtered["category"].str.contains(category, case=False, na=False)
        ]

    if priority:
        filtered = filtered[filtered["priority"].str.lower() == priority.lower()]

    return filtered


def print_summary(df: pd.DataFrame):
    """Print purchase summary statistics."""
    print("\n" + "=" * 60)
    print("PURCHASE SUMMARY")
    print("=" * 60)

    print(f"\nTotal Purchases: {len(df):,}")

    print("\nBy Status:")
    status_summary = df.groupby("status").size().sort_values(ascending=False)
    for status, count in status_summary.items():
        print(f"  {status:20s} {count:>4} items")

    print("\nBy Location:")
    location_summary = df.groupby("location").size().sort_values(ascending=False)
    for location, count in location_summary.items():
        print(f"  {location:20s} {count:>4} items")

    print("\nBy Category:")
    category_summary = df.groupby("category").size().sort_values(ascending=False)
    for category, count in category_summary.items():
        print(f"  {category:30s} {count:>4} items")

    print("\nBy Priority:")
    priority_summary = df.groupby("priority").size().sort_values(ascending=False)
    for priority, count in priority_summary.items():
        print(f"  {priority:20s} {count:>4} items")

    # Cost summary (if available)
    if "estimated_cost_usd" in df.columns and df["estimated_cost_usd"].notna().any():
        total_estimated = df["estimated_cost_usd"].sum()
        print(f"\nTotal Estimated Cost (USD): ${total_estimated:,.2f}")

    if "actual_cost_usd" in df.columns and df["actual_cost_usd"].notna().any():
        total_actual = df["actual_cost_usd"].sum()
        print(f"Total Actual Cost (USD): ${total_actual:,.2f}")

    print("\n" + "=" * 60)


def print_purchases(df: pd.DataFrame, limit: int = 50):
    """Print purchase list."""
    print(f"\nShowing {min(len(df), limit)} of {len(df)} purchases:\n")

    # Select columns to display
    display_cols = ["item_name", "status", "location", "priority", "category"]
    if "estimated_cost_usd" in df.columns:
        display_cols.append("estimated_cost_usd")
    if "actual_cost_usd" in df.columns:
        display_cols.append("actual_cost_usd")
    if "created_date" in df.columns:
        display_cols.append("created_date")

    display_df = df[display_cols].head(limit).copy()

    # Format dates
    if "created_date" in display_df.columns:
        display_df["created_date"] = display_df["created_date"].dt.date

    # Format costs
    if "estimated_cost_usd" in display_df.columns:
        display_df["estimated_cost_usd"] = display_df["estimated_cost_usd"].apply(
            lambda x: f"${x:,.2f}" if pd.notna(x) else "N/A"
        )
    if "actual_cost_usd" in display_df.columns:
        display_df["actual_cost_usd"] = display_df["actual_cost_usd"].apply(
            lambda x: f"${x:,.2f}" if pd.notna(x) else "N/A"
        )

    print(display_df.to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description="Query purchase data")
    parser.add_argument(
        "--status",
        type=str,
        help="Filter by status (pending, completed, cancelled, in_progress)",
    )
    parser.add_argument(
        "--location", type=str, help="Filter by location (substring match)"
    )
    parser.add_argument(
        "--category", type=str, help="Filter by category (substring match)"
    )
    parser.add_argument(
        "--priority", type=str, help="Filter by priority (low, medium, high, urgent)"
    )
    parser.add_argument(
        "--summary", action="store_true", help="Show summary statistics"
    )
    parser.add_argument(
        "--limit", type=int, default=50, help="Limit number of purchases shown"
    )

    args = parser.parse_args()

    # Load purchases
    df = load_purchases()

    # Filter purchases
    filtered_df = filter_purchases(
        df,
        status=args.status,
        location=args.location,
        category=args.category,
        priority=args.priority,
    )

    if len(filtered_df) == 0:
        print("No purchases found matching criteria.")
        sys.exit(0)

    # Print results
    if args.summary:
        print_summary(filtered_df)
    else:
        print_purchases(filtered_df, limit=args.limit)
        print("\n(Use --summary for detailed statistics)")


if __name__ == "__main__":
    main()
