#!/usr/bin/env python3
"""
Add task(s) to tasks.parquet for remaining work from Aquarea/Home Assistant chat.

Remaining work:
1. Set up Home Assistant Aquarea integration (HACS, Panasonic ID, optionally second account)
2. Optionally: post-power-outage automation (e.g. force heating mode after power restore)

Uses DATA_DIR from config (iCloud data on macOS). Creates snapshot before write.
"""

import sys
from datetime import date, datetime
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
    from datetime import timedelta

    import pandas as pd

    today_d = date.today()
    today = today_d.isoformat()
    due_d = date.today() + timedelta(days=30)
    due_d.isoformat()
    pd.Timestamp.now(tz="UTC")

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
            "import_source_file": "chat_aquarea_ha_2026_01_26",
            "created_date": today,
            "updated_date": today,
            "sync_log": None,
            "sync_datetime": None,
            "tags": None,
        }
        base.update(kwargs)
        return base

    return [
        row(
            task_id=str(uuid4())[:16],
            title="Set up Home Assistant Aquarea integration",
            description="Install and configure home-assistant-aquarea for Panasonic Aquarea heat pump control from Home Assistant.",
            due_date=due_d,
            notes="Chat 2026-01-26: (1) Install via HACS > Integrations > Aquarea. (2) Add integration: Settings > Devices & Services > Aquarea Smart Cloud; Panasonic ID + password. (3) Optional: create second Panasonic account for HA to avoid single-session conflict (csapl.pcpf.panasonic.com, then aquarea-smart.panasonic.com; add device with CZ-TAW1 ID; accept user request from main account). Min HA 2024.2. Ref: https://github.com/cjaliaga/home-assistant-aquarea",
        ),
        row(
            task_id=str(uuid4())[:16],
            title="Aquarea post-power-outage automation (optional)",
            description="Build HA automation to force heating mode after power restore so floors recover without manual reset.",
            due_date=None,  # backlog
            notes="Chat 2026-01-26: After HA Aquarea integration is in place, consider automation: on power/HA restart or specific trigger, call service to force heating mode and raise flow target (e.g. 32–35 °C). Depends on home-assistant-aquarea entities (climate, water_heater). Ref: Airzone heating issue fix relevance analysis.",
        ),
    ]


def main() -> int:
    import pandas as pd

    TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ensure_snapshot(TASKS_PATH)

    if TASKS_PATH.exists():
        df = pd.read_parquet(TASKS_PATH)
    else:
        df = pd.DataFrame()

    new_records = build_tasks()
    new_df = pd.DataFrame(new_records)
    # Align columns: add missing columns to new_df from existing df
    for c in df.columns:
        if c not in new_df.columns:
            new_df[c] = None
    # Reorder new_df to match df if we have existing columns
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
