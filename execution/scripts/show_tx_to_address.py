#!/usr/bin/env python3
"""Show transactions to a specific address."""

import sys
from datetime import datetime

import requests

if len(sys.argv) < 2:
    print("Usage: show_tx_to_address.py <bitcoin_address>")
    sys.exit(1)

target_addr = sys.argv[1]
sender_addr = "bc1qdagxsxz3ccsje9k82dykg5ewtj83hdef7xsjy3"
url = f"https://mempool.space/api/address/{sender_addr}/txs"

try:
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
except Exception as e:
    print(f"Error fetching transactions: {e}")
    sys.exit(1)

transactions = []

for tx in data:
    for vout in tx.get("vout", []):
        addr = vout.get("scriptpubkey_address", "")
        if addr == target_addr:
            block_time = tx.get("status", {}).get("block_time", 0)
            date_str = (
                datetime.fromtimestamp(block_time).strftime("%Y-%m-%d %H:%M:%S")
                if block_time
                else "Pending"
            )
            transactions.append(
                {
                    "txid": tx["txid"],
                    "amount_btc": vout.get("value", 0) / 100000000,
                    "amount_sats": vout.get("value", 0),
                    "date": date_str,
                    "block_time": block_time,
                    "fee": tx.get("fee", 0),
                    "confirmed": tx.get("status", {}).get("confirmed", False),
                }
            )

# Sort by date (most recent first)
transactions.sort(key=lambda x: x["block_time"], reverse=True)

print(f"\nTransactions to {target_addr}\n")
print(
    f"{'Date':<25} {'Amount (BTC)':<15} {'Amount (EUR @€77,900)':<20} {'TXID':<20} {'Fee (sats)':<12}"
)
print("-" * 100)

total_btc = 0
for tx in transactions:
    eur_approx = tx["amount_btc"] * 77900
    total_btc += tx["amount_btc"]
    print(
        f"{tx['date']:<25} {tx['amount_btc']:>14.8f} {eur_approx:>19.2f} {tx['txid'][:18]}... {tx['fee']:>11,}"
    )

print("-" * 100)
print(f"{'TOTAL':<25} {total_btc:>14.8f} {total_btc * 77900:>19.2f}")
print(f"\nTotal transactions: {len(transactions)}")
