#!/usr/bin/env python3
"""
Analyze all data types in $DATA_DIR for website publishing suitability.

Generates a comprehensive report categorizing each data type by:
- Record count
- Content quality (non-empty fields)
- Privacy concerns
- Publishing suitability
"""

import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

# Add project root to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.scripts.config import DATA_DIR

# Output directory for reports
REPORTS_DIR = PROJECT_ROOT / "tmp" / "website_data_analysis"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# Privacy classification
PRIVACY_CATEGORIES = {
    "EXCLUDE_FINANCIAL": [
        "transactions",
        "holdings",
        "balances",
        "income",
        "tax_events",
        "tax_filings",
        "crypto_transactions",
        "flows",
        "fixed_costs",
        "liabilities",
        "investments",
        "transfers",
        "wallets",
        "asset_types",
        "asset_values",
        "equity_units",
        "bank_certificates",
        "financial_strategies",
        "accounts",
        "account_identifiers",
        "orders",
        "properties",
        "property_equipment",
    ],
    "EXCLUDE_PERSONAL_IDENTIFIERS": [
        "contacts",
        "addresses",
        "user_accounts",
        "emails",
        "messages",
        "payroll_documents",
        "relationships",
    ],
    "EXCLUDE_PRIVATE_NOTES": ["arguments", "emotions", "disputes"],
    "EXCLUDE_HEALTH": ["workouts", "exercises", "sets", "meals", "foods"],
    "EXCLUDE_INTERNAL_OPS": [
        "execution_plans",
        "processes",
        "tasks",
        "task_comments",
        "task_attachments",
        "task_stories",
        "task_dependencies",
        "daily_triages",
        "mcp_server_integrations",
        "env_var_mappings",
        "task_custom_fields",
    ],
    "POTENTIALLY_SUITABLE": [
        "projects",
        "goals",
        "beliefs",
        "notes",
        "domains",
        "companies",
        "movies",
        "transcriptions",
        "events",
        "recurring_events",
        "locations",
        "purchases",
        "people",
        "outcomes",
        "related_materials",
        "contracts",
    ],
}


def get_all_data_types() -> list[str]:
    """Get list of all data type directories in DATA_DIR."""
    data_types = []
    for item in DATA_DIR.iterdir():
        if item.is_dir() and not item.name.startswith("."):
            # Check if it has a parquet file
            parquet_files = list(item.glob("*.parquet"))
            if parquet_files:
                data_types.append(item.name)
    return sorted(data_types)


def classify_privacy(data_type: str) -> tuple[str, str]:
    """Classify data type by privacy category."""
    for category, types in PRIVACY_CATEGORIES.items():
        if data_type in types:
            return category, get_category_reason(category)
    return "UNCLASSIFIED", "Not yet classified"


def get_category_reason(category: str) -> str:
    """Get human-readable reason for privacy category."""
    reasons = {
        "EXCLUDE_FINANCIAL": "Financial data - sensitive",
        "EXCLUDE_PERSONAL_IDENTIFIERS": "Personal identifiers - PII",
        "EXCLUDE_PRIVATE_NOTES": "Private notes - sensitive",
        "EXCLUDE_HEALTH": "Health data - sensitive",
        "EXCLUDE_INTERNAL_OPS": "Internal operations - not public-facing",
        "POTENTIALLY_SUITABLE": "Potentially suitable for publishing",
        "UNCLASSIFIED": "Not yet classified",
    }
    return reasons.get(category, "Unknown")


def analyze_data_type(data_type: str) -> dict[str, Any]:
    """Analyze a single data type for publishing suitability."""
    result = {
        "data_type": data_type,
        "exists": False,
        "record_count": 0,
        "parquet_file": None,
        "schema": None,
        "sample_records": [],
        "has_public_flag": False,
        "public_record_count": 0,
        "non_empty_fields": [],
        "privacy_category": None,
        "privacy_reason": None,
        "suitability": "UNKNOWN",
        "suitability_notes": [],
    }

    # Check if directory exists
    data_dir = DATA_DIR / data_type
    if not data_dir.exists():
        result["suitability"] = "NOT_FOUND"
        result["suitability_notes"].append("Directory does not exist")
        return result

    # Find parquet file
    parquet_files = list(data_dir.glob("*.parquet"))
    if not parquet_files:
        result["suitability"] = "NO_DATA"
        result["suitability_notes"].append("No parquet file found")
        return result

    result["exists"] = True
    result["parquet_file"] = str(parquet_files[0])

    try:
        # Read only metadata first to get row count
        parquet_file = pq.ParquetFile(parquet_files[0])
        result["record_count"] = parquet_file.metadata.num_rows

        if result["record_count"] == 0:
            result["suitability"] = "NO_DATA"
            result["suitability_notes"].append("Empty parquet file")
            return result

        # Read only first 10 rows for analysis (more efficient for large files)
        df = pd.read_parquet(parquet_files[0], engine="pyarrow").head(10)

        # Get schema from dataframe dtypes (simpler and more reliable)
        result["schema"] = {col: str(dtype) for col, dtype in df.dtypes.items()}

        # Check for public flag
        if "public" in df.columns:
            result["has_public_flag"] = True
            # For large files, only check if any public records exist in sample
            if df["public"].dtype == bool or df["public"].dtype == "boolean":
                result["public_record_count"] = int(df["public"].sum())
                if result["public_record_count"] > 0:
                    result["suitability_notes"].append(
                        "Has public records in sample (full count not computed for large files)"
                    )

        # Analyze non-empty fields (sample first 5 records)
        sample_df = df.head(5)
        non_empty_fields = set()
        for col in df.columns:
            non_empty = sample_df[col].notna().sum()
            if non_empty > 0:
                # Check if any non-empty value is not just empty string
                non_empty_values = sample_df[col].dropna()
                if len(non_empty_values) > 0:
                    # Check for non-empty strings
                    if non_empty_values.dtype == object:
                        has_content = any(
                            str(val).strip() != "" for val in non_empty_values
                        )
                        if has_content:
                            non_empty_fields.add(col)
                    else:
                        non_empty_fields.add(col)

        result["non_empty_fields"] = sorted(list(non_empty_fields))

        # Get sample records (convert to dict, handle dates) - only first 3
        sample_records = []
        for _, row in sample_df.head(3).iterrows():
            record = {}
            for col, val in row.items():
                if pd.isna(val):
                    record[col] = None
                elif isinstance(val, date | datetime):
                    record[col] = val.isoformat()
                elif isinstance(val, int | float | bool | str):
                    record[col] = val
                else:
                    record[col] = str(val)
            sample_records.append(record)
        result["sample_records"] = sample_records

    except Exception as e:
        result["suitability"] = "ERROR"
        result["suitability_notes"].append(f"Error reading parquet: {str(e)}")
        return result

    # Classify privacy
    privacy_category, privacy_reason = classify_privacy(data_type)
    result["privacy_category"] = privacy_category
    result["privacy_reason"] = privacy_reason

    # Determine suitability
    if privacy_category.startswith("EXCLUDE_"):
        result["suitability"] = "UNSUITABLE"
        result["suitability_notes"].append(privacy_reason)
    elif privacy_category == "POTENTIALLY_SUITABLE":
        # Further analysis
        if result["record_count"] == 0:
            result["suitability"] = "NO_DATA"
            result["suitability_notes"].append("No records available")
        elif len(result["non_empty_fields"]) < 3:
            result["suitability"] = "POOR_QUALITY"
            result["suitability_notes"].append("Too few non-empty fields")
        elif result["has_public_flag"] and result["public_record_count"] == 0:
            result["suitability"] = "NO_PUBLIC_DATA"
            result["suitability_notes"].append("Has public flag but no public records")
        elif result["has_public_flag"] and result["public_record_count"] > 0:
            result["suitability"] = "SUITABLE"
            result["suitability_notes"].append(
                f"Has {result['public_record_count']} public records"
            )
        else:
            result["suitability"] = "NEEDS_REVIEW"
            result["suitability_notes"].append(
                "Potentially suitable but needs manual review"
            )
    else:
        result["suitability"] = "NEEDS_REVIEW"
        result["suitability_notes"].append("Unclassified data type")

    return result


def generate_summary_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate summary statistics from analysis results."""
    summary = {
        "total_data_types": len(results),
        "by_suitability": defaultdict(int),
        "by_privacy_category": defaultdict(int),
        "suitable_types": [],
        "needs_review_types": [],
        "unsuitable_types": [],
        "total_records_analyzed": 0,
        "total_public_records": 0,
    }

    for result in results:
        summary["by_suitability"][result["suitability"]] += 1
        summary["by_privacy_category"][result["privacy_category"]] += 1
        summary["total_records_analyzed"] += result["record_count"]
        summary["total_public_records"] += result.get("public_record_count", 0)

        if result["suitability"] == "SUITABLE":
            summary["suitable_types"].append(
                {
                    "data_type": result["data_type"],
                    "record_count": result["record_count"],
                    "public_record_count": result.get("public_record_count", 0),
                    "notes": result["suitability_notes"],
                }
            )
        elif result["suitability"] == "NEEDS_REVIEW":
            summary["needs_review_types"].append(
                {
                    "data_type": result["data_type"],
                    "record_count": result["record_count"],
                    "has_public_flag": result["has_public_flag"],
                    "notes": result["suitability_notes"],
                }
            )
        elif result["suitability"] == "UNSUITABLE":
            summary["unsuitable_types"].append(
                {"data_type": result["data_type"], "reason": result["privacy_reason"]}
            )

    # Convert defaultdicts to regular dicts for JSON serialization
    summary["by_suitability"] = dict(summary["by_suitability"])
    summary["by_privacy_category"] = dict(summary["by_privacy_category"])

    return summary


def main():
    """Main analysis function."""
    print("Analyzing all data types in $DATA_DIR for website publishing...")
    print(f"DATA_DIR: {DATA_DIR}")
    print()

    # Get all data types
    data_types = get_all_data_types()
    print(f"Found {len(data_types)} data types")
    print()

    # Analyze each data type
    results = []
    for i, data_type in enumerate(data_types, 1):
        print(f"[{i}/{len(data_types)}] Analyzing {data_type}...", end=" ", flush=True)
        result = analyze_data_type(data_type)
        results.append(result)
        print(f"{result['suitability']} ({result['record_count']} records)", flush=True)

    print()

    # Generate summary
    summary = generate_summary_report(results)

    # Save detailed results
    detailed_report_path = REPORTS_DIR / "detailed_analysis.json"
    with open(detailed_report_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Detailed analysis saved to: {detailed_report_path}")

    # Save summary
    summary_report_path = REPORTS_DIR / "summary_report.json"
    with open(summary_report_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary report saved to: {summary_report_path}")

    # Print summary to console
    print()
    print("=" * 80)
    print("SUMMARY REPORT")
    print("=" * 80)
    print()
    print(f"Total data types analyzed: {summary['total_data_types']}")
    print(f"Total records analyzed: {summary['total_records_analyzed']:,}")
    print(f"Total public records found: {summary['total_public_records']}")
    print()
    print("By Suitability:")
    for suitability, count in sorted(summary["by_suitability"].items()):
        print(f"  {suitability}: {count}")
    print()
    print("By Privacy Category:")
    # Filter out None keys and sort
    privacy_items = [
        (k, v) for k, v in summary["by_privacy_category"].items() if k is not None
    ]
    none_count = summary["by_privacy_category"].get(None, 0)
    for category, count in sorted(privacy_items):
        print(f"  {category}: {count}")
    if none_count > 0:
        print(f"  (Unclassified): {none_count}")
    print()

    if summary["suitable_types"]:
        print(f"SUITABLE FOR PUBLISHING ({len(summary['suitable_types'])} types):")
        for item in summary["suitable_types"]:
            print(
                f"  - {item['data_type']}: {item['public_record_count']} public records"
            )
    print()

    if summary["needs_review_types"]:
        print(f"NEEDS MANUAL REVIEW ({len(summary['needs_review_types'])} types):")
        for item in summary["needs_review_types"]:
            print(
                f"  - {item['data_type']}: {item['record_count']} records (public_flag={item['has_public_flag']})"
            )
    print()

    print("=" * 80)
    print()
    print("Analysis complete. Review detailed results for next steps.")


if __name__ == "__main__":
    main()
