#!/usr/bin/env python3
"""
Add task(s) to tasks.parquet: complete Plaid Production Request form and follow up.

Context: Plaid risk team required full resubmission with more specific product details
per product (email thread "API Key Follow Up" with Ankita Bhat, Jan 2026).
Dashboard was reset; resubmit via Plaid dashboard with detailed info per product.

Uses DATA_DIR from config. Creates snapshot before write.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "execution" / "scripts"))

from config import get_data_dir

DATA_DIR = get_data_dir()
TASKS_PATH = DATA_DIR / "tasks" / "tasks.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"


def ensure_snapshot(path: Path) -> None:
    if not path.exists():
        return
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    stem = path.stem
    snap = SNAPSHOTS_DIR / f"{stem}-{ts}.parquet"
    import shutil

    shutil.copy2(path, snap)


def build_tasks() -> list[dict]:
    import pandas as pd

    today_d = date.today()
    today = today_d.isoformat()
    due_d = date.today() + timedelta(days=14)
    due_d.isoformat()

    def row(**kwargs) -> dict:
        base = {
            "task_id": str(uuid4())[:16],
            "title": "",
            "description": None,
            "description_html": None,
            "description_html_remote": None,
            "domain": "Admin",
            "status": "pending",
            "due_date": None,
            "start_date": None,
            "completed_date": None,
            "recurrence": None,
            "execution_plan_path": None,
            "notes": None,
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
            "created_at": pd.Timestamp.now(tz="UTC"),
            "updated_at": pd.Timestamp.now(tz="UTC"),
            "asana_workspace": None,
            "asana_source_gid": None,
            "asana_target_gid": None,
            "parent_task_id": None,
            "permalink_url": None,
            "followers_gids": None,
            "follower_names": None,
            "import_date": today_d,
            "import_source_file": "chat_plaid_form_followup_2026_01",
            "created_date": today,
            "updated_date": today,
            "sync_log": None,
            "sync_datetime": None,
            "tags": None,
        }
        base.update(kwargs)
        return base

    form_task_id = str(uuid4())[:16]
    return [
        row(
            task_id=form_task_id,
            title="Complete Plaid Production Request form",
            description="Resubmit Production Request on Plaid dashboard with more specific details for each product. Risk team noted product request details were unclear and identical across items; include per-product specifics (account metadata, identity verification, transaction pulls, Neotoma use case). Dashboard has been reset.",
            due_date=due_d,
            notes="Plaid API Key Follow Up thread (Ankita Bhat, Jan 2026). Full resubmission required. Client ID: 6912c0ea02e586001cc84152.",
        ),
        row(
            task_id=str(uuid4())[:16],
            parent_task_id=form_task_id,
            title="Follow up with Plaid (Ankita) on API key",
            description="After resubmitting the Production Request, email Ankita (abhat@plaid.com) to confirm receipt and ask for timeline or any further details needed.",
            due_date=due_d,
            notes="Plaid API Key Follow Up thread. Follow up once form is submitted.",
        ),
    ]


def link_existing_plaid_tasks() -> int:
    """Set parent_task_id on the follow-up task to the form task (for already-inserted rows)."""
    import pandas as pd

    if not TASKS_PATH.exists():
        print(f"No tasks file at {TASKS_PATH}", file=sys.stderr)
        return 1
    ensure_snapshot(TASKS_PATH)
    df = pd.read_parquet(TASKS_PATH)
    form_title = "Complete Plaid Production Request form"
    followup_title = "Follow up with Plaid (Ankita) on API key"
    source = "chat_plaid_form_followup_2026_01"
    mask_form = (df["title"] == form_title) & (df["import_source_file"] == source)
    mask_followup = (df["title"] == followup_title) & (
        df["import_source_file"] == source
    )
    if mask_form.sum() != 1 or mask_followup.sum() != 1:
        print(
            "Expected exactly one form task and one follow-up task from this script.",
            file=sys.stderr,
        )
        return 1
    form_task_id = df.loc[mask_form, "task_id"].iloc[0]
    df.loc[mask_followup, "parent_task_id"] = form_task_id
    df.to_parquet(TASKS_PATH, index=False)
    print(f"Linked follow-up task to form task_id={form_task_id} in {TASKS_PATH}")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--link-existing",
        action="store_true",
        help="Link already-inserted Plaid tasks (set parent_task_id on follow-up)",
    )
    args = parser.parse_args()
    if args.link_existing:
        return link_existing_plaid_tasks()

    import pandas as pd

    TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ensure_snapshot(TASKS_PATH)

    if TASKS_PATH.exists():
        df = pd.read_parquet(TASKS_PATH)
    else:
        df = pd.DataFrame()

    new_records = build_tasks()
    new_df = pd.DataFrame(new_records)
    for c in df.columns:
        if c not in new_df.columns:
            new_df[c] = None
    if not df.empty:
        new_df = new_df.reindex(columns=df.columns, fill_value=None)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat([df, new_df], ignore_index=True)
    df.to_parquet(TASKS_PATH, index=False)
    print(f"Added {len(new_records)} task(s) to {TASKS_PATH}")
    for t in new_records:
        print(f"  - {t['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
