#!/usr/bin/env python3
"""
Update task for emergency flood lights check for David.
"""

import sys
import uuid
from datetime import date, datetime
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("Error: pandas required. Install with: pip install pandas pyarrow")
    sys.exit(1)

tasks_file = Path("data/tasks/tasks.parquet")
if not tasks_file.exists():
    print(f"Tasks file not found: {tasks_file}")
    sys.exit(1)

# Create snapshot before modification
snapshot_dir = Path("data/snapshots")
snapshot_dir.mkdir(exist_ok=True)
timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
df_original = pd.read_parquet(tasks_file)
snapshot_path = snapshot_dir / f"tasks-{timestamp}.parquet"
df_original.to_parquet(snapshot_path, index=False)
print(f"Created snapshot: {snapshot_path}")

# Read tasks
df = pd.read_parquet(tasks_file)

# Search for existing task
search_terms = ["emergency", "David", "potency", "flood", "light"]
mask = df["title"].str.contains("|".join(search_terms), case=False, na=False) | (
    df["notes"].str.contains("|".join(search_terms), case=False, na=False)
    if "notes" in df.columns
    else False
)

existing_tasks = df[mask]

if len(existing_tasks) > 0:
    # Update existing task
    task_idx = existing_tasks.index[0]
    task_id = df.loc[task_idx, "task_id"]
    print(f"Found existing task: {task_id}")
    print(f"Current status: {df.loc[task_idx, 'status']}")

    # Update task
    df.loc[task_idx, "status"] = "in_progress"
    current_notes = (
        df.loc[task_idx, "notes"] if pd.notna(df.loc[task_idx, "notes"]) else ""
    )
    df.loc[task_idx, "notes"] = f"""{current_notes}

**Dec 23, 2025 Update:**
- Searched Gmail archive for Legrand/Netatmo installation documentation
- Reviewed certification PDFs (Cert 17-20) - no back façade emergency flood light specs found
- Searched pre-2021 and post-2021 emails with Ana Bragatti and Ricard Fayos - no documentation found
- Email sent to Ana Bragatti (CC: Ricard Fayos) requesting device specifications and general documentation
- Summary document updated: operations/admin/legrand-netatmo-installation-barcelona-summary.md
- Next step: Await response from Ana Bragatti"""
    # Preserve timezone if column has it, otherwise use naive timestamp
    if df["updated_at"].dtype.tz is not None:
        df.loc[task_idx, "updated_at"] = pd.Timestamp.now(tz=df["updated_at"].dtype.tz)
    else:
        df.loc[task_idx, "updated_at"] = pd.Timestamp.now()

    print(f"Updated task {task_id}")
else:
    # Create new task
    print("No existing task found. Creating new task...")
    task_id = str(uuid.uuid4())[:16]

    new_task = {
        "task_id": task_id,
        "title": "Check potency of emergency flood lights for David",
        "description": "Verify potency (illumination levels) of two emergency flood lights on back façade of Passatge d'Alió, 18. Different from stairwell emergency lights.",
        "description_html": None,
        "description_html_remote": None,
        "domain": "admin",
        "status": "in_progress",
        "due_date": None,
        "start_date": date.today(),
        "completed_date": None,
        "recurrence": None,
        "execution_plan_path": None,
        "notes": """**Dec 23, 2025:**
- Searched Gmail archive for Legrand/Netatmo installation documentation
- Reviewed certification PDFs (Cert 17-20) - no back façade emergency flood light specs found
- Searched pre-2021 and post-2021 emails with Ana Bragatti and Ricard Fayos - no documentation found
- Email sent to Ana Bragatti (CC: Ricard Fayos) requesting device specifications and general documentation (Message ID: 19b4b9d620ef9407)
- Summary document: operations/admin/legrand-netatmo-installation-barcelona-summary.md

**Next Steps:**
- Await response from Ana Bragatti
- Verify current operational status of both emergency lights
- Test battery backup functionality
- Measure illumination levels (potency)
- Check for visible damage or wear
- Check physical installation for visible model numbers""",
        "project_ids": None,
        "project_names": None,
        "outcome_ids": None,
        "outcome_names": None,
        "section_ids": None,
        "section_names": None,
        "my_tasks_section_ids": None,
        "my_tasks_section_names": None,
        "assignee_gid": None,
        "assignee_name": None,
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
        "asana_workspace": None,
        "asana_source_gid": None,
        "asana_target_gid": None,
        "parent_task_id": None,
        "permalink_url": None,
        "followers_gids": None,
        "follower_names": None,
        "import_date": date.today(),
        "import_source_file": "manual_entry",
    }

    # Ensure all columns exist
    for col in df.columns:
        if col not in new_task:
            new_task[col] = None

    # Add new task
    df = pd.concat([df, pd.DataFrame([new_task])], ignore_index=True)
    print(f"Created new task: {task_id}")

# Write updated tasks
df.to_parquet(tasks_file, index=False)
print(f"Updated tasks file: {tasks_file}")
