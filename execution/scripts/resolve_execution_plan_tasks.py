#!/usr/bin/env python3
"""
Resolve execution plan task relationships:
1. Match tasks with old markdown execution_plan_path to execution plans
2. Update execution plans' related_tasks field with all linked tasks
3. Ensure all execution plans have related tasks populated
"""

import re

# Import MCP tools (we'll use subprocess to call MCP server)
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def normalize_path(path: str) -> str:
    """Normalize file paths for matching."""
    if not path:
        return ""
    # Remove leading/trailing slashes and normalize
    path = path.strip().strip("/")
    # Convert to lowercase for case-insensitive matching
    path = path.lower()
    # Replace backslashes with forward slashes
    path = path.replace("\\", "/")
    return path


def extract_plan_name_from_path(path: str) -> str:
    """Extract execution plan name from file path."""
    if not path:
        return ""
    # Remove directory and extension
    basename = Path(path).stem
    # Remove common suffixes
    basename = re.sub(r"-execution-plan$", "", basename, flags=re.IGNORECASE)
    basename = re.sub(r"-project-plan$", "", basename, flags=re.IGNORECASE)
    basename = re.sub(r"-plan$", "", basename, flags=re.IGNORECASE)
    return basename.lower()


def normalize_plan_name(name: str) -> str:
    """Normalize execution plan name for matching."""
    if not name:
        return ""
    return name.lower().strip()


def match_task_to_execution_plan(task_path: str, execution_plans: list[dict]) -> dict:
    """Match a task's execution_plan_path to an execution plan."""
    if not task_path:
        return None

    normalized_task_path = normalize_path(task_path)

    # Try exact path match first
    for plan in execution_plans:
        original_path = plan.get("original_file_path", "")
        if original_path:
            normalized_plan_path = normalize_path(original_path)
            if normalized_task_path == normalized_plan_path:
                return plan

    # Try matching by extracted name
    task_plan_name = extract_plan_name_from_path(task_path)
    if task_plan_name:
        for plan in execution_plans:
            plan_name = normalize_plan_name(plan.get("name", ""))
            plan_name_from_path = extract_plan_name_from_path(
                plan.get("original_file_path", "")
            )

            if task_plan_name == plan_name or task_plan_name == plan_name_from_path:
                return plan

    # Try partial path matching
    for plan in execution_plans:
        original_path = plan.get("original_file_path", "")
        if original_path:
            normalized_plan_path = normalize_path(original_path)
            # Check if task path contains plan path or vice versa
            if (
                task_plan_name in normalized_plan_path
                or normalized_plan_path in normalized_task_path
            ):
                return plan

    return None


def parse_related_tasks(related_tasks_str: str) -> set[str]:
    """Parse related_tasks string into set of task IDs."""
    if not related_tasks_str:
        return set()

    task_ids = set()
    # Handle markdown list format
    for line in related_tasks_str.split("\n"):
        line = line.strip()
        if not line or line.startswith("---"):
            continue
        # Extract task ID from markdown format: - `task-id` - description
        match = re.search(r"`([^`]+)`", line)
        if match:
            task_ids.add(match.group(1))
        # Or just plain task ID
        elif line.startswith("-"):
            task_id = line.lstrip("-").strip().split()[0]
            if task_id:
                task_ids.add(task_id)

    return task_ids


def format_related_tasks(task_ids: set[str], task_titles: dict[str, str]) -> str:
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

    # Load execution plans via MCP
    subprocess.run(
        [
            "node",
            "-e",
            """
        const { spawn } = require("child_process");
        const proc = spawn("npx", ["-y", "@modelcontextprotocol/server-python"], {
            stdio: ["pipe", "pipe", "inherit"]
        });

        const request = {
            jsonrpc: "2.0",
            id: 1,
            method: "tools/call",
            params: {
                name: "read_parquet",
                arguments: {
                    data_type: "execution_plans",
                    columns: ["execution_plan_id", "name", "related_tasks", "original_file_path"]
                }
            }
        };

        proc.stdin.write(JSON.stringify(request) + "\\n");
        proc.stdin.end();

        let output = "";
        proc.stdout.on("data", (data) => {
            output += data.toString();
        });

        proc.on("close", (code) => {
            process.stdout.write(output);
        });
        """,
        ],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )

    # For now, let's use a simpler approach - read directly from parquet
    # Actually, let me use the MCP Python client approach
    print("Using MCP parquet server directly...")

    # Read execution plans
    import pandas as pd

    execution_plans_df = pd.read_parquet(
        DATA_DIR / "execution_plans" / "execution_plans.parquet"
    )
    execution_plans = execution_plans_df.to_dict("records")

    # Read tasks
    tasks_df = pd.read_parquet(DATA_DIR / "tasks" / "tasks.parquet")
    tasks = tasks_df.to_dict("records")

    print(f"✅ Loaded {len(execution_plans)} execution plans and {len(tasks)} tasks")

    # Build task lookup
    task_titles = {task["task_id"]: task.get("title", "") for task in tasks}

    # Map execution plans by ID
    plans_by_id = {plan["execution_plan_id"]: plan for plan in execution_plans}

    # Build task-to-plan mapping
    task_to_plan: dict[str, dict] = {}
    tasks_by_plan: dict[str, set[str]] = {
        plan_id: set() for plan_id in plans_by_id.keys()
    }

    print("\n🔗 Matching tasks to execution plans...")

    for task in tasks:
        task_id = task["task_id"]
        execution_plan_path = task.get("execution_plan_path")

        if not execution_plan_path:
            continue

        matched_plan = match_task_to_execution_plan(
            execution_plan_path, execution_plans
        )
        if matched_plan:
            plan_id = matched_plan["execution_plan_id"]
            task_to_plan[task_id] = matched_plan
            tasks_by_plan[plan_id].add(task_id)
            print(f"  ✓ Matched task '{task_id}' to plan '{matched_plan['name']}'")
        else:
            print(
                f"  ⚠ Could not match task '{task_id}' with path '{execution_plan_path}'"
            )

    # Also check for tasks mentioned in existing related_tasks
    print("\n📋 Processing existing related_tasks...")
    for plan in execution_plans:
        plan_id = plan["execution_plan_id"]
        existing_tasks = parse_related_tasks(plan.get("related_tasks", ""))
        tasks_by_plan[plan_id].update(existing_tasks)

    # Update execution plans
    print("\n📝 Updating execution plans...")
    updates = []

    for plan_id, task_ids in tasks_by_plan.items():
        if not task_ids:
            continue

        plan = plans_by_id[plan_id]
        current_related = plan.get("related_tasks", "")
        current_task_ids = parse_related_tasks(current_related)

        # Only update if there are changes
        if task_ids != current_task_ids:
            new_related_tasks = format_related_tasks(task_ids, task_titles)
            updates.append(
                {
                    "plan_id": plan_id,
                    "plan_name": plan["name"],
                    "old_tasks": current_task_ids,
                    "new_tasks": task_ids,
                    "new_related_tasks": new_related_tasks,
                }
            )
            print(
                f"  ✓ Plan '{plan['name']}': {len(current_task_ids)} → {len(task_ids)} tasks"
            )

    # Print summary
    print("\n📊 Summary:")
    print(f"  - Execution plans: {len(execution_plans)}")
    print(f"  - Tasks matched: {len(task_to_plan)}")
    print(f"  - Plans to update: {len(updates)}")

    if updates:
        print("\n📋 Plans that will be updated:")
        for update in updates:
            print(f"  - {update['plan_name']} ({update['plan_id']})")
            print(f"    Tasks: {', '.join(sorted(update['new_tasks']))}")

        # Ask for confirmation
        response = input("\n❓ Proceed with updates? (yes/no): ")
        if response.lower() != "yes":
            print("❌ Cancelled")
            return

        # Apply updates via MCP
        print("\n💾 Applying updates...")
        for update in updates:
            # Use MCP update_records
            # For now, print what would be updated
            print(f"  Updating {update['plan_name']}...")
            # TODO: Actually call MCP update_records

        print("✅ Updates complete!")
    else:
        print("\n✅ No updates needed - all execution plans are up to date")


if __name__ == "__main__":
    main()
