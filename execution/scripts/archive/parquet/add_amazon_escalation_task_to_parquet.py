#!/usr/bin/env python3
"""
Add task to tasks.parquet for Amazon undelivered package escalation (chat 2026-01-26).

Also: if purchases.parquet exists, update any records whose notes contain the order IDs
to status dispute and append report reference.

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
PURCHASES_PATH = DATA_DIR / "purchases" / "purchases.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
REPORT_PATH = (
    REPO_ROOT / "reports" / "amazon-undelivered-package-escalation-2026-01-26.md"
)

ORDER_IDS = ["171-1963417-0261102", "XXX-XXXXXXX-XXXXXXX", "404-7788712-8118730"]
CASE_ID = "A1WD1W2NTC2E3L"


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
    due_d = today_d + timedelta(
        days=14
    )  # follow-up: try order-level claim or consumer route
    now_ts = pd.Timestamp.now(tz="UTC")

    notes = (
        "Amazon.es: package marked delivered Nov, never received. "
        "Product: Help Flash IoT + Luz V16 (and other items same package). "
        "Chat + email (Nicolle, case " + CASE_ID + "): 30-day policy cited, no action. "
        "Order links: "
        "171-1963417-0261102, XXX-XXXXXXX-XXXXXXX, 404-7788712-8118730. "
        "Tracking: orderId=XXX-XXXXXXX-XXXXXXX itemId=nnmjsvppqimroo shipmentId=U6Hf1qzkq. "
        "Ref: reports/amazon-undelivered-package-escalation-2026-01-26.md"
    )

    return {
        "task_id": str(uuid4())[:16],
        "title": "Amazon undelivered package – Help Flash V16 (Nov)",
        "description": "Escalation requested; Amazon cited 30-day policy. Next: order-level Problema con el pedido or Garantía A-to-Z / consumer route (OCU, servicio consumo).",
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
        "import_source_file": "chat_amazon_escalation_2026_01_26",
        "created_date": today,
        "updated_date": today,
        "sync_log": None,
        "sync_datetime": None,
        "tags": None,
    }


def main() -> int:
    import pandas as pd

    TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ensure_snapshot(TASKS_PATH)

    if TASKS_PATH.exists():
        df = pd.read_parquet(TASKS_PATH)
    else:
        df = pd.DataFrame()

    # Idempotent: skip if task with same title already exists
    title_substring = "Amazon undelivered package"
    if not df.empty and "title" in df.columns:
        if (
            df["title"]
            .fillna("")
            .astype(str)
            .str.contains(title_substring, regex=False)
            .any()
        ):
            print(
                f"Task matching '{title_substring}' already in tasks.parquet; skipping add."
            )
        else:
            _append_task(df, TASKS_PATH, build_task())
    else:
        _append_task(df, TASKS_PATH, build_task())

    # Optionally update purchases
    _update_purchases()
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


def _update_purchases() -> None:
    import pandas as pd

    if not PURCHASES_PATH.exists():
        print(f"Purchases file not found: {PURCHASES_PATH}; skipping.")
        return
    ensure_snapshot(PURCHASES_PATH)
    pdf = pd.read_parquet(PURCHASES_PATH)
    if "notes" not in pdf.columns:
        pdf["notes"] = ""
    mask = (
        pdf["notes"]
        .fillna("")
        .astype(str)
        .str.contains("|".join(ORDER_IDS), regex=False)
    )
    if mask.any():
        ref = (
            "Dispute: reports/amazon-undelivered-package-escalation-2026-01-26.md (case "
            + CASE_ID
            + "). "
        )
        pdf.loc[mask, "notes"] = pdf.loc[mask, "notes"].fillna("").astype(str) + ref
        if "status" in pdf.columns:
            pdf.loc[mask, "status"] = "dispute"
        pdf.to_parquet(PURCHASES_PATH, index=False)
        print(
            f"Updated {int(mask.sum())} purchase(s) to dispute + report ref in {PURCHASES_PATH}"
        )
    else:
        print("No purchase records matched order IDs; purchases.parquet unchanged.")


if __name__ == "__main__":
    sys.exit(main())
