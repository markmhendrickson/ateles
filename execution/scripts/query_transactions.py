#!/usr/bin/env python3
"""
Transaction Query Script

Query and analyze normalized transaction data.

Usage:
    python query_transactions.py [--date-start YYYY-MM-DD] [--date-end YYYY-MM-DD] [--category CATEGORY] [--summary]

Examples:
    python query_transactions.py --summary
    python query_transactions.py --date-start 2025-12-01 --date-end 2025-12-31
    python query_transactions.py --category "Restaurante" --summary
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.config import get_data_dir

TRANSACTIONS_FILE = get_data_dir() / "transactions" / "transactions.parquet"


def load_transactions() -> pd.DataFrame:
    """Load transactions from Parquet file."""
    if not TRANSACTIONS_FILE.exists():
        print(f"Error: Transaction file not found: {TRANSACTIONS_FILE}")
        print("Run import_transactions.py first to import transactions.")
        sys.exit(1)

    df = pd.read_parquet(TRANSACTIONS_FILE)
    df["transaction_date"] = pd.to_datetime(df["transaction_date"])
    df["posting_date"] = pd.to_datetime(df["posting_date"])
    return df


def filter_transactions(
    df: pd.DataFrame,
    date_start: str = None,
    date_end: str = None,
    category: str = None,
    account_id: str = None,
) -> pd.DataFrame:
    """Filter transactions by criteria."""
    filtered = df.copy()

    if date_start:
        date_start = pd.to_datetime(date_start)
        filtered = filtered[filtered["transaction_date"] >= date_start]

    if date_end:
        date_end = pd.to_datetime(date_end)
        filtered = filtered[filtered["transaction_date"] <= date_end]

    if category:
        filtered = filtered[
            filtered["category"].str.contains(category, case=False, na=False)
        ]

    if account_id:
        filtered = filtered[filtered["account_id"] == account_id]

    return filtered


def print_summary(df: pd.DataFrame):
    """Print transaction summary statistics."""
    print("\n" + "=" * 60)
    print("TRANSACTION SUMMARY")
    print("=" * 60)

    print(f"\nTotal Transactions: {len(df):,}")
    print(
        f"Date Range: {df['transaction_date'].min().date()} to {df['transaction_date'].max().date()}"
    )

    print(f"\nTotal Amount (USD): ${df['amount_usd'].sum():,.2f}")
    print(f"Total Debits: ${df[df['amount_usd'] < 0]['amount_usd'].sum():,.2f}")
    print(f"Total Credits: ${df[df['amount_usd'] > 0]['amount_usd'].sum():,.2f}")

    print("\nBy Category:")
    category_summary = (
        df.groupby("category")["amount_usd"].agg(["sum", "count"]).sort_values("sum")
    )
    for category, row in category_summary.iterrows():
        print(
            f"  {category:30s} ${row['sum']:>12,.2f} ({int(row['count']):>4} transactions)"
        )

    print("\nBy Bank Provider:")
    provider_summary = df.groupby("bank_provider")["amount_usd"].agg(["sum", "count"])
    for provider, row in provider_summary.iterrows():
        print(
            f"  {provider:30s} ${row['sum']:>12,.2f} ({int(row['count']):>4} transactions)"
        )

    print("\nBy Month:")
    df["year_month"] = df["transaction_date"].dt.to_period("M")
    monthly_summary = df.groupby("year_month")["amount_usd"].sum().sort_index()
    for month, amount in monthly_summary.items():
        print(f"  {month} ${amount:>12,.2f}")

    print("\n" + "=" * 60)


def print_transactions(df: pd.DataFrame, limit: int = 50):
    """Print transaction list."""
    print(f"\nShowing {min(len(df), limit)} of {len(df)} transactions:\n")

    display_df = df[
        [
            "transaction_date",
            "description",
            "category",
            "amount_usd",
            "amount_original",
            "currency_original",
        ]
    ].head(limit)
    display_df["transaction_date"] = display_df["transaction_date"].dt.date
    display_df["amount_usd"] = display_df["amount_usd"].apply(lambda x: f"${x:,.2f}")
    display_df["amount_original"] = display_df.apply(
        lambda x: f"{x['amount_original']:,.2f} {x['currency_original']}", axis=1
    )
    display_df = display_df.drop(columns=["currency_original"])

    print(display_df.to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description="Query transaction data")
    parser.add_argument("--date-start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--date-end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument(
        "--category", type=str, help="Filter by category (substring match)"
    )
    parser.add_argument("--account-id", type=str, help="Filter by account ID")
    parser.add_argument(
        "--summary", action="store_true", help="Show summary statistics"
    )
    parser.add_argument(
        "--limit", type=int, default=50, help="Limit number of transactions shown"
    )

    args = parser.parse_args()

    # Load transactions
    df = load_transactions()

    # Filter transactions
    filtered_df = filter_transactions(
        df,
        date_start=args.date_start,
        date_end=args.date_end,
        category=args.category,
        account_id=args.account_id,
    )

    if len(filtered_df) == 0:
        print("No transactions found matching criteria.")
        sys.exit(0)

    # Print results
    if args.summary:
        print_summary(filtered_df)
    else:
        print_transactions(filtered_df, limit=args.limit)
        print("\n(Use --summary for detailed statistics)")


if __name__ == "__main__":
    main()
