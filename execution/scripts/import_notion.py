#!/usr/bin/env python3
"""
Notion Data Import Script

Imports Notion exported CSV files into normalized parquet format.
Handles Notion-specific formatting (URLs, relations, multi-select fields).

Usage:
    python import_notion.py --all
    python import_notion.py --category finance
    python import_notion.py --database people
"""

import csv
import json
import re
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.config import DATA_DIR

# Configuration
NOTION_IMPORT_DIR = DATA_DIR / "imports" / "notion"
LOGS_DIR = DATA_DIR / "logs"
SCHEMAS_DIR = DATA_DIR / "schemas"

# Ensure directories exist
LOGS_DIR.mkdir(parents=True, exist_ok=True)


class NotionImporter:
    """Base class for Notion data import"""

    def __init__(self, csv_file: Path, data_type: str):
        self.csv_file = csv_file
        self.data_type = data_type
        self.schema = self.load_schema()
        self.parquet_dir = DATA_DIR / data_type
        self.parquet_file = self.parquet_dir / f"{data_type}.parquet"

        # Create data directory
        self.parquet_dir.mkdir(parents=True, exist_ok=True)

    def load_schema(self) -> dict:
        """Load schema for data type"""
        schema_file = SCHEMAS_DIR / f"{self.data_type}_schema.json"
        if not schema_file.exists():
            raise ValueError(f"Schema file not found: {schema_file}")

        with open(schema_file) as f:
            return json.load(f)["schema"]

    def clean_notion_url(self, text: str) -> str:
        """Remove Notion URL references from text"""
        if not text:
            return ""
        # Remove (https://www.notion.so/...) patterns
        text = re.sub(r"\s*\(https://www\.notion\.so/[^\)]+\)", "", text)
        return text.strip()

    def parse_notion_relations(self, text: str) -> list[str]:
        """Parse Notion relation field (comma-separated with URLs)"""
        if not text:
            return []
        # Split by comma and clean each item
        items = [self.clean_notion_url(item.strip()) for item in text.split(",")]
        return [item for item in items if item]

    def parse_date(self, date_str: str) -> date | None:
        """Parse various date formats from Notion"""
        if not date_str or date_str.strip() == "":
            return None

        date_str = date_str.strip()

        # Try various date formats
        formats = [
            "%B %d, %Y",  # "January 28, 2024"
            "%Y-%m-%d",  # "2024-01-28"
            "%m/%d/%Y",  # "01/28/2024"
            "%d/%m/%Y",  # "28/01/2024"
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue

        # If nothing works, return None
        print(f"Warning: Could not parse date: {date_str}")
        return None

    def parse_float(self, value: str) -> float | None:
        """Parse float, handling various formats"""
        if not value or value.strip() == "":
            return None

        # Remove currency symbols and commas
        value = value.replace("$", "").replace("€", "").replace(",", "").strip()

        try:
            return float(value)
        except ValueError:
            return None

    def parse_int(self, value: str) -> int | None:
        """Parse integer"""
        if not value or value.strip() == "":
            return None

        value = value.replace(",", "").strip()

        try:
            return int(value)
        except ValueError:
            return None

    def generate_id(self) -> str:
        """Generate unique ID"""
        return str(uuid.uuid4())[:16]

    def read_csv(self) -> list[dict]:
        """Read CSV file and return list of row dictionaries"""
        rows = []

        try:
            with open(self.csv_file, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(row)
        except Exception as e:
            print(f"Error reading {self.csv_file}: {e}")
            return []

        return rows

    def normalize_record(self, row: dict) -> dict:
        """
        Normalize a single record - override in subclasses
        """
        raise NotImplementedError("Subclasses must implement normalize_record")

    def load_existing_data(self) -> pd.DataFrame:
        """Load existing parquet data if it exists"""
        if self.parquet_file.exists():
            return pd.read_parquet(self.parquet_file)
        return pd.DataFrame()

    def import_data(self):
        """Main import function"""
        print(f"\nImporting {self.data_type} from {self.csv_file.name}...")

        # Read CSV
        rows = self.read_csv()
        if not rows:
            print("No data to import")
            return

        print(f"Found {len(rows)} rows")

        # Normalize records
        normalized_records = []
        for row in rows:
            try:
                normalized = self.normalize_record(row)
                if normalized:
                    normalized_records.append(normalized)
            except Exception as e:
                print(f"Error normalizing row: {e}")
                print(f"Row data: {row}")
                continue

        print(f"Normalized {len(normalized_records)} records")

        if not normalized_records:
            print("No records to import after normalization")
            return

        # Load existing data
        existing_df = self.load_existing_data()

        # Create new dataframe
        new_df = pd.DataFrame(normalized_records)

        # Combine with existing
        if not existing_df.empty:
            combined_df = pd.concat([existing_df, new_df], ignore_index=True)
            # Remove duplicates based on ID column
            id_col = f"{self.data_type.rstrip('s')}_id"
            if id_col in combined_df.columns:
                combined_df = combined_df.drop_duplicates(subset=[id_col], keep="last")
        else:
            combined_df = new_df

        # Save to parquet
        combined_df.to_parquet(self.parquet_file, index=False)

        print(f"Saved {len(combined_df)} total records to {self.parquet_file}")

        # Log import
        self.log_import(len(rows), len(normalized_records))

    def log_import(self, rows_read: int, rows_imported: int):
        """Log import statistics"""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "data_type": self.data_type,
            "csv_file": str(self.csv_file),
            "rows_read": rows_read,
            "rows_imported": rows_imported,
            "parquet_file": str(self.parquet_file),
        }

        log_file = LOGS_DIR / "notion_import_log.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")


# Database-specific importers


class PeopleImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "person_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "priority_for_mark": row.get("Priority for Mark", ""),
            "relation_with_mark": row.get("Relation with Mark", ""),
            "profession": row.get("Profession", ""),
            "locations": ", ".join(
                self.parse_notion_relations(row.get("Locations", ""))
            ),
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class WorkoutsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "workout_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "exercises": row.get("💪 Exercises", "") or row.get("Exercises", ""),
            "type": row.get("Type", ""),
            "circuits": row.get("Circuits", ""),
            "primary_muscles": row.get("Primary muscles", ""),
            "secondary_muscles": row.get("Secondary muscles", ""),
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class ExercisesImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "exercise_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "workouts": row.get("🤸‍♂️ Workouts", "") or row.get("Workouts", ""),
            "categories": row.get("Categories", ""),
            "primary_muscles": "",
            "secondary_muscles": "",
            "equipment": "",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class SetsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        name = row.get("Name", "")
        exercise = row.get("Exercise", "")

        # Parse date from name (format: "Exercise @Date")
        date_match = re.search(r"@([^(]+)", name)
        workout_date = self.parse_date(date_match.group(1)) if date_match else None

        # Parse exercise name
        exercise_name = (
            self.clean_notion_url(exercise)
            if exercise
            else self.clean_notion_url(name.split("@")[0])
        )

        return {
            "set_id": self.generate_id(),
            "name": name,
            "exercise": exercise,
            "exercise_name": exercise_name,
            "date": workout_date,
            "repetitions": self.parse_int(row.get("Repetitions", "")),
            "weight": row.get("Weight", ""),
            "type": row.get("Type", ""),
            "notes": row.get("Notes", ""),
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class MealsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        days = row.get("Days", "")
        meal_date = self.parse_date(days) if days else None

        return {
            "meal_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "type": row.get("Type", ""),
            "servings": row.get("🍜 Servings", "") or row.get("Servings", ""),
            "calories": self.parse_float(row.get("Calories", "")),
            "carbs": self.parse_float(row.get("Carbs", "")),
            "fat": self.parse_float(row.get("Fat", "")),
            "protein": self.parse_float(row.get("Protein", "")),
            "days": days,
            "date": meal_date,
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class FoodsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "food_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "category": row.get("Category", ""),
            "calories_per_100g": self.parse_float(row.get("Calories per 100g", "")),
            "protein_per_100g": self.parse_float(row.get("Protein per 100g", "")),
            "carbs_per_100g": self.parse_float(row.get("Carbs per 100g", "")),
            "fat_per_100g": self.parse_float(row.get("Fat per 100g", "")),
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class GoalsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "goal_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "parent_goals": row.get("🎯 Goals", "") or row.get("Goals", ""),
            "status": "",
            "priority": "",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class ProjectsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "project_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "goals": row.get("🎯 Goals", "") or row.get("Goals", ""),
            "status": "",
            "priority": "",
            "start_date": None,
            "end_date": None,
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class RecurringEventsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "event_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "time": row.get("Time", ""),
            "duration_hours": self.parse_float(row.get("Duration (hours)", "")),
            "times_per_year": self.parse_float(row.get("Times per year", "")),
            "categories": row.get("Categories", ""),
            "owner": self.clean_notion_url(row.get("Owner", "")),
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class EventsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "event_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "start_date": self.parse_date(row.get("Start date", "")),
            "end_date": self.parse_date(row.get("End date", "")),
            "locations": self.clean_notion_url(row.get("Locations", "")),
            "categories": row.get("Categories", ""),
            "type": row.get("Type", ""),
            "dates_status": row.get("Dates status", ""),
            "destination_status": row.get("Destination status", ""),
            "jet_lag": row.get("Jet lag", ""),
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class LocationsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "location_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "country": row.get("Country", ""),
            "people": row.get("People", ""),
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class BeliefsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "belief_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "categories": row.get("Categories", ""),
            "confidence_level": row.get("Confidence level", ""),
            "asset_types": row.get("💵 Asset types", "") or row.get("Asset types", ""),
            "date": self.parse_date(row.get("Date", "")),
            "events": row.get("📅 Events", "") or row.get("Events", ""),
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class EmotionsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "emotion_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "category": "",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class MoviesImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "movie_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "year": None,
            "rating": None,
            "watched_date": None,
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class PropertiesImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "property_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "address": "",
            "type": "",
            "purchase_date": None,
            "purchase_price": None,
            "current_value": None,
            "currency": "EUR",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class AccountsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "account_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "wallet": row.get("Wallet", ""),
            "wallet_name": self.clean_notion_url(row.get("Wallet", "")),
            "number": row.get("Number", ""),
            "categories": row.get("Categories", ""),
            "denomination": self.clean_notion_url(row.get("Denomination", "")),
            "status": "active",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class WalletsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "wallet_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "number": self.parse_int(row.get("Number", "")),
            "accounts": row.get("Accounts", ""),
            "categories": row.get("Categories", ""),
            "url": row.get("URL", ""),
            "urls": row.get("URLs", ""),
            "investments": row.get("⏳ Investments", "") or row.get("Investments", ""),
            "status": "active",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class CompaniesImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "company_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "type": "",
            "status": "active",
            "jurisdiction": "",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class InvestmentsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "investment_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "categories": row.get("Categories", ""),
            "asset_type": "",
            "status": "active",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class AddressesImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "address_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "accounts": row.get("Accounts", ""),
            "address": row.get("Address", ""),
            "blockchain": "",
            "address_type": "",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class AssetTypesImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "asset_type_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "symbol": "",
            "category": "",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class AssetValuesImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        name = row.get("Name", "")
        asset_type = name.split("@")[0] if "@" in name else ""

        return {
            "asset_value_id": self.generate_id(),
            "name": self.clean_notion_url(name),
            "asset_type": self.clean_notion_url(asset_type),
            "date": self.parse_date(row.get("Date", "")),
            "market_cap_current": self.parse_float(row.get("Market cap (current)", "")),
            "unit_price": self.parse_float(row.get("Unit", "")),
            "currency": "USD",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class ContractsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "contract_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "signed_date": self.parse_date(row.get("Signed", "")),
            "companies": row.get("Companies", ""),
            "files": row.get("Files", ""),
            "type": "",
            "status": "signed",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class EquityUnitsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "equity_unit_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "units": self.parse_float(row.get("Units", "")),
            "categories": row.get("Categories", ""),
            "investments": self.clean_notion_url(row.get("Investments", "")),
            "exercised": "Exercised" in row.get("Categories", ""),
            "issued_date": self.parse_date(row.get("Issued", "")),
            "exercised_date": (
                self.parse_date(row.get("Exercised", ""))
                if row.get("Exercised")
                else None
            ),
            "url": row.get("URL", ""),
            "files": row.get("Files & media", ""),
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class ExpensesImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "expense_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "category": "",
            "amount": None,
            "currency": "",
            "date": None,
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class FeesImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "fee_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "tags": row.get("Tags", ""),
            "amount": None,
            "currency": "",
            "frequency": "",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class FinancialStrategiesImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "strategy_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "date": self.parse_date(row.get("Date", "")),
            "description": "",
            "status": "active",
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class OrdersImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "order_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
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
            "import_source_file": f"notion:{self.csv_file.name}",
        }


class TaxFilingsImporter(NotionImporter):
    def normalize_record(self, row: dict) -> dict:
        return {
            "filing_id": self.generate_id(),
            "name": self.clean_notion_url(row.get("Name", "")),
            "jurisdiction": row.get("Jurisdiction", ""),
            "year": self.parse_int(row.get("Year", "")),
            "notes": "",
            "import_date": date.today(),
            "import_source_file": f"notion:{self.csv_file.name}",
        }
