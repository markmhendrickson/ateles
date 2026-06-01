#!/usr/bin/env python3
"""
Analyze Notion CSV files and generate schema analysis
"""

import csv
import json
from collections import defaultdict
from pathlib import Path


def analyze_csv(filepath):
    """Analyze a single CSV file and return schema info"""
    try:
        with open(filepath, encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = next(reader)

            # Get first few rows for data type inference
            sample_rows = []
            for i, row in enumerate(reader):
                if i >= 5:  # Get first 5 rows
                    break
                sample_rows.append(row)

            return {
                "headers": headers,
                "num_columns": len(headers),
                "sample_rows": len(sample_rows),
                "filepath": str(filepath),
            }
    except Exception as e:
        return {"error": str(e), "filepath": str(filepath)}


def main():
    """Main analysis function"""
    repo_root = Path(__file__).parent.parent
    import sys

    sys.path.insert(0, str(repo_root))
    from scripts.config import get_data_dir

    data_dir = get_data_dir()
    notion_dir = data_dir / "imports/notion"

    # Find all CSV files (excluding _all.csv duplicates)
    csv_files = []
    for filepath in notion_dir.rglob("*.csv"):
        if "_all.csv" not in str(filepath):
            csv_files.append(filepath)

    csv_files.sort()

    # Analyze each file
    results = {}
    categories = defaultdict(list)

    for filepath in csv_files:
        # Extract category from path
        relative_path = filepath.relative_to(notion_dir)
        parts = list(relative_path.parts)

        # Determine category
        if "Finances" in parts:
            category = "Finance"
        elif "Health" in parts:
            category = "Health"
        elif "Restricted" in parts:
            category = "Restricted"
        else:
            category = "General"

        # Extract database name from filename
        filename = filepath.stem
        # Remove UUID suffix
        db_name = filename.rsplit(" ", 1)[0] if " " in filename else filename

        analysis = analyze_csv(filepath)
        analysis["category"] = category
        analysis["db_name"] = db_name

        results[str(filepath)] = analysis
        categories[category].append(db_name)

    # Generate summary
    summary = {
        "total_files": len(csv_files),
        "categories": {cat: len(dbs) for cat, dbs in categories.items()},
        "databases_by_category": {
            cat: sorted(set(dbs)) for cat, dbs in categories.items()
        },
        "files": results,
    }

    # Write analysis to file
    output_path = data_dir / "logs/notion_csv_analysis.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Analyzed {len(csv_files)} CSV files")
    print("\nCategories:")
    for cat, count in summary["categories"].items():
        print(f"  {cat}: {count} files")

    print(f"\nAnalysis saved to: {output_path}")

    # Print summary by category
    print("\n=== Databases by Category ===")
    for cat in sorted(summary["databases_by_category"].keys()):
        dbs = summary["databases_by_category"][cat]
        print(f"\n{cat} ({len(set(dbs))} unique databases):")
        for db in sorted(set(dbs)):
            print(f"  - {db}")


if __name__ == "__main__":
    main()
