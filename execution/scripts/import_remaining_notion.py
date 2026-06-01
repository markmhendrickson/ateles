#!/usr/bin/env python3
"""Quick script to import remaining Notion databases"""

import csv
import re
import uuid
from datetime import date
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).parent.parent
import sys

sys.path.insert(0, str(REPO_ROOT))
from scripts.config import get_data_dir

BASE = REPO_ROOT
DATA_DIR = get_data_dir()
NOTION = DATA_DIR / "imports/notion/Main/82793e7277a640d899030ce9376f0c20"


def clean_url(text):
    if not text:
        return ""
    return re.sub(r"\s*\(https://www\.notion\.so/[^\)]+\)", "", text).strip()


def parse_float(val):
    if not val or not val.strip():
        return None
    try:
        return float(val.replace("$", "").replace("€", "").replace(",", ""))
    except Exception:
        return None


def gen_id():
    return str(uuid.uuid4())[:16]


# Import addresses
print("Importing addresses...")
rows = []
with open(NOTION / "Finances/Addresses 11525d6ffd174d26a4a21ed7c309a93c.csv") as f:
    for row in csv.DictReader(f):
        rows.append(
            {
                "address_id": gen_id(),
                "name": clean_url(row.get("Name", "")),
                "accounts": row.get("Accounts", ""),
                "address": row.get("Address", ""),
                "blockchain": "",
                "address_type": "",
                "notes": "",
                "import_date": date.today(),
                "import_source_file": "notion:Addresses.csv",
            }
        )
(DATA_DIR / "addresses").mkdir(exist_ok=True)
pd.DataFrame(rows).to_parquet(DATA_DIR / "addresses/addresses.parquet", index=False)
print(f"  Saved {len(rows)} addresses")

# Import asset_types
print("Importing asset_types...")
rows = []
with open(NOTION / "Finances/Asset types 9f72c0ab85484f3b8bb3292c6d1de9dc.csv") as f:
    for row in csv.DictReader(f):
        rows.append(
            {
                "asset_type_id": gen_id(),
                "name": clean_url(row.get("Name", "")),
                "symbol": "",
                "category": "",
                "notes": "",
                "import_date": date.today(),
                "import_source_file": "notion:Asset types.csv",
            }
        )
(DATA_DIR / "asset_types").mkdir(exist_ok=True)
pd.DataFrame(rows).to_parquet(DATA_DIR / "asset_types/asset_types.parquet", index=False)
print(f"  Saved {len(rows)} asset_types")

# Import asset_values
print("Importing asset_values...")
rows = []
with open(NOTION / "Finances/Asset values 4920919787964787986e974edbc48d4a.csv") as f:
    for row in csv.DictReader(f):
        name = row.get("Name", "")
        rows.append(
            {
                "asset_value_id": gen_id(),
                "name": clean_url(name),
                "asset_type": clean_url(name.split("@")[0] if "@" in name else ""),
                "date": None,
                "market_cap_current": parse_float(row.get("Market cap (current)", "")),
                "unit_price": parse_float(row.get("Unit", "")),
                "currency": "USD",
                "notes": "",
                "import_date": date.today(),
                "import_source_file": "notion:Asset values.csv",
            }
        )
(DATA_DIR / "asset_values").mkdir(exist_ok=True)
pd.DataFrame(rows).to_parquet(
    DATA_DIR / "asset_values/asset_values.parquet", index=False
)
print(f"  Saved {len(rows)} asset_values")

# Import contracts
print("Importing contracts...")
rows = []
with open(NOTION / "Finances/Contracts 6be24f9a139641b3ae4267369a45443d.csv") as f:
    for row in csv.DictReader(f):
        rows.append(
            {
                "contract_id": gen_id(),
                "name": clean_url(row.get("Name", "")),
                "signed_date": None,
                "companies": row.get("Companies", ""),
                "files": row.get("Files", ""),
                "type": "",
                "status": "signed",
                "notes": "",
                "import_date": date.today(),
                "import_source_file": "notion:Contracts.csv",
            }
        )
(DATA_DIR / "contracts").mkdir(exist_ok=True)
pd.DataFrame(rows).to_parquet(DATA_DIR / "contracts/contracts.parquet", index=False)
print(f"  Saved {len(rows)} contracts")

# Import equity_units
print("Importing equity_units...")
rows = []
with open(NOTION / "Finances/Equity units 2503bb6409f648a3a7c05bb669345181.csv") as f:
    for row in csv.DictReader(f):
        rows.append(
            {
                "equity_unit_id": gen_id(),
                "name": clean_url(row.get("Name", "")),
                "units": parse_float(row.get("Units", "")),
                "categories": row.get("Categories", ""),
                "investments": clean_url(row.get("Investments", "")),
                "exercised": "Exercised" in row.get("Categories", ""),
                "issued_date": None,
                "exercised_date": None,
                "url": row.get("URL", ""),
                "files": row.get("Files & media", ""),
                "notes": "",
                "import_date": date.today(),
                "import_source_file": "notion:Equity units.csv",
            }
        )
(DATA_DIR / "equity_units").mkdir(exist_ok=True)
pd.DataFrame(rows).to_parquet(
    DATA_DIR / "equity_units/equity_units.parquet", index=False
)
print(f"  Saved {len(rows)} equity_units")

# Import financial_strategies
print("Importing financial_strategies...")
rows = []
with open(
    NOTION / "Finances/Financial strategies fc772bb19ab14c6db7e2e5d982d66710.csv"
) as f:
    for row in csv.DictReader(f):
        rows.append(
            {
                "strategy_id": gen_id(),
                "name": clean_url(row.get("Name", "")),
                "date": None,
                "description": "",
                "status": "active",
                "notes": "",
                "import_date": date.today(),
                "import_source_file": "notion:Financial strategies.csv",
            }
        )
(DATA_DIR / "financial_strategies").mkdir(exist_ok=True)
pd.DataFrame(rows).to_parquet(
    DATA_DIR / "financial_strategies/financial_strategies.parquet", index=False
)
print(f"  Saved {len(rows)} financial_strategies")

# Import orders
print("Importing orders...")
rows = []
with open(NOTION / "Finances/Orders af9609fa0b794fa18b8d38e52a6178fb.csv") as f:
    for row in csv.DictReader(f):
        rows.append(
            {
                "order_id": gen_id(),
                "name": clean_url(row.get("Name", "")),
                "status": row.get("Status", ""),
                "accounts": row.get("Accounts", ""),
                "amount": None,
                "asset_type": "",
                "order_type": "",
                "price": None,
                "url": row.get("URL", ""),
                "date": None,
                "notes": "",
                "import_date": date.today(),
                "import_source_file": "notion:Orders.csv",
            }
        )
(DATA_DIR / "orders").mkdir(exist_ok=True)
pd.DataFrame(rows).to_parquet(DATA_DIR / "orders/orders.parquet", index=False)
print(f"  Saved {len(rows)} orders")

# Import tax_filings
print("Importing tax_filings...")
rows = []
with open(
    NOTION / "Finances/Taxes/Tax filings 2234eb63ab5b42d6bf468b51bf749efe.csv"
) as f:
    for row in csv.DictReader(f):
        rows.append(
            {
                "filing_id": gen_id(),
                "name": clean_url(row.get("Name", "")),
                "jurisdiction": row.get("Jurisdiction", ""),
                "year": None,
                "filings": row.get("Filings", ""),
                "status": row.get("Status", ""),
                "companies": row.get("Companies", ""),
                "due_date": None,
                "filed_date": None,
                "amount_owed": None,
                "amount_paid": None,
                "currency": "",
                "notes": "",
                "import_date": date.today(),
                "import_source_file": "notion:Tax filings.csv",
            }
        )
(DATA_DIR / "tax_filings").mkdir(exist_ok=True)
pd.DataFrame(rows).to_parquet(DATA_DIR / "tax_filings/tax_filings.parquet", index=False)
print(f"  Saved {len(rows)} tax_filings")

# Import transfers
print("Importing transfers...")
rows = []
with open(NOTION / "Finances/Transfers 809787ff8daa45d7a1a46c376b2d38a5.csv") as f:
    for row in csv.DictReader(f):
        rows.append(
            {
                "transfer_id": gen_id(),
                "name": clean_url(row.get("Name", "")),
                "status": row.get("Status", ""),
                "amount": parse_float(row.get("Amount", "")),
                "origin_account": clean_url(row.get("Origin account", "")),
                "destination_account": clean_url(row.get("Destination account", "")),
                "created_time": None,
                "deposit_address": row.get("Deposit address", ""),
                "fees": parse_float(row.get("Fees", "")),
                "transaction": row.get("Transaction", ""),
                "transactions": row.get("Transactions", ""),
                "notes": "",
                "import_date": date.today(),
                "import_source_file": "notion:Transfers.csv",
            }
        )
(DATA_DIR / "transfers").mkdir(exist_ok=True)
pd.DataFrame(rows).to_parquet(DATA_DIR / "transfers/transfers.parquet", index=False)
print(f"  Saved {len(rows)} transfers")

# Import arguments
print("Importing arguments...")
rows = []
with open(NOTION / "Restricted/Arguments cd3a4f1f00e04881ab206eba2653ad00.csv") as f:
    for row in csv.DictReader(f):
        rows.append(
            {
                "argument_id": gen_id(),
                "name": clean_url(row.get("Name", "")),
                "date": None,
                "category": "",
                "resolution": "",
                "notes": "",
                "import_date": date.today(),
                "import_source_file": "notion:Arguments.csv",
            }
        )
(DATA_DIR / "arguments").mkdir(exist_ok=True)
pd.DataFrame(rows).to_parquet(DATA_DIR / "arguments/arguments.parquet", index=False)
print(f"  Saved {len(rows)} arguments")

print("\nAll remaining databases imported successfully!")
