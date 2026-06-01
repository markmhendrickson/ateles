"""
Import orders from JSON file and store in parquet via MCP.

This script reads orders from a JSON file (e.g., manually exported from Coinbase)
and stores them in the orders parquet file via MCP.

Usage:
    python execution/scripts/stx_import_orders_from_json.py [json_file_path]
"""

import json
import sys
from pathlib import Path


def load_orders_from_json(file_path: str) -> list[dict]:
    """Load orders from JSON file."""
    with open(file_path) as f:
        data = json.load(f)

    # Handle both list and dict with "orders" key
    if isinstance(data, list):
        return data
    elif isinstance(data, dict) and "orders" in data:
        return data["orders"]
    else:
        raise ValueError(
            "JSON file must contain a list of orders or a dict with 'orders' key"
        )


def main() -> int:
    if len(sys.argv) > 1:
        json_file = Path(sys.argv[1])
    else:
        # Default to the output from fetch script
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.config import get_data_dir

        json_file = get_data_dir() / "imports" / "coinbase_orders_historical.json"

    if not json_file.exists():
        print(f"ERROR: JSON file not found: {json_file}")
        print("\nUsage:")
        print(f"  python {sys.argv[0]} [json_file_path]")
        print("\nOr export orders from Coinbase UI and save as JSON.")
        return 1

    print(f"Loading orders from: {json_file}")
    orders = load_orders_from_json(str(json_file))
    print(f"Loaded {len(orders)} orders")

    print("\nOrders ready for MCP import:")
    for i, order in enumerate(orders[:10], 1):
        print(
            f"{i}. {order.get('order_id', 'N/A')}: {order.get('amount', 0)} STX @ {order.get('price', 0)} USDC"
        )

    if len(orders) > 10:
        print(f"... and {len(orders) - 10} more")

    print("\n" + "=" * 72)
    print("To import these orders, use MCP parquet add_record for each order.")
    print("The agent will handle the MCP import.")
    print("=" * 72)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
