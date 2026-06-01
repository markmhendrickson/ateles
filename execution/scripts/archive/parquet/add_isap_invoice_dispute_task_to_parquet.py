#!/usr/bin/env python3
"""
Add task to tasks.parquet for ISAP invoice 342/25 dispute (Maite Ballesteros).

DISPUTE DATA SOURCE OF TRUTH: Neotoma (dispute ent_2d1f1a5cb08c8b3c02dfc51e,
task ent_964f957d50dce669e5234294, contact ent_bdbf4402806a61ad58e52755).
This script is legacy: it wrote to Parquet for one-time sync; do not use for
canonical dispute/task data.

Resolve dispute: duplicate charges with Grupo Kiak, disputed labor (Tasks 5 & 6),
faulty Zigbee driver credit, underdocumented invoice.

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

TITLE_SUBSTRING = "ISAP invoice 342/25 dispute"
DOCS = (
    "All dispute data in Neotoma only. Dispute ent_2d1f1a5cb08c8b3c02dfc51e; "
    "index ent_97af1bd8b6cd6b2d9b47a01b. Legacy ID: 59b5a57d-f85e-41. Contact: Maite Ballesteros (ISAP)."
)


def ensure_snapshot(path: Path) -> None:
    if not path.exists():
        return
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    stem = path.stem
    snap = SNAPSHOTS_DIR / f"{stem}-{ts}.parquet"
    import shutil

    shutil.copy2(path, snap)


def build_task() -> dict:
    import pandas as pd

    today_d = date.today()
    today = today_d.isoformat()
    due_d = today_d + timedelta(days=7)  # follow-up: respond to Maite / negotiate
    now_ts = pd.Timestamp.now(tz="UTC")

    notes = (
        "ISAP invoice 342/25 (675,99 €) disputed Dec 2025. "
        "Key issues: (1) Duplicate charges with Grupo Kiak for same lamp work 18/11 (771,56 € combined). "
        "(2) Task 5: 60 € – Zigbee programming attempt, ISAP wrong model, not billable. "
        "(3) Task 6: 30 € – switch replacement, damage by G.Kiak, not billable. "
        "(4) Faulty Zigbee driver – credit if in 278/25 prepayment. "
        "(5) Underdocumented invoice. Ref: " + DOCS
    )

    return {
        "task_id": str(uuid4())[:16],
        "title": "Resolve ISAP invoice 342/25 dispute with Maite",
        "description": (
            "Negotiate resolution of disputed invoice 342/25: duplicate charges with Grupo Kiak, "
            "disputed labor (Tasks 5 & 6 = 90 €), faulty Zigbee driver credit, request proper documentation."
        ),
        "description_html": None,
        "description_html_remote": None,
        "domain": "Admin",
        "status": "pending",
        "due_date": due_d,
        "start_date": None,
        "completed_date": None,
        "recurrence": None,
        "execution_plan_path": None,
        "notes": notes,
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
        "created_at": now_ts,
        "updated_at": now_ts,
        "asana_workspace": None,
        "asana_source_gid": None,
        "asana_target_gid": None,
        "parent_task_id": None,
        "permalink_url": None,
        "followers_gids": None,
        "follower_names": None,
        "import_date": today_d,
        "import_source_file": "isap_invoice_dispute_task_2026",
        "created_date": today,
        "updated_date": today,
        "sync_log": None,
        "sync_datetime": None,
        "tags": None,
    }


def main() -> int:
    import pandas as pd

    today_d = date.today()

    TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ensure_snapshot(TASKS_PATH)

    if TASKS_PATH.exists():
        df = pd.read_parquet(TASKS_PATH)
    else:
        df = pd.DataFrame()

    if not df.empty and "title" in df.columns:
        mask = (
            df["title"]
            .fillna("")
            .astype(str)
            .str.contains(TITLE_SUBSTRING, regex=False)
        )
        if mask.any():
            due_d = today_d + timedelta(days=7)
            df.loc[mask, "due_date"] = due_d
            if "updated_date" in df.columns:
                df.loc[mask, "updated_date"] = today_d.isoformat()
            if "updated_at" in df.columns:
                df.loc[mask, "updated_at"] = pd.Timestamp.now(tz="UTC")
            df.to_parquet(TASKS_PATH, index=False)
            print(
                f"Updated due_date to 7 days from now ({due_d}) for task matching '{TITLE_SUBSTRING}'."
            )
            return 0

    _append_task(df, TASKS_PATH, build_task())
    return 0


def _append_task(df: "pd.DataFrame", path: Path, new_record: dict) -> None:
    import warnings

    import pandas as pd

    new_df = pd.DataFrame([new_record])
    for c in df.columns:
        if c not in new_df.columns:
            new_df[c] = None
    if not df.empty:
        new_df = new_df.reindex(columns=df.columns, fill_value=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat([df, new_df], ignore_index=True)
    df.to_parquet(path, index=False)
    print(f"Added 1 task to {path}: {new_record['title']}")


if __name__ == "__main__":
    sys.exit(main())
