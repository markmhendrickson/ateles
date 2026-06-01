#!/usr/bin/env python3
"""Show last 20 outbound transactions from a Bitcoin address."""

import sys
from datetime import datetime

import requests

if len(sys.argv) < 2:
    print("Usage: show_outbound_txs.py <bitcoin_address>")
    sys.exit(1)

address = sys.argv[1]
url = f"https://mempool.space/api/address/{address}/txs"

try:
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
except Exception as e:
    print(f"Error fetching transactions: {e}")
    sys.exit(1)

outbound = []

for tx in data:
    # Get all outputs that are NOT change (not back to sender)
    for vout in tx.get("vout", []):
        addr = vout.get("scriptpubkey_address", "")
        if addr and addr != address:
            outbound.append(
                {
                    "txid": tx["txid"],
                    "to_address": addr,
                    "amount_btc": vout.get("value", 0) / 100000000,
                    "amount_sats": vout.get("value", 0),
                    "block_time": tx.get("status", {}).get("block_time", 0),
                    "confirmed": tx.get("status", {}).get("confirmed", False),
                    "fee": tx.get("fee", 0),
                }
            )

# Sort by block time (most recent first)
outbound.sort(key=lambda x: x["block_time"], reverse=True)

print(f"\nLast 20 Outbound Transactions from {address}\n")
print(
    f"{'TXID':<20} {'To Address':<45} {'Amount (BTC)':<15} {'Date':<25} {'Fee (sats)':<12} {'Status':<10}"
)
print("-" * 130)

for tx in outbound[:20]:
    date_str = (
        datetime.fromtimestamp(tx["block_time"]).strftime("%Y-%m-%d %H:%M:%S")
        if tx["block_time"]
        else "Pending"
    )
    status = "Confirmed" if tx["confirmed"] else "Pending"
    to_addr_short = (
        tx["to_address"][:43] + "..."
        if len(tx["to_address"]) > 45
        else tx["to_address"]
    )
    print(
        f"{tx['txid'][:18]}... {to_addr_short:<45} {tx['amount_btc']:>14.8f} {date_str:<25} {tx['fee']:>11,} {status:<10}"
    )

print(f"\nTotal outbound transactions found: {len(outbound)}")
