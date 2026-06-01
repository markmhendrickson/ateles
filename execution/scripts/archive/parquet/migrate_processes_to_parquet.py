#!/usr/bin/env python3
"""
Migrate operational process markdown files to parquet data objects.

Scans strategy/operations/ for process files (daily-triage-protocol.md,
quarterly-portfolio-review-process.md), parses their content, and creates
parquet records in $DATA_DIR/processes/processes.parquet.

NOTE: After migration, markdown files are removed. Parquet is the single source of truth.

Usage:
    python execution/scripts/migrate_processes_to_parquet.py
"""

import hashlib
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from scripts.config import get_data_dir

DATA_DIR = get_data_dir()
OPERATIONS_DIR = PROJECT_ROOT / "strategy" / "operations"
SCHEMAS_DIR = DATA_DIR / "schemas"


def generate_process_id(filename: str) -> str:
    """Generate a 16-character ID from filename hash."""
    hash_obj = hashlib.sha256(filename.encode())
    return hash_obj.hexdigest()[:16]


def extract_frequency_from_content(content: str) -> str:
    """Extract frequency from content patterns."""
    content_lower = content.lower()

    # Check for explicit frequency mentions
    if "daily" in content_lower[:500]:  # Check first 500 chars
        return "daily"
    elif "quarterly" in content_lower[:500]:
        return "quarterly"
    elif "weekly" in content_lower[:500]:
        return "weekly"
    elif "monthly" in content_lower[:500]:
        return "monthly"
    elif "annual" in content_lower[:500]:
        return "annual"

    return "on_demand"


def extract_frequency_details(content: str, filename: str) -> str:
    """Extract specific schedule details from content."""
    content_lower = content.lower()

    # Daily triage
    if "daily-triage" in filename:
        if "weekdays" in content_lower or "weekday" in content_lower:
            return "Daily (weekdays)"
        return "Daily"

    # Quarterly review
    if "quarterly" in filename or "quarterly" in content_lower[:1000]:
        # Look for specific scheduling pattern
        schedule_match = re.search(
            r"(15th of [the ]*month following [each ]*quarter end|"
            r"January 15|April 15|July 15|October 15)",
            content,
            re.IGNORECASE,
        )
        if schedule_match:
            return "Quarterly (15th of month following quarter end)"
        return "Quarterly"

    return ""


def extract_domain(content: str, file_path: Path) -> str:
    """Extract domain from content or file location."""
    # Check file path for domain hints
    if "finance" in str(file_path):
        return "Finance"

    # Check content for domain mentions
    content_lower = content.lower()
    domains = []

    if any(
        word in content_lower[:1000] for word in ["finance", "portfolio", "rebalancing"]
    ):
        domains.append("Finance")
    if any(word in content_lower[:1000] for word in ["admin", "administrative"]):
        domains.append("Admin")
    if any(word in content_lower[:1000] for word in ["work", "professional"]):
        domains.append("Work")
    if any(word in content_lower[:1000] for word in ["health", "fitness"]):
        domains.append("Health")

    # Daily triage is cross-domain
    if "daily-triage" in str(file_path):
        return "Admin,Finance,Work,Health"

    return ",".join(domains) if domains else "Admin"


def parse_process_file(file_path: Path) -> dict[str, Any]:
    """Parse a process markdown file."""
    content = file_path.read_text(encoding="utf-8")

    # Generate ID
    process_id = generate_process_id(file_path.name)

    # Extract name from filename
    name = file_path.stem
    if "daily-triage-protocol" in name:
        name = "Daily Triage Protocol"
    elif "quarterly-portfolio-review-process" in name:
        name = "Quarterly Portfolio Review Process"
    else:
        name = name.replace("-", " ").replace("_", " ").title()

    # Extract frequency and details
    frequency = extract_frequency_from_content(content)
    frequency_details = extract_frequency_details(content, file_path.name)

    # Extract domain
    domain = extract_domain(content, file_path)

    # Extract description from first paragraph or heading
    description_match = re.search(r"^#[^#\n]+\n+([^\n]+)", content, re.MULTILINE)
    description = description_match.group(1).strip() if description_match else ""

    # Full workflow content is the entire markdown (we keep the full documentation)
    workflow_content = content

    # Extract related documentation references
    related_docs = []
    doc_matches = re.findall(
        r"(?:\*\*Reference:\*\*|See|Reference:)\s*`?([^`\n]+\.md)`?", content
    )
    related_docs.extend(doc_matches)

    # For merged processes, add merged source files to related_documentation
    if "daily-triage-protocol" in file_path.name:
        related_docs.append("strategy/operations/tasks-daily-review-process.md")
    elif "quarterly-portfolio-review" in file_path.name:
        related_docs.append(
            "strategy/operations/finance/quarterly-rebalancing-checklist.md"
        )
        related_docs.append("strategy/operations/operating-manual.md")

    # Build record
    record = {
        "process_id": process_id,
        "name": name,
        "description": description,
        "frequency": frequency,
        "frequency_details": frequency_details,
        "domain": domain,
        "status": "active",
        "workflow_content": workflow_content,
        "related_documentation": "|".join(set(related_docs)) if related_docs else None,
        "related_processes": None,  # Can be set later if needed
        "last_executed_date": None,
        "next_scheduled_date": None,
        "execution_count": 0,
        "average_duration_minutes": None,
        "configuration": None,
        "notes_updates": None,
        "created_date": date.today(),
        "updated_date": date.today(),
        "import_date": date.today(),
        "import_source_file": str(file_path.relative_to(PROJECT_ROOT)),
    }

    return record


def migrate_processes():
    """Migrate consolidated process markdown files to parquet."""
    print("=" * 80)
    print("Processes to Parquet Migration")
    print("=" * 80)
    print()

    # Define the consolidated process files to migrate
    process_files = [
        OPERATIONS_DIR / "daily-triage-protocol.md",
        OPERATIONS_DIR / "finance" / "quarterly-portfolio-review-process.md",
    ]

    # Check that files exist
    existing_files = [f for f in process_files if f.exists()]
    if not existing_files:
        print("[error] No process files found")
        print(
            f"  Expected files: {', '.join(str(f.relative_to(PROJECT_ROOT)) for f in process_files)}"
        )
        return

    print(f"Found {len(existing_files)} process file(s) to migrate")
    print()

    # Parse all process files
    records = []
    for file_path in existing_files:
        try:
            print(f"Processing: {file_path.relative_to(PROJECT_ROOT)}")
            record = parse_process_file(file_path)
            records.append(record)
            print(f"  ✓ Parsed: {record['name']}")
            print(f"    Frequency: {record['frequency']}")
            print(f"    Domain: {record['domain']}")
            print(f"    Status: {record['status']}")
        except Exception as e:
            print(f"  [error] Failed to parse {file_path.name}: {e}")
            import traceback

            traceback.print_exc()

    print()
    print(f"Successfully parsed {len(records)} process(es)")
    print()

    # Create DataFrame and save to parquet
    if records:
        try:
            import pandas as pd

            df = pd.DataFrame(records)

            # Ensure output directory exists
            output_dir = DATA_DIR / "processes"
            output_dir.mkdir(parents=True, exist_ok=True)

            # Save to parquet
            output_path = output_dir / "processes.parquet"
            df.to_parquet(output_path, index=False)

            print(f"✓ Created: {output_path}")
            print(f"  Records: {len(df)}")
            print()

            # Display summary
            print("Process Summary:")
            print("-" * 80)
            for _, row in df.iterrows():
                print(f"  • {row['name']}")
                print(f"    ID: {row['process_id']}")
                print(f"    Frequency: {row['frequency']}")
                if row["frequency_details"]:
                    print(f"    Details: {row['frequency_details']}")
                print(f"    Domain: {row['domain']}")
                print(f"    Source: {row['import_source_file']}")
                if row["related_documentation"]:
                    print(f"    Related docs: {row['related_documentation']}")
                print()

        except Exception as e:
            print(f"[error] Failed to create parquet file: {e}")
            import traceback

            traceback.print_exc()
    else:
        print("[warn] No processes to migrate")

    print("=" * 80)
    print("Migration complete")
    print("=" * 80)


if __name__ == "__main__":
    migrate_processes()
