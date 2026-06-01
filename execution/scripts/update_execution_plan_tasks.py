#!/usr/bin/env python3
"""
Update execution plans with related tasks by matching tasks' execution_plan_path
to execution plans' original_file_path.

This script works around MCP server date handling issues by directly updating
the parquet file with proper date field handling.
"""

import os
import re
from datetime import date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# Get DATA_DIR from environment
DATA_DIR = Path(
    os.environ.get(
        "DATA_DIR",
        "/Users/markmhendrickson/Library/Mobile Documents/com~apple~CloudDocs/Documents/data",
    )
)


def normalize_path(p):
    """Normalize file paths for matching."""
    if pd.isna(p) or not p:
        return ""
    p = str(p).lower().strip().strip("/").replace("\\", "/")
    return p


def extract_name(p):
    """Extract execution plan name from file path."""
    if pd.isna(p) or not p:
        return ""
    basename = Path(str(p)).stem.lower()
    basename = re.sub(
        r"-(execution-plan|project-plan|plan|status|guide)$", "", basename
    )
    return basename


def parse_related_tasks(related_tasks_str):
    """Parse related_tasks string into set of task IDs."""
    if pd.isna(related_tasks_str) or not related_tasks_str:
        return set()

    task_ids = set()
    for line in str(related_tasks_str).split("\n"):
        line = line.strip()
        if not line or line.startswith("---"):
            continue
        # Extract task ID from markdown format: - `task-id` - description
        match = re.search(r"`([^`]+)`", line)
        if match:
            task_ids.add(match.group(1))

    return task_ids


def format_related_tasks(task_ids, task_titles):
    """Format task IDs into markdown list format."""
    if not task_ids:
        return None

    lines = []
    for task_id in sorted(task_ids):
        title = task_titles.get(task_id, "")
        if title:
            lines.append(f"- `{task_id}` - {title}")
        else:
            lines.append(f"- `{task_id}`")

    if lines:
        return "\n".join(lines) + "\n\n---"
    return None


def main():
    print("🔍 Loading execution plans and tasks...")

    # Load data
    execution_plans_df = pd.read_parquet(
        DATA_DIR / "execution_plans" / "execution_plans.parquet"
    )
    tasks_df = pd.read_parquet(DATA_DIR / "tasks" / "tasks.parquet")

    print(
        f"✅ Loaded {len(execution_plans_df)} execution plans and {len(tasks_df)} tasks"
    )

    # Build execution plan lookup
    plans_by_path = {}
    plans_by_name = {}
    plans_by_id = {}

    for _, plan in execution_plans_df.iterrows():
        plan_id = plan["execution_plan_id"]
        name = str(plan.get("name", "")).lower()
        orig_path = normalize_path(plan.get("original_file_path", ""))
        name_from_path = extract_name(plan.get("original_file_path", ""))

        plans_by_path[orig_path] = plan_id
        plans_by_name[name] = plan_id
        if name_from_path:
            plans_by_name[name_from_path] = plan_id
        plans_by_id[plan_id] = plan

    # Build task lookup
    task_titles = {
        task["task_id"]: task.get("title", "") for _, task in tasks_df.iterrows()
    }

    # Match tasks to plans
    tasks_by_plan = {plan_id: set() for plan_id in plans_by_id.keys()}

    print("\n🔗 Matching tasks to execution plans...")

    for _, task in tasks_df.iterrows():
        task_id = task["task_id"]
        exec_path = task.get("execution_plan_path")

        if pd.isna(exec_path) or not exec_path:
            continue

        normalized = normalize_path(exec_path)
        name_from_path = extract_name(exec_path)

        matched_plan_id = None
        if normalized in plans_by_path:
            matched_plan_id = plans_by_path[normalized]
        elif name_from_path in plans_by_name:
            matched_plan_id = plans_by_name[name_from_path]
        else:
            # Try partial matching
            for path, pid in plans_by_path.items():
                if name_from_path and (name_from_path in path or path in normalized):
                    matched_plan_id = pid
                    break

        if matched_plan_id:
            tasks_by_plan[matched_plan_id].add(task_id)
            plan_name = plans_by_id[matched_plan_id].get("name", "")
            print(f"  ✓ Matched task '{task_id}' to plan '{plan_name}'")

    # Also parse existing related_tasks
    print("\n📋 Processing existing related_tasks...")
    for _, plan in execution_plans_df.iterrows():
        plan_id = plan["execution_plan_id"]
        existing = plan.get("related_tasks", "")
        if pd.notna(existing) and existing:
            existing_tasks = parse_related_tasks(existing)
            tasks_by_plan[plan_id].update(existing_tasks)

    # Manual keyword-based matching for tasks without execution_plan_path
    print("\n🔍 Keyword-based matching for remaining plans...")
    keyword_matches = {
        "b8af19a530633ac0": [
            "1211437797099847"
        ],  # Update Movistar Address - "Update Movistar address"
        "21a0d513bea137ac": [
            "1200297494446377"
        ],  # Movistar Line Cancellation - "Call Movistar (1004) to cancel addition line"
        "be88d3bbb6d0aba8": [
            "1211572956904017"
        ],  # Modelo 900D Filing - "Update IBI registration for San Vicente (Modelo 900D)"
        "85218d288a05347b": [
            "1198967674257965",
            "1147869978824524",
            "152839387180302",
        ],  # Projector Filter Cleaning
        "04421f974cbb4164": [],  # Verify Porsche Spare Tire Kit - no clear match found
        "9665d3c7f8aa1b00": [],  # Sgx Invoice Dispute Response - many SGX tasks but none clearly "dispute"
        "5f4d6699f5d85c23": [],  # Okx Fbar Declaration - FBAR tasks found but none OKX-specific
        "c80a64d7a02fa67b": [],  # Order Extra Emergency Vest For Car - canceled plan
        "c62d8c7171f17d7e": [
            "1170785135813834"
        ],  # Personal Website - "Create website for Dionysian Designs"
        "61c6624baa641825": [
            "1170785135813834"
        ],  # Personal Website (duplicate) - same task
        "1cdcaf7718b65979": [],  # 2025 Tax Preparation - many tax tasks but need 2025-specific
        "19161c3761f85038": [],  # Entity Structure Setup - no clear matches
        "9fb1d8360349ea59": [],  # Eoy Fixed Costs Review - no clear matches
        "0a127e3404d6561c": [],  # Repository Infrastructure Automation - no clear matches
    }

    for plan_id, task_ids in keyword_matches.items():
        if plan_id in tasks_by_plan and task_ids:
            tasks_by_plan[plan_id].update(task_ids)
            plan_name = plans_by_id[plan_id].get("name", "")
            print(f"  ✓ Matched {len(task_ids)} tasks to '{plan_name}' via keywords")

    # Update execution plans
    print("\n📝 Updating execution plans...")
    updates_count = 0

    for plan_id, task_ids in tasks_by_plan.items():
        if not task_ids:
            continue

        plan = plans_by_id[plan_id]
        plan_name = plan.get("name", "")
        current_related = plan.get("related_tasks", "")
        current_task_ids = parse_related_tasks(current_related)

        # Only update if there are changes
        if task_ids != current_task_ids:
            new_related_tasks = format_related_tasks(task_ids, task_titles)

            # Update the dataframe
            mask = execution_plans_df["execution_plan_id"] == plan_id
            execution_plans_df.loc[mask, "related_tasks"] = new_related_tasks
            execution_plans_df.loc[mask, "updated_date"] = date.today()

            updates_count += 1
            print(
                f"  ✓ Updated '{plan_name}': {len(current_task_ids)} → {len(task_ids)} tasks"
            )

    if updates_count > 0:
        print("\n💾 Saving updates to parquet file...")
        # Ensure date columns are properly formatted
        execution_plans_df["updated_date"] = pd.to_datetime(
            execution_plans_df["updated_date"], errors="coerce"
        ).dt.date
        if "created_date" in execution_plans_df.columns:
            execution_plans_df["created_date"] = pd.to_datetime(
                execution_plans_df["created_date"], errors="coerce"
            ).dt.date
        if "target_completion_date" in execution_plans_df.columns:
            execution_plans_df["target_completion_date"] = pd.to_datetime(
                execution_plans_df["target_completion_date"], errors="coerce"
            ).dt.date
        if "start_date" in execution_plans_df.columns:
            execution_plans_df["start_date"] = pd.to_datetime(
                execution_plans_df["start_date"], errors="coerce"
            ).dt.date
        if "import_date" in execution_plans_df.columns:
            execution_plans_df["import_date"] = pd.to_datetime(
                execution_plans_df["import_date"], errors="coerce"
            ).dt.date

        # Save
        execution_plans_df.to_parquet(
            DATA_DIR / "execution_plans" / "execution_plans.parquet",
            index=False,
            engine="pyarrow",
        )

        print(f"✅ Successfully updated {updates_count} execution plans!")
    else:
        print("\n✅ No updates needed - all execution plans are up to date")

    # Print summary
    print("\n📊 Summary:")
    plans_with_tasks = sum(1 for task_ids in tasks_by_plan.values() if task_ids)
    print(f"  - Execution plans: {len(execution_plans_df)}")
    print(f"  - Plans with related tasks: {plans_with_tasks}")
    print(f"  - Plans updated: {updates_count}")


if __name__ == "__main__":
    main()
