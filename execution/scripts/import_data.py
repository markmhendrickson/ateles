#!/usr/bin/env python3
"""
Generic Data Import Script

Imports CSV files for various financial data types, normalizes them to standard schemas,
and stores in Parquet format with deduplication.

Usage:
    python import_data.py <data_type> <csv_file> [--source <source_name>] [--currency <currency_code>] [--options <key=value,...>]

Examples:
    python import_data.py transactions ~/Desktop/transactions.csv --source capital_one --currency EUR
    python import_data.py holdings ~/Desktop/portfolio.csv --source broker_name
    python import_data.py balances ~/Desktop/balances.csv --source bank_name --currency EUR
    python import_data.py income ~/Desktop/income.csv --source consulting
    python import_data.py crypto_transactions ~/Desktop/crypto.csv --source exchange_name
    python import_data.py tax_events ~/Desktop/tax.csv --source tax_software
"""

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from dateutil import parser as date_parser

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.config import DATA_DIR
from scripts.frankfurter_fx import CurrencyConverter

# Configuration
IMPORTS_DIR = DATA_DIR / "imports"
LOGS_DIR = DATA_DIR / "logs"

# Ensure directories exist
IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


class BaseDataImporter:
    """Base class for data type-specific importers."""

    def __init__(self, data_type: str, converter: CurrencyConverter):
        self.data_type = data_type
        self.converter = converter
        self.data_dir = DATA_DIR / data_type
        self.data_file = self.data_dir / f"{data_type}.parquet"
        self.schema_file = DATA_DIR / "schemas" / f"{data_type}_schema.json"

        # Ensure data directory exists
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def generate_id(self, record: dict, source_file: str) -> str:
        """Generate unique ID from record data."""
        # Create hash from key fields (override in subclasses)
        hash_string = json.dumps(record, sort_keys=True, default=str) + source_file
        return hashlib.sha256(hash_string.encode()).hexdigest()[:16]

    def parse_csv(self, csv_path: Path, **kwargs) -> list[dict]:
        """Parse CSV file and return list of records. Override in subclasses."""
        raise NotImplementedError("Subclasses must implement parse_csv")

    def normalize_record(
        self, record: dict, source: str, source_file: str, **kwargs
    ) -> dict:
        """Normalize record to standard schema. Override in subclasses."""
        raise NotImplementedError("Subclasses must implement normalize_record")

    def load_existing_data(self) -> pd.DataFrame:
        """Load existing data from Parquet file."""
        if self.data_file.exists():
            try:
                return pd.read_parquet(self.data_file)
            except Exception as e:
                print(f"Warning: Could not load existing {self.data_type}: {e}")
                return pd.DataFrame()
        return pd.DataFrame()

    def import_csv(self, csv_path: Path, source: str = "unknown", **kwargs) -> dict:
        """Import CSV file and return import statistics."""
        csv_path = Path(csv_path)

        if not csv_path.exists():
            raise FileNotFoundError(f"CSV file not found: {csv_path}")

        # Parse CSV
        print(f"Parsing CSV file: {csv_path}")
        raw_records = self.parse_csv(csv_path, **kwargs)
        print(f"Found {len(raw_records)} records")

        # Normalize records
        source_filename = csv_path.name
        normalized_records = [
            self.normalize_record(r, source, source_filename, **kwargs)
            for r in raw_records
        ]

        # Load existing data
        existing_df = self.load_existing_data()

        # Create DataFrame from new records
        new_df = pd.DataFrame(normalized_records)

        if new_df.empty:
            print("No records to import after normalization")
            return {"imported": 0, "duplicates": 0, "total": len(existing_df)}

        # Deduplicate: remove records that already exist
        id_column = (
            f"{self.data_type.split('_')[0]}_id"  # e.g., transaction_id, holding_id
        )
        if not existing_df.empty and id_column in existing_df.columns:
            existing_ids = set(existing_df[id_column].values)
            new_df = new_df[~new_df[id_column].isin(existing_ids)]
            duplicates = len(normalized_records) - len(new_df)
        else:
            duplicates = 0

        # Merge with existing
        if existing_df.empty:
            combined_df = new_df
        else:
            combined_df = pd.concat([existing_df, new_df], ignore_index=True)

        # Save to Parquet
        combined_df.to_parquet(self.data_file, index=False, engine="pyarrow")
        print(
            f"Saved {len(combined_df)} total {self.data_type} records to {self.data_file}"
        )

        # Archive source CSV
        try:
            archive_path = (
                IMPORTS_DIR
                / f"{datetime.now().date()}_{source}_{self.data_type}_{source_filename}"
            )
            import shutil

            shutil.copy2(str(csv_path), str(archive_path))
            print(f"Archived source CSV to {archive_path}")
        except Exception as e:
            print(f"Warning: Could not archive source CSV: {e}")

        # Log import
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "data_type": self.data_type,
            "source_file": str(csv_path),
            "source": source,
            "records_imported": len(new_df),
            "records_duplicate": duplicates,
            "total_records": len(combined_df),
            "options": kwargs,
        }

        log_file = LOGS_DIR / "import_log.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

        return {
            "imported": len(new_df),
            "duplicates": duplicates,
            "total": len(combined_df),
        }


class TransactionImporter(BaseDataImporter):
    """Importer for transaction data (reuses existing transaction import logic)."""

    def __init__(self, converter: CurrencyConverter):
        super().__init__("transactions", converter)
        # Import existing transaction parsers dynamically
        import importlib.util
        import sys

        transactions_module_path = Path(__file__).parent / "import_transactions.py"
        spec = importlib.util.spec_from_file_location(
            "import_transactions", transactions_module_path
        )
        transactions_module = importlib.util.module_from_spec(spec)
        sys.modules["import_transactions"] = transactions_module
        spec.loader.exec_module(transactions_module)

        self.parsers = {
            "capital_one": transactions_module.CapitalOneParser(),
            "ibercaja": transactions_module.IbercajaParser(),
        }

    def parse_csv(
        self, csv_path: Path, bank_provider: str = "capital_one", **kwargs
    ) -> list[dict]:
        """Parse CSV using bank-specific parser or generic finance format."""
        # Check if this is the finance spreadsheet format
        if bank_provider == "finance_spreadsheet":
            return self._parse_finance_transactions(csv_path)

        parser = self.parsers.get(bank_provider)
        if not parser:
            raise ValueError(
                f"Unknown bank provider: {bank_provider}. Available: {list(self.parsers.keys())} + 'finance_spreadsheet'"
            )
        return parser.parse(csv_path)

    def _parse_finance_transactions(self, csv_path: Path) -> list[dict]:
        """Parse finance spreadsheet transaction format."""
        transactions = []

        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        rows = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if rows is None:
            raise ValueError("Could not read CSV file with any encoding")

        # Check if this is expenses format (has € column and Expense column)
        is_expenses_format = "€" in str(rows[0].keys()) if rows else False

        for row in rows:
            if not any(row.values()):
                continue

            # Parse date
            date_str = row.get("Date", "").strip()
            if not date_str:
                continue

            try:
                transaction_date = date_parser.parse(date_str).date()
                posting_date = transaction_date  # Use same date if not specified
            except (ValueError, TypeError):
                continue

            if is_expenses_format:
                # Expenses format: Date, €, Expense
                amount_str = row.get("€", "").strip()
                description = row.get("Expense", "").strip()
                category = (
                    description.split("(")[0].strip()
                    if "(" in description
                    else description
                )

                if not amount_str:
                    continue

                try:
                    # Remove currency symbols and commas
                    amount_str = (
                        amount_str.replace("€", "")
                        .replace(",", "")
                        .replace("$", "")
                        .strip()
                    )
                    amount = float(amount_str)
                    # Expenses are negative
                    amount = -abs(amount)
                except ValueError:
                    continue

                transactions.append(
                    {
                        "transaction_date": transaction_date,
                        "posting_date": posting_date,
                        "amount_original": amount,
                        "description": description,
                        "category": category,
                        "account_id": "expenses",
                    }
                )
            else:
                # Standard finance transaction format
                # Parse amount - prefer Amount (€) if available, otherwise Original Amount
                amount_str = (
                    row.get("Amount (€)", "").strip()
                    or row.get("Original Amount", "").strip()
                )
                if not amount_str:
                    continue

                try:
                    # Remove currency symbols and commas
                    amount_str = (
                        amount_str.replace("€", "")
                        .replace(",", "")
                        .replace("$", "")
                        .strip()
                    )
                    amount = float(amount_str)
                except ValueError:
                    continue

                # Get currency
                row.get("Currency", "EUR").strip() or "EUR"

                transactions.append(
                    {
                        "transaction_date": transaction_date,
                        "posting_date": posting_date,
                        "amount_original": amount,
                        "description": row.get("Description", "").strip()
                        or row.get("Original description", "").strip(),
                        "category": row.get("Category", "").strip() or "?",
                        "account_id": row.get("Bank", "").strip(),
                    }
                )

        return transactions

    def normalize_record(
        self,
        record: dict,
        source: str,
        source_file: str,
        currency: str = "EUR",
        **kwargs,
    ) -> dict:
        """Normalize transaction record."""
        transaction_id = self.generate_id(record, source_file)

        # Convert to USD
        date_str = record["transaction_date"].isoformat()
        amount_original = record["amount_original"]

        amount_usd = self.converter.convert_to_usd(
            abs(amount_original), currency, date_str
        )

        if amount_original < 0:
            amount_usd = -amount_usd

        return {
            "transaction_id": transaction_id,
            "transaction_date": record["transaction_date"],
            "posting_date": record["posting_date"],
            "amount_usd": amount_usd,
            "amount_original": amount_original,
            "currency_original": currency,
            "description": record["description"],
            "category": record.get("category", ""),
            "account_id": record.get("account_id", ""),
            "bank_provider": source,
            "import_date": datetime.now().date(),
            "import_source_file": source_file,
        }


class HoldingsImporter(BaseDataImporter):
    """Importer for portfolio holdings data."""

    def __init__(self, converter: CurrencyConverter):
        super().__init__("holdings", converter)

    def _parse_assets_format(self, csv_path: Path) -> list[dict]:
        """Parse Assets-Table 1.csv format (wallet/account structure)."""
        records = []

        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        rows = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if rows is None:
            raise ValueError("Could not read CSV file with any encoding")

        # Use current date as snapshot date
        snapshot_date = datetime.now().date()

        for row in rows:
            if not any(row.values()):
                continue

            # Get asset details
            asset_symbol = row.get("Asset", "").strip()
            if not asset_symbol:
                continue

            units_str = row.get("Units", "").strip()
            value_str = row.get("$", "").strip() or row.get("€", "").strip()

            try:
                units = float(str(units_str).replace(",", "")) if units_str else 0.0
                # Extract numeric value from currency string
                value_str_clean = (
                    value_str.replace("US$", "")
                    .replace("€", "")
                    .replace(",", "")
                    .replace("$", "")
                    .strip()
                )
                value_usd = float(value_str_clean) if value_str_clean else 0.0
            except (ValueError, TypeError):
                continue

            # Skip if no units or value
            if units == 0.0 and value_usd == 0.0:
                continue

            records.append(
                {
                    "snapshot_date": snapshot_date,
                    "asset_symbol": asset_symbol,
                    "asset_name": row.get("Description", "").strip()
                    or row.get("Description 2", "").strip()
                    or asset_symbol,
                    "asset_type": row.get("Currency type", "").strip()
                    or row.get("Type", "").strip()
                    or "unknown",
                    "quantity": units,
                    "current_value_usd": value_usd,
                    "cost_basis_usd": (
                        float(
                            str(row.get("$ cost basis", "0"))
                            .replace("US$", "")
                            .replace(",", "")
                            .strip()
                        )
                        if row.get("$ cost basis")
                        else 0.0
                    ),
                    "account_id": row.get("Account", "").strip()
                    or row.get("Wallet", "").strip(),
                    "account_type": row.get("Type", "").strip() or "wallet",
                }
            )

        return records

    def parse_csv(
        self, csv_path: Path, format_type: str = "generic", **kwargs
    ) -> list[dict]:
        """Parse holdings CSV (supports multiple formats)."""
        # Check if this is the assets/wallet format
        if format_type == "assets" or "Assets" in str(csv_path):
            return self._parse_assets_format(csv_path)

        records = []

        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        rows = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if rows is None:
            raise ValueError("Could not read CSV file with any encoding")

        for row in rows:
            if not any(row.values()):
                continue

            # Try to parse common column names
            date_str = (
                row.get("date")
                or row.get("snapshot_date")
                or row.get("Date")
                or row.get("Date")
            )
            symbol = (
                row.get("symbol") or row.get("asset_symbol") or row.get("Symbol") or ""
            )
            quantity = (
                row.get("quantity") or row.get("shares") or row.get("Quantity") or "0"
            )
            value = (
                row.get("value") or row.get("current_value") or row.get("Value") or "0"
            )
            cost_basis = (
                row.get("cost_basis") or row.get("cost") or row.get("Cost Basis") or "0"
            )

            try:
                snapshot_date = (
                    date_parser.parse(date_str).date()
                    if date_str
                    else datetime.now().date()
                )
                quantity_val = float(str(quantity).replace(",", ""))
                value_val = float(
                    str(value)
                    .replace(",", "")
                    .replace("$", "")
                    .replace("€", "")
                    .replace("£", "")
                )
                cost_basis_val = (
                    float(
                        str(cost_basis)
                        .replace(",", "")
                        .replace("$", "")
                        .replace("€", "")
                        .replace("£", "")
                    )
                    if cost_basis
                    else 0.0
                )
            except (ValueError, TypeError):
                continue

            records.append(
                {
                    "snapshot_date": snapshot_date,
                    "asset_symbol": symbol,
                    "asset_name": row.get("name") or row.get("asset_name") or symbol,
                    "asset_type": row.get("type") or row.get("asset_type") or "unknown",
                    "quantity": quantity_val,
                    "current_value_usd": value_val,
                    "cost_basis_usd": cost_basis_val,
                    "account_id": row.get("account_id") or row.get("account") or "",
                    "account_type": row.get("account_type") or "",
                }
            )

        return records

    def normalize_record(
        self, record: dict, source: str, source_file: str, **kwargs
    ) -> dict:
        """Normalize holdings record."""
        holding_id = self.generate_id(record, source_file)

        return {
            "holding_id": holding_id,
            "snapshot_date": record["snapshot_date"],
            "asset_type": record.get("asset_type", "unknown"),
            "asset_symbol": record.get("asset_symbol", ""),
            "asset_name": record.get("asset_name", ""),
            "quantity": record.get("quantity", 0.0),
            "cost_basis_usd": record.get("cost_basis_usd", 0.0),
            "current_value_usd": record.get("current_value_usd", 0.0),
            "account_id": record.get("account_id", ""),
            "account_type": record.get("account_type", ""),
            "provider": source,
            "import_date": datetime.now().date(),
            "import_source_file": source_file,
        }


class BalancesImporter(BaseDataImporter):
    """Importer for account balance data."""

    def __init__(self, converter: CurrencyConverter):
        super().__init__("balances", converter)

    def parse_csv(self, csv_path: Path, **kwargs) -> list[dict]:
        """Parse balances CSV."""
        records = []

        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        rows = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if rows is None:
            raise ValueError("Could not read CSV file with any encoding")

        for row in rows:
            if not any(row.values()):
                continue

            date_str = (
                row.get("date") or row.get("snapshot_date") or row.get("Date") or ""
            )
            account_id = (
                row.get("account_id") or row.get("account") or row.get("Account") or ""
            )
            balance_str = row.get("balance") or row.get("Balance") or "0"

            try:
                snapshot_date = (
                    date_parser.parse(date_str).date()
                    if date_str
                    else datetime.now().date()
                )
                balance_val = float(
                    str(balance_str)
                    .replace(",", "")
                    .replace("$", "")
                    .replace("€", "")
                    .replace("£", "")
                )
            except (ValueError, TypeError):
                continue

            records.append(
                {
                    "snapshot_date": snapshot_date,
                    "account_id": account_id,
                    "account_name": row.get("account_name")
                    or row.get("name")
                    or account_id,
                    "account_type": row.get("account_type") or row.get("type") or "",
                    "balance_original": balance_val,
                    "currency_original": row.get("currency")
                    or row.get("Currency")
                    or "USD",
                }
            )

        return records

    def normalize_record(
        self,
        record: dict,
        source: str,
        source_file: str,
        currency: str = "USD",
        **kwargs,
    ) -> dict:
        """Normalize balance record."""
        balance_id = self.generate_id(record, source_file)

        # Convert to USD
        date_str = record["snapshot_date"].isoformat()
        currency_orig = record.get("currency_original", currency)
        balance_usd = self.converter.convert_to_usd(
            record["balance_original"], currency_orig, date_str
        )

        return {
            "balance_id": balance_id,
            "snapshot_date": record["snapshot_date"],
            "account_id": record.get("account_id", ""),
            "account_type": record.get("account_type", ""),
            "account_name": record.get("account_name", ""),
            "balance_usd": balance_usd,
            "balance_original": record["balance_original"],
            "currency_original": currency_orig,
            "provider": source,
            "import_date": datetime.now().date(),
            "import_source_file": source_file,
        }


class IncomeImporter(BaseDataImporter):
    """Importer for income data."""

    def __init__(self, converter: CurrencyConverter):
        super().__init__("income", converter)

    def _parse_earnings_format(self, csv_path: Path) -> list[dict]:
        """Parse Earnings-Table 1.csv format (detailed earnings with tax info)."""
        records = []

        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        rows = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if rows is None:
            raise ValueError("Could not read CSV file with any encoding")

        for row in rows:
            if not any(row.values()):
                continue

            # Get source and type
            source = row.get("Source", "").strip()
            if not source:
                continue

            # Skip if not executed
            executed = row.get("Executed", "").strip().lower()
            if executed not in ["yes", "true", "1"]:
                continue

            # Parse date - prefer Receipt date, then $ Invoice date, then € Invoice date
            date_str = (
                row.get("Receipt date", "").strip()
                or row.get("$ Invoice date", "").strip()
                or row.get("€ Invoice date", "").strip()
            )
            if not date_str:
                continue

            try:
                income_date = date_parser.parse(date_str).date()
            except (ValueError, TypeError):
                continue

            # Get amounts - prefer $ US, then € Spain
            amount_str = row.get("$ US", "").strip() or row.get("€ Spain", "").strip()
            if not amount_str:
                continue

            try:
                # Clean amount string
                amount_str = (
                    amount_str.replace("US$", "")
                    .replace("€", "")
                    .replace(",", "")
                    .replace("$", "")
                    .strip()
                )
                amount_val = float(amount_str)
            except (ValueError, TypeError):
                continue

            # Determine currency
            currency = "USD" if row.get("$ US", "").strip() else "EUR"

            # Get earnings type and asset type
            earnings_type = row.get("Earnings type", "").strip() or "income"
            asset_type = row.get("Asset type", "").strip()

            # Determine income type
            if asset_type:
                income_type = asset_type.lower()
            else:
                income_type = earnings_type.lower()

            # Get year and quarter
            year = row.get("Year", "").strip()
            quarter = row.get("Quarter", "").strip()

            # Determine tax year
            if year:
                try:
                    tax_year = int(year)
                except ValueError:
                    tax_year = income_date.year
            else:
                tax_year = income_date.year

            records.append(
                {
                    "income_date": income_date,
                    "income_type": income_type,
                    "source": source,
                    "amount_original": amount_val,
                    "currency_original": currency,
                    "description": f"{source} - {earnings_type}"
                    + (f" Q{quarter}" if quarter else ""),
                    "entity": "",
                    "tax_year": tax_year,
                }
            )

        return records

    def parse_csv(
        self, csv_path: Path, format_type: str = "generic", **kwargs
    ) -> list[dict]:
        """Parse income CSV - supports multiple formats."""
        # Check if this is the Earnings format
        if format_type == "earnings" or "Earnings" in str(csv_path):
            return self._parse_earnings_format(csv_path)

        records = []

        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        rows = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if rows is None:
            raise ValueError("Could not read CSV file with any encoding")

        for row in rows:
            if not any(row.values()):
                continue

            # Check if this is finance spreadsheet format (has $ Invoice date or € Invoice date)
            if "Invoice date" in str(row.keys()) or "$ US" in str(row.keys()):
                # Finance spreadsheet format
                date_str = (
                    row.get("$ Invoice date", "").strip()
                    or row.get("€ Invoice date", "").strip()
                )
                amount_str = (
                    row.get("$ US", "").strip() or row.get("€ Spain", "").strip()
                )
                source = row.get("Source", "").strip()
                income_type = row.get("Type", "").strip() or "unknown"
                description = (
                    row.get("Notes", "").strip() or row.get("Denomination", "").strip()
                )
                currency = "USD" if row.get("$ US") else "EUR"

                if not date_str or not amount_str:
                    continue

                try:
                    income_date = date_parser.parse(date_str).date()
                    # Clean amount string
                    amount_str = (
                        amount_str.replace("US$", "")
                        .replace("€", "")
                        .replace(",", "")
                        .replace("$", "")
                        .strip()
                    )
                    amount_val = float(amount_str)
                except (ValueError, TypeError):
                    continue

                # Determine tax year from date
                tax_year = income_date.year

                records.append(
                    {
                        "income_date": income_date,
                        "income_type": (
                            income_type.lower() if income_type else "unknown"
                        ),
                        "source": source,
                        "amount_original": amount_val,
                        "currency_original": currency,
                        "description": description,
                        "entity": "",
                        "tax_year": tax_year,
                    }
                )
            else:
                # Standard format
                date_str = (
                    row.get("date") or row.get("income_date") or row.get("Date") or ""
                )
                amount_str = row.get("amount") or row.get("Amount") or "0"
                income_type = (
                    row.get("type")
                    or row.get("income_type")
                    or row.get("Type")
                    or "unknown"
                )

                try:
                    income_date = (
                        date_parser.parse(date_str).date()
                        if date_str
                        else datetime.now().date()
                    )
                    amount_val = float(
                        str(amount_str)
                        .replace(",", "")
                        .replace("$", "")
                        .replace("€", "")
                        .replace("£", "")
                    )
                except (ValueError, TypeError):
                    continue

                records.append(
                    {
                        "income_date": income_date,
                        "income_type": income_type,
                        "source": row.get("source") or row.get("Source") or "",
                        "amount_original": amount_val,
                        "currency_original": row.get("currency")
                        or row.get("Currency")
                        or "USD",
                        "description": row.get("description")
                        or row.get("Description")
                        or "",
                        "entity": row.get("entity") or row.get("Entity") or "",
                        "tax_year": (
                            int(
                                row.get("tax_year")
                                or row.get("Tax Year")
                                or income_date.year
                            )
                            if row.get("tax_year") or row.get("Tax Year")
                            else income_date.year
                        ),
                    }
                )

        return records

    def normalize_record(
        self,
        record: dict,
        source: str,
        source_file: str,
        currency: str = "USD",
        **kwargs,
    ) -> dict:
        """Normalize income record."""
        income_id = self.generate_id(record, source_file)

        # Convert to USD
        date_str = record["income_date"].isoformat()
        currency_orig = record.get("currency_original", currency)
        amount_usd = self.converter.convert_to_usd(
            record["amount_original"], currency_orig, date_str
        )

        return {
            "income_id": income_id,
            "income_date": record["income_date"],
            "income_type": record.get("income_type", "unknown"),
            "source": record.get("source", source),
            "amount_usd": amount_usd,
            "amount_original": record["amount_original"],
            "currency_original": currency_orig,
            "description": record.get("description", ""),
            "entity": record.get("entity", ""),
            "tax_year": record.get("tax_year", record["income_date"].year),
            "import_date": datetime.now().date(),
            "import_source_file": source_file,
        }


class CryptoTransactionsImporter(BaseDataImporter):
    """Importer for crypto transaction data."""

    def __init__(self, converter: CurrencyConverter):
        super().__init__("crypto_transactions", converter)

    def _parse_koinly_format(self, csv_path: Path) -> list[dict]:
        """Parse Koinly export format (Stacks transactions)."""
        records = []

        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        rows = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if rows is None:
            raise ValueError("Could not read CSV file with any encoding")

        for row in rows:
            if not any(row.values()):
                continue

            # Parse date
            date_str = row.get("Date (UTC)", "").strip()
            if not date_str:
                continue

            try:
                transaction_date = date_parser.parse(date_str).date()
            except (ValueError, TypeError):
                continue

            # Get transaction type and amounts
            tx_type = row.get("Type", "").strip() or "unknown"
            from_amount_str = row.get("From Amount", "").strip()
            from_currency = row.get("From Currency", "").strip()
            to_amount_str = row.get("To Amount", "").strip()
            to_currency = row.get("To Currency", "").strip()
            tx_hash = row.get("TxHash", "").strip()

            # Parse amounts
            try:
                from_amount = (
                    float(from_amount_str.replace(",", "")) if from_amount_str else 0.0
                )
                to_amount = (
                    float(to_amount_str.replace(",", "")) if to_amount_str else 0.0
                )
            except ValueError:
                continue

            # Determine primary asset and quantity
            if from_amount > 0:
                asset_symbol = (
                    from_currency.split(";")[0]
                    if ";" in from_currency
                    else from_currency
                )
                quantity = from_amount
            elif to_amount > 0:
                asset_symbol = (
                    to_currency.split(";")[0] if ";" in to_currency else to_currency
                )
                quantity = to_amount
            else:
                continue

            # Get value
            net_value_str = row.get("Net Value (read-only)", "").strip() or "0"
            try:
                value_usd = float(net_value_str.replace(",", ""))
            except ValueError:
                value_usd = 0.0

            # Get fee
            fee_str = row.get("Fee Amount", "").strip() or "0"
            try:
                fee_amount = float(fee_str.replace(",", ""))
            except ValueError:
                fee_amount = 0.0

            records.append(
                {
                    "transaction_date": transaction_date,
                    "transaction_type": tx_type,
                    "blockchain": "stacks",
                    "from_address": row.get("TxSrc", "").strip(),
                    "to_address": row.get("TxDest", "").strip(),
                    "asset_symbol": asset_symbol,
                    "quantity": quantity,
                    "value_usd": value_usd,
                    "fee_usd": fee_amount,
                    "tx_hash": tx_hash,
                    "wallet_id": row.get("From Wallet (read-only)", "").strip()
                    or row.get("To Wallet (read-only)", "").strip(),
                }
            )

        return records

    def parse_csv(
        self, csv_path: Path, format_type: str = "generic", **kwargs
    ) -> list[dict]:
        """Parse crypto transactions CSV (supports multiple formats)."""
        # Check if this is Koinly format
        if (
            format_type == "koinly"
            or "Stacks transactions" in str(csv_path)
            or "transactions-Table" in str(csv_path)
        ):
            return self._parse_koinly_format(csv_path)

        records = []

        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        rows = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if rows is None:
            raise ValueError("Could not read CSV file with any encoding")

        for row in rows:
            if not any(row.values()):
                continue

            date_str = (
                row.get("date") or row.get("transaction_date") or row.get("Date") or ""
            )
            tx_type = (
                row.get("type")
                or row.get("transaction_type")
                or row.get("Type")
                or "unknown"
            )
            symbol = row.get("symbol") or row.get("asset") or row.get("Symbol") or ""
            quantity_str = (
                row.get("quantity") or row.get("amount") or row.get("Quantity") or "0"
            )

            try:
                transaction_date = (
                    date_parser.parse(date_str).date()
                    if date_str
                    else datetime.now().date()
                )
                quantity = float(str(quantity_str).replace(",", ""))
                value_usd = (
                    float(str(row.get("value_usd", "0")).replace(",", ""))
                    if row.get("value_usd")
                    else 0.0
                )
                fee_usd = (
                    float(str(row.get("fee_usd", "0")).replace(",", ""))
                    if row.get("fee_usd")
                    else 0.0
                )
            except (ValueError, TypeError):
                continue

            records.append(
                {
                    "transaction_date": transaction_date,
                    "transaction_type": tx_type,
                    "blockchain": row.get("blockchain")
                    or row.get("chain")
                    or row.get("Blockchain")
                    or "",
                    "from_address": row.get("from")
                    or row.get("from_address")
                    or row.get("From")
                    or "",
                    "to_address": row.get("to")
                    or row.get("to_address")
                    or row.get("To")
                    or "",
                    "asset_symbol": symbol,
                    "quantity": quantity,
                    "value_usd": value_usd,
                    "fee_usd": fee_usd,
                    "tx_hash": row.get("tx_hash")
                    or row.get("hash")
                    or row.get("Hash")
                    or "",
                    "wallet_id": row.get("wallet_id")
                    or row.get("wallet")
                    or row.get("Wallet")
                    or "",
                }
            )

        return records

    def normalize_record(
        self, record: dict, source: str, source_file: str, **kwargs
    ) -> dict:
        """Normalize crypto transaction record."""
        crypto_tx_id = self.generate_id(record, source_file)

        return {
            "crypto_tx_id": crypto_tx_id,
            "transaction_date": record["transaction_date"],
            "transaction_type": record.get("transaction_type", "unknown"),
            "blockchain": record.get("blockchain", ""),
            "from_address": record.get("from_address", ""),
            "to_address": record.get("to_address", ""),
            "asset_symbol": record.get("asset_symbol", ""),
            "quantity": record.get("quantity", 0.0),
            "value_usd": record.get("value_usd", 0.0),
            "fee_usd": record.get("fee_usd", 0.0),
            "tx_hash": record.get("tx_hash", ""),
            "wallet_id": record.get("wallet_id", ""),
            "import_date": datetime.now().date(),
            "import_source_file": source_file,
        }


class TaxEventsImporter(BaseDataImporter):
    """Importer for tax event data."""

    def __init__(self, converter: CurrencyConverter):
        super().__init__("tax_events", converter)

    def parse_csv(self, csv_path: Path, **kwargs) -> list[dict]:
        """Parse tax events CSV - supports crypto sales format."""
        records = []

        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        rows = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if rows is None:
            raise ValueError("Could not read CSV file with any encoding")

        for row in rows:
            if not any(row.values()):
                continue

            # Check if this is crypto sales format (has Token, USD proceeds, USD acquisition value)
            if "Token" in str(row.keys()) and "USD proceeds" in str(row.keys()):
                # Crypto sales format
                date_str = row.get("Date", "").strip()
                token = row.get("Token", "").strip()
                quantity_str = row.get("Amount", "").strip()
                proceeds_str = row.get("USD proceeds", "").strip()
                cost_basis_str = row.get("USD acquisition value", "").strip()
                profit_loss_str = row.get("USD profit / loss", "").strip()

                if not date_str or not token:
                    continue

                try:
                    event_date = date_parser.parse(date_str).date()
                    quantity = (
                        float(str(quantity_str).replace(",", ""))
                        if quantity_str
                        else 0.0
                    )
                    proceeds = (
                        float(
                            str(proceeds_str)
                            .replace("US$", "")
                            .replace(",", "")
                            .replace("$", "")
                            .strip()
                        )
                        if proceeds_str
                        else 0.0
                    )
                    cost_basis = (
                        float(
                            str(cost_basis_str)
                            .replace("US$", "")
                            .replace(",", "")
                            .replace("$", "")
                            .strip()
                        )
                        if cost_basis_str
                        else 0.0
                    )

                    # Calculate gain/loss if not provided
                    if profit_loss_str:
                        gain_loss = float(
                            str(profit_loss_str)
                            .replace("US$", "")
                            .replace(",", "")
                            .replace("$", "")
                            .strip()
                        )
                    else:
                        gain_loss = proceeds - cost_basis
                except (ValueError, TypeError):
                    continue

                records.append(
                    {
                        "event_date": event_date,
                        "event_type": "sale",
                        "asset_symbol": token,
                        "quantity": quantity,
                        "cost_basis_usd": cost_basis,
                        "proceeds_usd": proceeds,
                        "gain_loss_usd": gain_loss,
                        "tax_year": event_date.year,
                        "jurisdiction": "",
                        "description": f"{token} sale",
                    }
                )
            else:
                # Standard format
                date_str = (
                    row.get("date") or row.get("event_date") or row.get("Date") or ""
                )
                event_type = (
                    row.get("type")
                    or row.get("event_type")
                    or row.get("Type")
                    or "unknown"
                )
                symbol = (
                    row.get("symbol") or row.get("asset") or row.get("Symbol") or ""
                )

                try:
                    event_date = (
                        date_parser.parse(date_str).date()
                        if date_str
                        else datetime.now().date()
                    )
                    quantity = float(str(row.get("quantity", "0")).replace(",", ""))
                    cost_basis = float(str(row.get("cost_basis", "0")).replace(",", ""))
                    proceeds = float(str(row.get("proceeds", "0")).replace(",", ""))
                    gain_loss = proceeds - cost_basis
                except (ValueError, TypeError):
                    continue

                records.append(
                    {
                        "event_date": event_date,
                        "event_type": event_type,
                        "asset_symbol": symbol,
                        "quantity": quantity,
                        "cost_basis_usd": cost_basis,
                        "proceeds_usd": proceeds,
                        "gain_loss_usd": gain_loss,
                        "tax_year": (
                            int(
                                row.get("tax_year")
                                or row.get("Tax Year")
                                or event_date.year
                            )
                            if row.get("tax_year") or row.get("Tax Year")
                            else event_date.year
                        ),
                        "jurisdiction": row.get("jurisdiction")
                        or row.get("Jurisdiction")
                        or "",
                        "description": row.get("description")
                        or row.get("Description")
                        or "",
                    }
                )

        return records

    def normalize_record(
        self, record: dict, source: str, source_file: str, **kwargs
    ) -> dict:
        """Normalize tax event record."""
        tax_event_id = self.generate_id(record, source_file)

        return {
            "tax_event_id": tax_event_id,
            "event_date": record["event_date"],
            "event_type": record.get("event_type", "unknown"),
            "asset_symbol": record.get("asset_symbol", ""),
            "quantity": record.get("quantity", 0.0),
            "cost_basis_usd": record.get("cost_basis_usd", 0.0),
            "proceeds_usd": record.get("proceeds_usd", 0.0),
            "gain_loss_usd": record.get("gain_loss_usd", 0.0),
            "tax_year": record.get("tax_year", record["event_date"].year),
            "jurisdiction": record.get("jurisdiction", ""),
            "description": record.get("description", ""),
            "import_date": datetime.now().date(),
            "import_source_file": source_file,
        }


class FlowsImporter(BaseDataImporter):
    """Importer for cash flow data."""

    def __init__(self, converter: CurrencyConverter):
        super().__init__("flows", converter)

    def parse_csv(self, csv_path: Path, **kwargs) -> list[dict]:
        """Parse flows CSV."""
        records = []

        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        rows = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if rows is None:
            raise ValueError("Could not read CSV file with any encoding")

        for row in rows:
            if not any(row.values()):
                continue

            flow_name = row.get("Flow", "").strip()
            if not flow_name:
                continue

            # Parse year and timeline
            year_str = row.get("Year", "").strip()
            timeline = row.get("Timeline", "").strip()

            # Parse amounts
            amount_usd_str = row.get("$", "").strip()
            amount_eur_str = row.get("€", "").strip()

            # Prefer USD if available
            if amount_usd_str:
                amount_str = amount_usd_str
                currency = "USD"
            elif amount_eur_str:
                amount_str = amount_eur_str
                currency = "EUR"
            else:
                continue

            try:
                # Clean amount string
                amount_str = (
                    amount_str.replace("US$", "")
                    .replace("€", "")
                    .replace(",", "")
                    .replace("$", "")
                    .replace("(", "-")
                    .replace(")", "")
                    .strip()
                )
                amount_val = float(amount_str)
            except (ValueError, TypeError):
                continue

            # Parse year
            try:
                year = int(year_str) if year_str else None
            except ValueError:
                year = None

            # Determine date from timeline or year
            if timeline:
                try:
                    flow_date = date_parser.parse(timeline).date()
                except Exception:
                    flow_date = (
                        datetime(year, 1, 1).date() if year else datetime.now().date()
                    )
            elif year:
                flow_date = datetime(year, 1, 1).date()
            else:
                flow_date = datetime.now().date()

            # Check if for cash flow
            for_cash_flow_str = row.get("For cash flow?", "").strip().lower()
            for_cash_flow = for_cash_flow_str in ["yes", "true", "1"]

            records.append(
                {
                    "flow_name": flow_name,
                    "flow_date": flow_date,
                    "year": year,
                    "timeline": timeline,
                    "amount_original": amount_val,
                    "currency_original": currency,
                    "for_cash_flow": for_cash_flow,
                    "party": row.get("Party", "").strip(),
                    "flow_type": row.get("Type", "").strip(),
                    "location": row.get("Location", "").strip(),
                    "category": row.get("Category", "").strip(),
                    "notes": row.get("Notes", "").strip(),
                }
            )

        return records

    def normalize_record(
        self, record: dict, source: str, source_file: str, **kwargs
    ) -> dict:
        """Normalize flow record."""
        flow_id = self.generate_id(record, source_file)

        # Convert to USD
        date_str = record["flow_date"].isoformat()
        currency = record.get("currency_original", "USD")
        amount_usd = self.converter.convert_to_usd(
            abs(record["amount_original"]), currency, date_str
        )

        # Restore sign
        if record["amount_original"] < 0:
            amount_usd = -amount_usd

        return {
            "flow_id": flow_id,
            "flow_name": record.get("flow_name", ""),
            "flow_date": record["flow_date"],
            "year": record.get("year"),
            "timeline": record.get("timeline", ""),
            "amount_usd": amount_usd,
            "amount_original": record["amount_original"],
            "currency_original": currency,
            "for_cash_flow": record.get("for_cash_flow", False),
            "party": record.get("party", ""),
            "flow_type": record.get("flow_type", ""),
            "location": record.get("location", ""),
            "category": record.get("category", ""),
            "notes": record.get("notes", ""),
            "import_date": datetime.now().date(),
            "import_source_file": source_file,
        }


class LiabilitiesImporter(BaseDataImporter):
    """Importer for liabilities data."""

    def __init__(self, converter: CurrencyConverter):
        super().__init__("liabilities", converter)

    def parse_csv(self, csv_path: Path, **kwargs) -> list[dict]:
        """Parse liabilities CSV."""
        records = []

        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        rows = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if rows is None:
            raise ValueError("Could not read CSV file with any encoding")

        # Use current date as snapshot date
        snapshot_date = datetime.now().date()

        for row in rows:
            if not any(row.values()):
                continue

            name = row.get("Name", "").strip()
            if not name:
                continue

            liability_type = row.get("Type", "").strip()

            # Parse amounts
            amount_usd_str = row.get("$", "").strip()
            amount_eur_str = row.get("€", "").strip()

            # Prefer USD if available
            if amount_usd_str:
                amount_str = amount_usd_str
                currency = "USD"
            elif amount_eur_str:
                amount_str = amount_eur_str
                currency = "EUR"
            else:
                continue

            try:
                # Clean amount string
                amount_str = (
                    amount_str.replace("US$", "")
                    .replace("€", "")
                    .replace(",", "")
                    .replace("$", "")
                    .replace("(", "-")
                    .replace(")", "")
                    .strip()
                )
                amount_val = float(amount_str)
            except (ValueError, TypeError):
                continue

            records.append(
                {
                    "name": name,
                    "liability_type": liability_type,
                    "amount_original": amount_val,
                    "currency_original": currency,
                    "snapshot_date": snapshot_date,
                    "notes": "",
                }
            )

        return records

    def normalize_record(
        self, record: dict, source: str, source_file: str, **kwargs
    ) -> dict:
        """Normalize liability record."""
        liability_id = self.generate_id(record, source_file)

        # Convert to USD
        date_str = record["snapshot_date"].isoformat()
        currency = record.get("currency_original", "USD")
        amount_usd = self.converter.convert_to_usd(
            abs(record["amount_original"]), currency, date_str
        )

        # Restore sign
        if record["amount_original"] < 0:
            amount_usd = -amount_usd

        return {
            "liability_id": liability_id,
            "name": record.get("name", ""),
            "liability_type": record.get("liability_type", ""),
            "amount_usd": amount_usd,
            "amount_original": record["amount_original"],
            "currency_original": currency,
            "snapshot_date": record["snapshot_date"],
            "notes": record.get("notes", ""),
            "import_date": datetime.now().date(),
            "import_source_file": source_file,
        }


class ContactsImporter(BaseDataImporter):
    """Importer for contact data."""

    def __init__(self, converter: CurrencyConverter):
        super().__init__("contacts", converter)

    def parse_csv(self, csv_path: Path, **kwargs) -> list[dict]:
        """Parse contacts CSV - supports Minted API format and generic formats."""
        records = []

        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        rows = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if rows is None:
            raise ValueError("Could not read CSV file with any encoding")

        # Check if this is Minted API format (has 'id', 'url', 'name', 'address1', etc.)
        is_minted_format = "id" in str(rows[0].keys()) if rows else False

        for row in rows:
            if not any(row.values()):
                continue

            if is_minted_format:
                # Minted API format
                name = row.get("name", "").strip()
                if not name:
                    continue

                # Parse updated_at date
                updated_date = None
                updated_at_str = row.get("updated_at", "").strip()
                if updated_at_str:
                    try:
                        updated_date = date_parser.parse(updated_at_str).date()
                    except (ValueError, TypeError):
                        pass

                # Build address from components
                address_parts = []
                if row.get("address1", "").strip():
                    address_parts.append(row.get("address1", "").strip())
                if row.get("address2", "").strip():
                    address_parts.append(row.get("address2", "").strip())
                address = ", ".join(address_parts) if address_parts else ""

                # Extract contact type from groups/labels if available
                contact_type = "personal"  # default
                groups = row.get("groups", "").strip()
                if groups:
                    contact_type = "group"

                records.append(
                    {
                        "name": name,
                        "contact_type": contact_type,
                        "category": row.get("labels", "").strip() or "",
                        "platform": "minted",
                        "email": "",  # Minted API doesn't include email
                        "phone": "",  # Minted API doesn't include phone
                        "address": address,
                        "country": row.get("country", "").strip() or "",
                        "website": "",
                        "notes": row.get("notes", "").strip() or "",
                        "first_contact_date": None,
                        "last_contact_date": updated_date,
                        "created_date": updated_date,  # Use updated_at as proxy
                        "updated_date": updated_date,
                    }
                )
            else:
                # Generic format - flexible column name matching
                name = (
                    row.get("name")
                    or row.get("Name")
                    or row.get("Full Name")
                    or row.get("full_name")
                    or row.get("Contact Name")
                    or ""
                ).strip()
                if not name:
                    continue

                # Parse dates if available
                first_contact_date = None
                last_contact_date = None
                created_date = None
                updated_date = None

                for date_field in [
                    "first_contact_date",
                    "First Contact Date",
                    "first_contact",
                    "First Contact",
                ]:
                    if row.get(date_field):
                        try:
                            first_contact_date = date_parser.parse(
                                row[date_field]
                            ).date()
                            break
                        except (ValueError, TypeError):
                            pass

                for date_field in [
                    "last_contact_date",
                    "Last Contact Date",
                    "last_contact",
                    "Last Contact",
                ]:
                    if row.get(date_field):
                        try:
                            last_contact_date = date_parser.parse(
                                row[date_field]
                            ).date()
                            break
                        except (ValueError, TypeError):
                            pass

                for date_field in [
                    "created_date",
                    "Created Date",
                    "created",
                    "Created",
                    "Date Added",
                ]:
                    if row.get(date_field):
                        try:
                            created_date = date_parser.parse(row[date_field]).date()
                            break
                        except (ValueError, TypeError):
                            pass

                for date_field in [
                    "updated_date",
                    "Updated Date",
                    "updated",
                    "Updated",
                    "Last Modified",
                ]:
                    if row.get(date_field):
                        try:
                            updated_date = date_parser.parse(row[date_field]).date()
                            break
                        except (ValueError, TypeError):
                            pass

                records.append(
                    {
                        "name": name,
                        "contact_type": (
                            row.get("contact_type")
                            or row.get("Type")
                            or row.get("type")
                            or row.get("Contact Type")
                            or "unknown"
                        ).strip(),
                        "category": (
                            row.get("category")
                            or row.get("Category")
                            or row.get("group")
                            or row.get("Group")
                            or ""
                        ).strip(),
                        "platform": (
                            row.get("platform")
                            or row.get("Platform")
                            or row.get("source")
                            or row.get("Source")
                            or ""
                        ).strip(),
                        "email": (
                            row.get("email")
                            or row.get("Email")
                            or row.get("Email Address")
                            or row.get("email_address")
                            or ""
                        ).strip(),
                        "phone": (
                            row.get("phone")
                            or row.get("Phone")
                            or row.get("Phone Number")
                            or row.get("phone_number")
                            or row.get("Mobile")
                            or row.get("mobile")
                            or ""
                        ).strip(),
                        "address": (
                            row.get("address")
                            or row.get("Address")
                            or row.get("Street Address")
                            or row.get("street_address")
                            or row.get("Address Line 1")
                            or ""
                        ).strip(),
                        "country": (
                            row.get("country")
                            or row.get("Country")
                            or row.get("Country Code")
                            or ""
                        ).strip(),
                        "website": (
                            row.get("website")
                            or row.get("Website")
                            or row.get("url")
                            or row.get("URL")
                            or ""
                        ).strip(),
                        "notes": (
                            row.get("notes")
                            or row.get("Notes")
                            or row.get("description")
                            or row.get("Description")
                            or row.get("Comments")
                            or ""
                        ).strip(),
                        "first_contact_date": first_contact_date,
                        "last_contact_date": last_contact_date,
                        "created_date": created_date,
                        "updated_date": updated_date,
                    }
                )

        return records

    def normalize_record(
        self, record: dict, source: str, source_file: str, **kwargs
    ) -> dict:
        """Normalize contact record."""
        contact_id = self.generate_id(record, source_file)

        # Set platform to source if not provided
        platform = record.get("platform", "").strip() or source

        # Set created_date to today if not provided
        created_date = record.get("created_date") or datetime.now().date()

        return {
            "contact_id": contact_id,
            "name": record.get("name", ""),
            "contact_type": record.get("contact_type", "unknown"),
            "category": record.get("category", ""),
            "platform": platform,
            "email": record.get("email", ""),
            "phone": record.get("phone", ""),
            "address": record.get("address", ""),
            "country": record.get("country", ""),
            "website": record.get("website", ""),
            "notes": record.get("notes", ""),
            "first_contact_date": record.get("first_contact_date"),
            "last_contact_date": record.get("last_contact_date"),
            "created_date": created_date,
            "updated_date": record.get("updated_date") or datetime.now().date(),
        }


# Registry of importers
IMPORTERS = {
    "transactions": TransactionImporter,
    "holdings": HoldingsImporter,
    "balances": BalancesImporter,
    "income": IncomeImporter,
    "crypto_transactions": CryptoTransactionsImporter,
    "tax_events": TaxEventsImporter,
    "flows": FlowsImporter,
    "liabilities": LiabilitiesImporter,
    "contacts": ContactsImporter,
}


def parse_options(options_str: str) -> dict[str, Any]:
    """Parse options string (key=value,key2=value2) into dictionary."""
    if not options_str:
        return {}

    result = {}
    for pair in options_str.split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def main():
    parser = argparse.ArgumentParser(description="Import financial data from CSV files")
    parser.add_argument(
        "data_type",
        type=str,
        choices=list(IMPORTERS.keys()),
        help="Type of data to import",
    )
    parser.add_argument("csv_file", type=str, help="Path to CSV file")
    parser.add_argument(
        "--source",
        type=str,
        default="unknown",
        help="Source/provider name (e.g., bank name, broker name)",
    )
    parser.add_argument(
        "--currency",
        type=str,
        default="USD",
        help="Original currency code (default: USD)",
    )
    parser.add_argument(
        "--options",
        type=str,
        default="",
        help="Additional options as key=value pairs (e.g., bank_provider=capital_one)",
    )

    args = parser.parse_args()

    # Parse options
    options = parse_options(args.options)

    # Add currency to options if provided
    if args.currency:
        options["currency"] = args.currency

    # Create converter and importer
    converter = CurrencyConverter()
    importer_class = IMPORTERS[args.data_type]
    importer = importer_class(converter)

    try:
        stats = importer.import_csv(Path(args.csv_file), source=args.source, **options)

        print("\nImport Summary:")
        print(f"  Data type: {args.data_type}")
        print(f"  New records imported: {stats['imported']}")
        print(f"  Duplicate records skipped: {stats['duplicates']}")
        print(f"  Total records in database: {stats['total']}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
