"""
Fetch STX on-chain balances for known Stacks addresses (e.g. Ledger wallets)
using the public Stacks node API.

This script is intentionally read-only. It:
- Queries the Stacks API for each configured address
- Prints total, locked, and unlocked balances per address
- Prints the aggregate on-chain STX balance

You should:
- Run this locally (not from the repo sandbox)
- Ensure `requests` is installed: `pip install requests`
- Adjust `STACKS_ADDRESSES` below to match your actual wallets
"""

import requests

STACKS_NODE_API_BASE = "https://stacks-node-api.mainnet.stacks.co/extended/v1"

# Known addresses from Notion imports (Ledger Nano S Stacking wallet)
# Adjust / extend as needed. Labels are purely for display.
STACKS_ADDRESSES: dict[str, str] = {
    "SP2M1X1X26X2D3KAQCT3RJFGXR3NX1ZK5XD5VMZ69": "Ledger S Stack 1",
    "SPFTM1H08PV6PYQF2S4CBPEHJFJ2Z498XVE5TX6C": "Ledger S Stack 2",
    "SP2A77ZF39Z512ECJXS1CSKFC0DC882YQ8Y16XM0G": "Ledger S Stack 3",
    "SP1QNHF1HY91WQ5AY7DTAP6K9GM6R2YXMX5C88FKM": "Ledger S Stack 4",
}


def fetch_stx_balance(address: str) -> tuple[float, float, float]:
    """
    Return (total_stx, locked_stx, unlocked_stx) for a given Stacks address.
    All values are in STX (not micro-STX).
    """
    url = f"{STACKS_NODE_API_BASE}/address/{address}/stx"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    balance_micro = int(data.get("balance", 0))
    locked_micro = int(data.get("locked", 0))

    total = balance_micro / 1_000_000
    locked = locked_micro / 1_000_000
    unlocked = total - locked

    return total, locked, unlocked


def main() -> int:
    print("Querying Stacks node API for STX balances\n")
    total_all = 0.0

    for addr, label in STACKS_ADDRESSES.items():
        try:
            total, locked, unlocked = fetch_stx_balance(addr)
        except Exception as e:  # noqa: BLE001
            print(f"{label} ({addr}): ERROR -> {e}")
            continue

        total_all += total

        print(f"{label}")
        print(f"  Address : {addr}")
        print(f"  Total   : {total:,.6f} STX")
        print(f"  Locked  : {locked:,.6f} STX")
        print(f"  Unlocked: {unlocked:,.6f} STX")
        print()

    print("=" * 72)
    print(f"TOTAL on-chain STX: {total_all:,.6f} STX")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
