#!/usr/bin/env python3
"""
Transaction Import Script

Imports CSV files from bank providers, normalizes them to a standard schema,
converts currencies to USD, and stores in Parquet format with deduplication.

Usage:
    python import_transactions.py <csv_file> [--bank <bank_name>] [--currency <currency_code>]

Example:
    python import_transactions.py ~/Desktop/2025-12-05_transacción_descargar.csv --bank capital_one --currency EUR
"""

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from dateutil import parser as date_parser

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configuration
from scripts.config import DATA_DIR
from scripts.frankfurter_fx import CurrencyConverter

TRANSACTIONS_FILE = DATA_DIR / "transactions" / "transactions.parquet"
IMPORTS_DIR = DATA_DIR / "imports"
LOGS_DIR = DATA_DIR / "logs"
SCHEMA_FILE = DATA_DIR / "schemas" / "transaction_schema.json"

# Ensure directories exist
TRANSACTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


class BankParser:
    """Base class for bank-specific CSV parsers."""

    def parse(self, csv_path: Path) -> list[dict]:
        """Parse CSV file and return list of transaction dictionaries."""
        raise NotImplementedError


class CapitalOneParser(BankParser):
    """Parser for Capital One Spanish bank CSV format."""

    def parse(self, csv_path: Path) -> list[dict]:
        """Parse Capital One CSV format."""
        transactions = []

        # Normalize column names (handle encoding issues)
        def normalize_key(key):
            """Normalize column names to handle encoding variations."""
            replacements = {
                "Fecha de Transacci�n": "Fecha de Transacción",
                "Fecha de Publicacion": "Fecha de Publicacion",
                "Descripci�n": "Descripción",
                "Categor�a": "Categoría",
                "D�bito": "Débito",
                "Cr�dito": "Crédito",
            }
            return replacements.get(key, key)

        # Try multiple encodings
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
            raise ValueError(f"Could not read CSV file with any encoding: {encodings}")

        for row in rows:
            # Normalize row keys
            row = {normalize_key(k): v for k, v in row.items()}

            # Skip empty rows
            if not any(row.values()):
                continue

            # Parse dates
            try:
                transaction_date = date_parser.parse(
                    row.get("Fecha de Transacción", "")
                    or row.get("Fecha de Transacci�n", "")
                ).date()
                posting_date = date_parser.parse(
                    row.get("Fecha de Publicacion", "")
                    or row.get("Fecha de Publicación", "")
                ).date()
            except (KeyError, ValueError, TypeError):
                print(f"Warning: Could not parse dates in row: {row}")
                continue

            # Parse amounts (debit or credit)
            debit_str = (row.get("Débito", "") or row.get("D�bito", "") or "").strip()
            credit_str = (
                row.get("Crédito", "") or row.get("Cr�dito", "") or ""
            ).strip()

            try:
                debit = float(debit_str.replace(",", "")) if debit_str else 0.0
                credit = float(credit_str.replace(",", "")) if credit_str else 0.0
            except ValueError:
                continue

            # Determine amount (negative for debits, positive for credits)
            amount = credit - debit

            transaction = {
                "transaction_date": transaction_date,
                "posting_date": posting_date,
                "amount_original": amount,  # Keep sign for proper handling
                "description": (
                    row.get("Descripción", "") or row.get("Descripci�n", "") or ""
                ).strip(),
                "category": (
                    row.get("Categoría", "") or row.get("Categor�a", "") or ""
                ).strip(),
                "account_id": row.get("Tarjeta", "").strip(),
            }

            transactions.append(transaction)

        return transactions


class IbercajaParser(BankParser):
    """Parser for Ibercaja bank CSV format."""

    def parse(self, csv_path: Path) -> list[dict]:
        """Parse Ibercaja CSV format."""
        transactions = []

        # Try multiple encodings
        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]
        lines = None

        for encoding in encodings:
            try:
                with open(csv_path, encoding=encoding) as f:
                    lines = f.readlines()
                    if lines:
                        break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if lines is None:
            raise ValueError(f"Could not read CSV file with any encoding: {encodings}")

        # Find header row (contains "Nº Orden" or similar)
        header_idx = None
        for i, line in enumerate(lines):
            if "Nº Orden" in line or "Fecha Oper" in line:
                header_idx = i
                break

        if header_idx is None:
            raise ValueError("Could not find header row in CSV file")

        # Parse CSV starting from header row
        reader = csv.DictReader(lines[header_idx:])

        for row in reader:
            # Skip empty rows
            if not any(row.values()):
                continue

            # Parse dates (DD-MM-YYYY format)
            fecha_oper = row.get("Fecha Oper", "").strip()
            fecha_valor = row.get("Fecha Valor", "").strip()

            try:
                # Parse DD-MM-YYYY format
                if fecha_oper:
                    day, month, year = fecha_oper.split("-")
                    transaction_date = datetime(int(year), int(month), int(day)).date()
                else:
                    continue

                if fecha_valor:
                    day, month, year = fecha_valor.split("-")
                    posting_date = datetime(int(year), int(month), int(day)).date()
                else:
                    posting_date = transaction_date
            except (ValueError, AttributeError):
                print(f"Warning: Could not parse dates in row: {row}")
                continue

            # Parse amount (Importe column)
            importe_str = row.get("Importe", "").strip()
            if not importe_str:
                continue

            try:
                # Remove quotes and commas, handle negative amounts
                importe_str = importe_str.replace('"', "").replace(",", "").strip()
                amount = float(importe_str)
            except ValueError:
                continue

            # Get description and category
            concepto = row.get("Concepto", "").strip()
            descripcion = (
                row.get("Descripción", "").strip() or row.get("Descripci�n", "").strip()
            )

            # Combine concepto and descripcion for full description
            if concepto and descripcion:
                full_description = f"{concepto} - {descripcion}"
            elif concepto:
                full_description = concepto
            elif descripcion:
                full_description = descripcion
            else:
                full_description = ""

            # Use concepto as category, or derive from description
            category = concepto if concepto else ""

            # Get reference/account info
            referencia = row.get("Referencia", "").strip()

            transaction = {
                "transaction_date": transaction_date,
                "posting_date": posting_date,
                "amount_original": amount,  # Already has correct sign
                "description": full_description,
                "category": category,
                "account_id": (
                    referencia[:20] if referencia else ""
                ),  # Use reference as account ID
            }

            transactions.append(transaction)

        return transactions


class TransactionImporter:
    """Main transaction importer class."""

    def __init__(self):
        self.converter = CurrencyConverter()
        self.parsers = {
            "capital_one": CapitalOneParser(),
            "ibercaja": IbercajaParser(),
        }

    def generate_transaction_id(self, transaction: dict, source_file: str) -> str:
        """Generate unique transaction ID from transaction data."""
        # Create hash from key fields
        hash_string = (
            f"{transaction['transaction_date']}"
            f"{transaction['posting_date']}"
            f"{transaction['amount_original']}"
            f"{transaction['description']}"
            f"{transaction['account_id']}"
            f"{source_file}"
        )
        return hashlib.sha256(hash_string.encode()).hexdigest()[:16]

    def normalize_transaction(
        self, transaction: dict, bank_provider: str, currency: str, source_file: str
    ) -> dict:
        """Normalize transaction to standard schema."""
        transaction_id = self.generate_transaction_id(transaction, source_file)

        # Convert to USD
        date_str = transaction["transaction_date"].isoformat()
        amount_original = transaction["amount_original"]

        # Convert absolute value, then restore sign
        amount_usd = self.converter.convert_to_usd(
            abs(amount_original), currency, date_str
        )

        # Restore sign (negative for debits, positive for credits)
        if amount_original < 0:
            amount_usd = -amount_usd

        normalized = {
            "transaction_id": transaction_id,
            "transaction_date": transaction["transaction_date"],
            "posting_date": transaction["posting_date"],
            "amount_usd": amount_usd,
            "amount_original": transaction["amount_original"],
            "currency_original": currency,
            "description": transaction["description"],
            "category": transaction.get("category", ""),
            "account_id": transaction.get("account_id", ""),
            "bank_provider": bank_provider,
            "import_date": datetime.now().date(),
            "import_source_file": source_file,
        }

        return normalized

    def load_existing_transactions(self) -> pd.DataFrame:
        """Load existing transactions from Parquet file."""
        if TRANSACTIONS_FILE.exists():
            try:
                return pd.read_parquet(TRANSACTIONS_FILE)
            except Exception as e:
                print(f"Warning: Could not load existing transactions: {e}")
                return pd.DataFrame()
        return pd.DataFrame()

    def import_csv(
        self, csv_path: Path, bank_provider: str = "capital_one", currency: str = "EUR"
    ) -> dict:
        """Import CSV file and return import statistics."""
        csv_path = Path(csv_path)

        if not csv_path.exists():
            raise FileNotFoundError(f"CSV file not found: {csv_path}")

        # Get parser
        parser = self.parsers.get(bank_provider)
        if not parser:
            raise ValueError(
                f"Unknown bank provider: {bank_provider}. Available: {list(self.parsers.keys())}"
            )

        # Parse CSV
        print(f"Parsing CSV file: {csv_path}")
        raw_transactions = parser.parse(csv_path)
        print(f"Found {len(raw_transactions)} transactions")

        # Normalize transactions
        source_filename = csv_path.name
        normalized_transactions = [
            self.normalize_transaction(t, bank_provider, currency, source_filename)
            for t in raw_transactions
        ]

        # Load existing transactions
        existing_df = self.load_existing_transactions()

        # Create DataFrame from new transactions
        new_df = pd.DataFrame(normalized_transactions)

        # Deduplicate: remove transactions that already exist
        if not existing_df.empty:
            existing_ids = set(existing_df["transaction_id"].values)
            new_df = new_df[~new_df["transaction_id"].isin(existing_ids)]
            duplicates = len(normalized_transactions) - len(new_df)
        else:
            duplicates = 0

        # Merge with existing
        if existing_df.empty:
            combined_df = new_df
        else:
            combined_df = pd.concat([existing_df, new_df], ignore_index=True)

        # Save to Parquet
        combined_df.to_parquet(TRANSACTIONS_FILE, index=False, engine="pyarrow")
        print(f"Saved {len(combined_df)} total transactions to {TRANSACTIONS_FILE}")

        # Archive source CSV
        try:
            archive_path = (
                IMPORTS_DIR
                / f"{datetime.now().date()}_{bank_provider}_{source_filename}"
            )
            import shutil

            shutil.copy2(str(csv_path), str(archive_path))
            print(f"Archived source CSV to {archive_path}")
        except Exception as e:
            print(f"Warning: Could not archive source CSV: {e}")

        # Log import
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "source_file": str(csv_path),
            "bank_provider": bank_provider,
            "currency": currency,
            "transactions_imported": len(new_df),
            "transactions_duplicate": duplicates,
            "total_transactions": len(combined_df),
        }

        log_file = LOGS_DIR / "import_log.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

        return {
            "imported": len(new_df),
            "duplicates": duplicates,
            "total": len(combined_df),
        }


def main():
    parser = argparse.ArgumentParser(description="Import bank transaction CSV files")
    parser.add_argument("csv_file", type=str, help="Path to CSV file")
    parser.add_argument(
        "--bank",
        type=str,
        default="capital_one",
        choices=["capital_one", "ibercaja"],
        help="Bank provider name",
    )
    parser.add_argument(
        "--currency",
        type=str,
        default="EUR",
        help="Original currency code (default: EUR)",
    )

    args = parser.parse_args()

    importer = TransactionImporter()

    try:
        stats = importer.import_csv(
            Path(args.csv_file), bank_provider=args.bank, currency=args.currency
        )

        print("\nImport Summary:")
        print(f"  New transactions imported: {stats['imported']}")
        print(f"  Duplicate transactions skipped: {stats['duplicates']}")
        print(f"  Total transactions in database: {stats['total']}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
