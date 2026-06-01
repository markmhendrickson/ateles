#!/usr/bin/env python3
"""
Extract public data from $DATA_DIR for website publishing.

Based on analysis, extracts:
- Projects with public: true
- Other data types marked as public (if they have such flags)
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

# Add project root to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.scripts.config import DATA_DIR

# Output directory for extracted data
OUTPUT_DIR = PROJECT_ROOT / "tmp" / "website_data_analysis" / "extracted"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def extract_public_projects() -> list[dict[str, Any]]:
    """Extract projects marked as public."""
    projects_file = DATA_DIR / "projects" / "projects.parquet"

    if not projects_file.exists():
        print("Projects file not found")
        return []

    df = pd.read_parquet(projects_file)

    # Filter for public projects
    if "public" not in df.columns:
        print("Projects does not have 'public' field")
        return []

    public_df = df[df["public"] is True]

    if len(public_df) == 0:
        print("No public projects found")
        return []

    print(f"Found {len(public_df)} public projects")

    # Convert to list of dicts
    projects = []
    for _, row in public_df.iterrows():
        project = {}
        for col, val in row.items():
            if pd.isna(val):
                project[col] = None
            elif isinstance(val, date | datetime):
                project[col] = val.isoformat()
            elif isinstance(val, int | float | bool | str):
                project[col] = val
            else:
                project[col] = str(val)
        projects.append(project)

    return projects


def extract_public_goals() -> list[dict[str, Any]]:
    """Extract goals if they have public flag."""
    goals_file = DATA_DIR / "goals" / "goals.parquet"

    if not goals_file.exists():
        print("Goals file not found")
        return []

    df = pd.read_parquet(goals_file)

    # Check if has public field
    if "public" not in df.columns:
        print("Goals does not have 'public' field - skipping")
        return []

    public_df = df[df["public"] is True]

    if len(public_df) == 0:
        print("No public goals found")
        return []

    print(f"Found {len(public_df)} public goals")

    # Convert to list of dicts
    goals = []
    for _, row in public_df.iterrows():
        goal = {}
        for col, val in row.items():
            if pd.isna(val):
                goal[col] = None
            elif isinstance(val, date | datetime):
                goal[col] = val.isoformat()
            elif isinstance(val, int | float | bool | str):
                goal[col] = val
            else:
                goal[col] = str(val)
        goals.append(goal)

    return goals


def extract_domains() -> list[dict[str, Any]]:
    """Extract domain registrations (already public information)."""
    domains_file = DATA_DIR / "domains" / "domains.parquet"

    if not domains_file.exists():
        print("Domains file not found")
        return []

    df = pd.read_parquet(domains_file)

    if len(df) == 0:
        print("No domains found")
        return []

    print(f"Found {len(df)} domains")

    # Convert to list of dicts
    domains = []
    for _, row in df.iterrows():
        domain = {}
        for col, val in row.items():
            if pd.isna(val):
                domain[col] = None
            elif isinstance(val, date | datetime):
                domain[col] = val.isoformat()
            elif isinstance(val, int | float | bool | str):
                domain[col] = val
            else:
                domain[col] = str(val)
        domains.append(domain)

    return domains


def main():
    """Main extraction function."""
    print("Extracting public data from $DATA_DIR...")
    print(f"DATA_DIR: {DATA_DIR}")
    print()

    results = {
        "extracted_at": datetime.now().isoformat(),
        "source": str(DATA_DIR),
        "data_types": {},
    }

    # Extract projects
    print("Extracting projects with public: true...")
    projects = extract_public_projects()
    results["data_types"]["projects"] = {"count": len(projects), "records": projects}
    print()

    # Extract goals
    print("Extracting goals with public: true...")
    goals = extract_public_goals()
    results["data_types"]["goals"] = {"count": len(goals), "records": goals}
    print()

    # Extract domains (already public info)
    print("Extracting domains...")
    domains = extract_domains()
    results["data_types"]["domains"] = {"count": len(domains), "records": domains}
    print()

    # Save extraction results
    output_file = OUTPUT_DIR / "extracted_data.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)

    print("=" * 80)
    print("EXTRACTION SUMMARY")
    print("=" * 80)
    print()
    print(f"Projects: {len(projects)} records")
    print(f"Goals: {len(goals)} records")
    print(f"Domains: {len(domains)} records")
    print()
    print(f"Total public records: {len(projects) + len(goals) + len(domains)}")
    print()
    print(f"Results saved to: {output_file}")
    print()

    if len(projects) + len(goals) + len(domains) == 0:
        print("⚠️  No public data found to extract.")
        print()
        print("Recommendation:")
        print("- Review projects and mark appropriate ones as public: true")
        print("- Add public flags to other data types if needed")
        print("- Continue using manually-curated JSON files for website content")
    else:
        print("✓ Public data extracted successfully")
        print()
        print("Next steps:")
        print("- Review extracted data for appropriateness")
        print("- Transform to website JSON format")
        print("- Generate JSON files for website")


if __name__ == "__main__":
    main()
