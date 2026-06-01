#!/usr/bin/env python3
"""
Migrate execution plan markdown files to parquet data objects.

Scans truth/operations/execution-plans/ for *-execution-plan.md and *-project-plan.md files,
parses their structured content, and creates parquet records in $DATA_DIR/execution_plans/execution_plans.parquet.

Usage:
    python execution/scripts/migrate_execution_plans_to_parquet.py
"""

import hashlib
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from scripts.config import get_data_dir

DATA_DIR = get_data_dir()
EXECUTION_PLANS_DIR = PROJECT_ROOT / "truth" / "operations" / "execution-plans"
SCHEMAS_DIR = DATA_DIR / "schemas"


def generate_plan_id(filename: str) -> str:
    """Generate a 16-character ID from filename hash."""
    hash_obj = hashlib.sha256(filename.encode())
    return hash_obj.hexdigest()[:16]


def parse_date(date_str: Optional[str]) -> Optional[date]:
    """Parse date string in various formats."""
    if not date_str or date_str.strip() == "":
        return None

    date_str = date_str.strip()

    # Try various date formats
    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue

    # If no format matches, return None
    print(f"  [warn] Could not parse date: {date_str}")
    return None


def extract_frontmatter(content: str) -> dict[str, Any]:
    """Extract frontmatter metadata from markdown content."""
    metadata = {}

    # Try to find frontmatter patterns (lines starting with ** at the beginning)
    lines = content.split("\n")
    for i, line in enumerate(lines[:20]):  # Check first 20 lines
        line = line.strip()

        # Pattern: **Key:** Value
        if line.startswith("**") and ":**" in line:
            key_val = line.split(":**", 1)
            if len(key_val) == 2:
                key = key_val[0].replace("**", "").strip()
                value = key_val[1].strip()
                metadata[key.lower().replace(" ", "_")] = value

        # Pattern: **Key:** `value`
        elif line.startswith("**") and ":" in line:
            parts = line.split(":", 1)
            if len(parts) == 2:
                key = parts[0].replace("**", "").strip()
                value = parts[1].strip().replace("`", "").replace("**", "")
                metadata[key.lower().replace(" ", "_")] = value

    return metadata


def extract_section(content: str, section_name: str) -> Optional[str]:
    """Extract content from a markdown section."""
    # Try various section header patterns
    patterns = [
        rf"^##\s+{re.escape(section_name)}.*?\n(.*?)(?=^##\s|\Z)",
        rf"^###\s+{re.escape(section_name)}.*?\n(.*?)(?=^###\s|^##\s|\Z)",
        rf"^\*\*{re.escape(section_name)}:\*\*\s*(.*?)(?=^\*\*[A-Z]|\Z)",
    ]

    for pattern in patterns:
        match = re.search(pattern, content, re.MULTILINE | re.DOTALL | re.IGNORECASE)
        if match:
            section_content = match.group(1).strip()
            return section_content if section_content else None

    return None


def parse_execution_plan(file_path: Path) -> dict[str, Any]:
    """Parse an execution plan markdown file."""
    content = file_path.read_text(encoding="utf-8")

    # Extract frontmatter
    metadata = extract_frontmatter(content)

    # Generate ID
    execution_plan_id = generate_plan_id(file_path.name)

    # Extract name from filename (remove -execution-plan.md or -project-plan.md suffix)
    name = file_path.stem
    name = re.sub(r"-(execution|project)-plan$", "", name)
    name = name.replace("-", " ").title()

    # Extract sections
    objective = extract_section(content, "Objective and Scope") or extract_section(
        content, "Objective"
    )
    scope_section = extract_section(content, "Scope")
    out_of_scope = extract_section(content, "Out of Scope")
    milestones = (
        extract_section(content, "Key Milestones and Phases")
        or extract_section(content, "Steps/Phases")
        or extract_section(content, "Phases")
    )
    dependencies = extract_section(
        content, "Dependencies and Constraints"
    ) or extract_section(content, "Dependencies")
    constraints = extract_section(content, "Constraints")
    success_criteria = extract_section(content, "Success Criteria") or extract_section(
        content, "Completion Criteria"
    )
    related_tasks = extract_section(content, "Related Tasks")
    related_docs = extract_section(content, "Related Documentation")
    notes = extract_section(content, "Notes/Updates") or extract_section(
        content, "Notes"
    )

    # Extract dates from metadata
    start_date = parse_date(metadata.get("start_date"))
    target_date = parse_date(metadata.get("target_completion_date"))
    created_date = parse_date(metadata.get("created"))

    # Build record
    record = {
        "execution_plan_id": execution_plan_id,
        "name": name,
        "original_file_path": str(file_path.relative_to(PROJECT_ROOT)),
        "project_id": metadata.get("project_id", "").strip(),
        "project_name": metadata.get("project_name", "").strip()
        or metadata.get("name", "").strip(),
        "status": metadata.get("status", "planned").lower().strip(),
        "domain": metadata.get("domain", "").strip(),
        "priority": metadata.get("priority", "medium").lower().strip(),
        "start_date": start_date,
        "target_completion_date": target_date,
        "objective": objective,
        "scope": scope_section,
        "out_of_scope": out_of_scope,
        "milestones_phases": milestones,
        "dependencies": dependencies,
        "constraints": constraints,
        "success_criteria": success_criteria,
        "related_tasks": related_tasks,
        "related_documentation": related_docs,
        "notes_updates": notes,
        "created_date": created_date or date.today(),
        "updated_date": date.today(),
        "import_date": date.today(),
        "import_source_file": str(file_path.relative_to(PROJECT_ROOT)),
    }

    return record


def migrate_execution_plans():
    """Migrate all execution plan markdown files to parquet."""
    print("=" * 80)
    print("Execution Plans to Parquet Migration")
    print("=" * 80)
    print()

    # Check that execution plans directory exists
    if not EXECUTION_PLANS_DIR.exists():
        print(f"[error] Execution plans directory not found: {EXECUTION_PLANS_DIR}")
        return

    # Find all execution plan and project plan markdown files
    execution_plan_files = list(EXECUTION_PLANS_DIR.glob("*-execution-plan.md"))
    project_plan_files = list(EXECUTION_PLANS_DIR.glob("*-project-plan.md"))
    all_files = execution_plan_files + project_plan_files

    print(f"Found {len(all_files)} execution plan files:")
    print(f"  - {len(execution_plan_files)} execution plans")
    print(f"  - {len(project_plan_files)} project plans")
    print()

    if not all_files:
        print("[warn] No execution plan files found to migrate")
        return

    # Parse all files
    records = []
    errors = []

    for file_path in all_files:
        print(f"[parse] {file_path.name}")
        try:
            record = parse_execution_plan(file_path)
            records.append(record)
            print(f"  ✓ Parsed: {record['name']}")
            print(f"    - ID: {record['execution_plan_id']}")
            print(f"    - Status: {record['status']}")
            print(f"    - Domain: {record['domain'] or 'N/A'}")
        except Exception as e:
            print(f"  ✗ Error parsing: {str(e)}")
            errors.append((file_path.name, str(e)))

    print()
    print(f"Parsed {len(records)} records successfully")
    if errors:
        print(f"Errors: {len(errors)}")
        for filename, error in errors:
            print(f"  - {filename}: {error}")
    print()

    # Write records using MCP parquet server
    # For now, write directly to parquet file using pandas
    # (In production, would use MCP tools, but for migration, direct write is acceptable)
    import pandas as pd

    output_dir = DATA_DIR / "execution_plans"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "execution_plans.parquet"

    # Create DataFrame
    df = pd.DataFrame(records)

    # Convert date columns
    date_columns = [
        "start_date",
        "target_completion_date",
        "created_date",
        "updated_date",
        "import_date",
    ]
    for col in date_columns:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # Create snapshot if file exists
    if output_file.exists():
        snapshot_dir = DATA_DIR / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        snapshot_file = snapshot_dir / f"execution_plans-{timestamp}.parquet"
        print(f"[snapshot] Creating snapshot: {snapshot_file.name}")
        df_existing = pd.read_parquet(output_file)
        df_existing.to_parquet(snapshot_file, index=False)

    # Write parquet file
    print(f"[write] Writing to {output_file}")
    df.to_parquet(output_file, index=False)

    print()
    print("=" * 80)
    print(f"Migration complete: {len(records)} records written to parquet")
    print(f"Location: {output_file}")
    print("=" * 80)

    # Summary
    print()
    print("Summary:")
    print(f"  - Total files processed: {len(all_files)}")
    print(f"  - Records created: {len(records)}")
    print(f"  - Errors: {len(errors)}")
    print()

    # Show status distribution
    if records:
        status_counts = {}
        for record in records:
            status = record.get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

        print("Status distribution:")
        for status, count in sorted(status_counts.items()):
            print(f"  - {status}: {count}")


if __name__ == "__main__":
    migrate_execution_plans()
