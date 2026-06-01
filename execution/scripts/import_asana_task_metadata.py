#!/usr/bin/env python3
"""
Import Asana task metadata: custom fields, dependencies, and all story types.

Creates snapshots before modification and handles duplicate detection.
"""

import sys
import uuid
from datetime import date, datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from scripts.config import DATA_DIR

TASKS_FILE = DATA_DIR / "tasks" / "tasks.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"

CUSTOM_FIELDS_DIR = DATA_DIR / "task_custom_fields"
CUSTOM_FIELDS_FILE = CUSTOM_FIELDS_DIR / "task_custom_fields.parquet"

DEPENDENCIES_DIR = DATA_DIR / "task_dependencies"
DEPENDENCIES_FILE = DEPENDENCIES_DIR / "task_dependencies.parquet"

STORIES_DIR = DATA_DIR / "task_stories"
STORIES_FILE = STORIES_DIR / "task_stories.parquet"

from scripts.client import AsanaClientWrapper
from scripts.import_asana_task_comments import (
    download_description_attachments,
    html_to_local_text,
    rewrite_html_with_local_attachments,
)


def _ensure_custom_fields_df() -> pd.DataFrame:
    """Load existing custom fields parquet or create empty with schema columns."""
    CUSTOM_FIELDS_DIR.mkdir(parents=True, exist_ok=True)
    if CUSTOM_FIELDS_FILE.exists():
        df = pd.read_parquet(CUSTOM_FIELDS_FILE)
        # Ensure all columns exist
        required_columns = [
            "custom_field_id",
            "task_id",
            "asana_task_gid",
            "asana_custom_field_gid",
            "asana_workspace",
            "custom_field_name",
            "custom_field_type",
            "text_value",
            "number_value",
            "enum_value",
            "enum_option_name",
            "date_value",
            "people_value_gids",
            "people_value_names",
            "multi_enum_values",
            "multi_enum_option_names",
            "created_at",
            "imported_at",
            "import_source_file",
        ]
        for col in required_columns:
            if col not in df.columns:
                df[col] = None
        return df

    columns = [
        "custom_field_id",
        "task_id",
        "asana_task_gid",
        "asana_custom_field_gid",
        "asana_workspace",
        "custom_field_name",
        "custom_field_type",
        "text_value",
        "number_value",
        "enum_value",
        "enum_option_name",
        "date_value",
        "people_value_gids",
        "people_value_names",
        "multi_enum_values",
        "multi_enum_option_names",
        "created_at",
        "imported_at",
        "import_source_file",
    ]
    return pd.DataFrame(columns=columns)


def _ensure_dependencies_df() -> pd.DataFrame:
    """Load existing dependencies parquet or create empty with schema columns."""
    DEPENDENCIES_DIR.mkdir(parents=True, exist_ok=True)
    if DEPENDENCIES_FILE.exists():
        df = pd.read_parquet(DEPENDENCIES_FILE)
        required_columns = [
            "dependency_id",
            "task_id",
            "asana_task_gid",
            "asana_workspace",
            "predecessor_task_id",
            "predecessor_asana_gid",
            "successor_task_id",
            "successor_asana_gid",
            "created_at",
            "imported_at",
            "import_source_file",
        ]
        for col in required_columns:
            if col not in df.columns:
                df[col] = None
        return df

    columns = [
        "dependency_id",
        "task_id",
        "asana_task_gid",
        "asana_workspace",
        "predecessor_task_id",
        "predecessor_asana_gid",
        "successor_task_id",
        "successor_asana_gid",
        "created_at",
        "imported_at",
        "import_source_file",
    ]
    return pd.DataFrame(columns=columns)


def _ensure_stories_df() -> pd.DataFrame:
    """Load existing stories parquet or create empty with schema columns."""
    STORIES_DIR.mkdir(parents=True, exist_ok=True)
    if STORIES_FILE.exists():
        df = pd.read_parquet(STORIES_FILE)
        required_columns = [
            "story_id",
            "task_id",
            "asana_task_gid",
            "asana_story_gid",
            "asana_workspace",
            "story_type",
            "author_name",
            "author_gid",
            "text",
            "story_html",
            "story_html_remote",
            "created_at",
            "imported_at",
            "import_source_file",
        ]
        for col in required_columns:
            if col not in df.columns:
                df[col] = None
        return df

    columns = [
        "story_id",
        "task_id",
        "asana_task_gid",
        "asana_story_gid",
        "asana_workspace",
        "story_type",
        "author_name",
        "author_gid",
        "text",
        "story_html",
        "story_html_remote",
        "created_at",
        "imported_at",
        "import_source_file",
    ]
    return pd.DataFrame(columns=columns)


def _snapshot_file(file_path: Path) -> None:
    """Create timestamped snapshot of a parquet file."""
    if not file_path.exists():
        return
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    filename = file_path.stem
    snapshot_path = SNAPSHOTS_DIR / f"{filename}-{ts}.parquet"
    pd.read_parquet(file_path).to_parquet(snapshot_path, index=False)


def import_custom_fields_for_tasks(
    client: AsanaClientWrapper,
    workspace_name: str,
    task_gids: list[str],
) -> int:
    """Import custom fields for the given Asana task GIDs.

    Returns number of new custom field values imported.
    """
    if not task_gids:
        return 0

    # Load tasks to map GIDs to task_ids
    tasks_df = pd.read_parquet(TASKS_FILE) if TASKS_FILE.exists() else pd.DataFrame()

    custom_fields_df = _ensure_custom_fields_df()
    existing_keys = set()
    if not custom_fields_df.empty:
        existing_keys = set(
            zip(
                custom_fields_df["asana_task_gid"].astype(str),
                custom_fields_df["asana_custom_field_gid"].astype(str),
            )
        )

    _snapshot_file(CUSTOM_FIELDS_FILE)

    total_new = 0
    new_rows = []

    for task_gid in task_gids:
        # Map to local task_id
        task_id_val: str | None = None
        if not tasks_df.empty and "asana_source_gid" in tasks_df.columns:
            match = tasks_df[tasks_df["asana_source_gid"].astype(str) == str(task_gid)]
            if not match.empty:
                task_id_val = str(match.iloc[0]["task_id"])

        # Fetch task with custom fields (already fetched in main import, but we'll use the data)
        # For now, we'll need to fetch again or pass the data
        # Actually, custom fields should already be in task_data from the main import
        # So we'll need to modify the import flow to pass task_data here
        # For now, let's fetch it
        try:
            opts = {
                "opt_fields": "custom_fields,custom_fields.gid,custom_fields.name,custom_fields.type,custom_fields.text_value,custom_fields.number_value,custom_fields.enum_value,custom_fields.enum_value.name,custom_fields.date_value,custom_fields.people_value,custom_fields.people_value.gid,custom_fields.people_value.name,custom_fields.multi_enum_values,custom_fields.multi_enum_values.name"
            }
            task_data = client._with_retry(client.tasks.get_task, task_gid, opts)
        except Exception:
            continue

        custom_fields = task_data.get("custom_fields", []) or []
        for cf in custom_fields:
            cf_gid = str(cf.get("gid") or "")
            if not cf_gid:
                continue

            key = (str(task_gid), cf_gid)
            if key in existing_keys:
                continue

            cf_type = cf.get("type", "")
            cf_name = cf.get("name", "")

            # Extract value based on type
            text_value = cf.get("text_value")
            number_value = cf.get("number_value")
            enum_value = cf.get("enum_value")
            enum_option_name = None
            if enum_value:
                enum_option_name = enum_value.get("name")
                enum_value = enum_value.get("gid") or str(enum_value)
            date_value = cf.get("date_value")
            people_value = cf.get("people_value", []) or []
            people_gids = [p.get("gid") for p in people_value if p.get("gid")]
            people_names = [p.get("name") for p in people_value if p.get("name")]
            multi_enum_values = cf.get("multi_enum_values", []) or []
            multi_enum_gids = [v.get("gid") for v in multi_enum_values if v.get("gid")]
            multi_enum_names = [
                v.get("name") for v in multi_enum_values if v.get("name")
            ]

            created_at_raw = cf.get("created_at")
            created_at_ts = None
            if created_at_raw:
                try:
                    created_at_ts = datetime.fromisoformat(
                        created_at_raw.replace("Z", "+00:00")
                    )
                except Exception:
                    created_at_ts = None

            new_rows.append(
                {
                    "custom_field_id": str(uuid.uuid4())[:16],
                    "task_id": task_id_val or task_gid,
                    "asana_task_gid": str(task_gid),
                    "asana_custom_field_gid": cf_gid,
                    "asana_workspace": workspace_name,
                    "custom_field_name": cf_name,
                    "custom_field_type": cf_type,
                    "text_value": text_value,
                    "number_value": (
                        float(number_value) if number_value is not None else None
                    ),
                    "enum_value": enum_value if enum_value else None,
                    "enum_option_name": enum_option_name,
                    "date_value": (
                        datetime.fromisoformat(date_value).date()
                        if date_value
                        else None
                    ),
                    "people_value_gids": "|".join(people_gids) if people_gids else None,
                    "people_value_names": (
                        "|".join(people_names) if people_names else None
                    ),
                    "multi_enum_values": (
                        "|".join(multi_enum_gids) if multi_enum_gids else None
                    ),
                    "multi_enum_option_names": (
                        "|".join(multi_enum_names) if multi_enum_names else None
                    ),
                    "created_at": created_at_ts,
                    "imported_at": date.today(),
                    "import_source_file": f"asana_custom_fields_{workspace_name}",
                }
            )
            total_new += 1

    if new_rows:
        custom_fields_df = pd.concat(
            [custom_fields_df, pd.DataFrame(new_rows)], ignore_index=True
        )
        custom_fields_df.to_parquet(CUSTOM_FIELDS_FILE, index=False)

    return total_new


def import_dependencies_for_tasks(
    client: AsanaClientWrapper,
    workspace_name: str,
    task_gids: list[str],
) -> int:
    """Import dependencies for the given Asana task GIDs.

    Returns number of new dependencies imported.
    """
    if not task_gids:
        return 0

    # Load tasks to map GIDs to task_ids
    tasks_df = pd.read_parquet(TASKS_FILE) if TASKS_FILE.exists() else pd.DataFrame()

    dependencies_df = _ensure_dependencies_df()
    existing_keys = set()
    if not dependencies_df.empty:
        existing_keys = set(
            zip(
                dependencies_df["asana_task_gid"].astype(str),
                dependencies_df["predecessor_asana_gid"].astype(str),
                dependencies_df["successor_asana_gid"].astype(str),
            )
        )

    _snapshot_file(DEPENDENCIES_FILE)

    total_new = 0
    new_rows = []

    for task_gid in task_gids:
        # Map to local task_id
        task_id_val: str | None = None
        if not tasks_df.empty and "asana_source_gid" in tasks_df.columns:
            match = tasks_df[tasks_df["asana_source_gid"].astype(str) == str(task_gid)]
            if not match.empty:
                task_id_val = str(match.iloc[0]["task_id"])

        # Fetch task with dependencies
        try:
            opts = {
                "opt_fields": "dependencies,dependencies.predecessor.gid,dependencies.successor.gid"
            }
            task_data = client._with_retry(client.tasks.get_task, task_gid, opts)
        except Exception:
            continue

        dependencies = task_data.get("dependencies", []) or []
        for dep in dependencies:
            predecessor = dep.get("predecessor", {})
            successor = dep.get("successor", {})
            pred_gid = str(predecessor.get("gid") or "")
            succ_gid = str(successor.get("gid") or "")

            if not pred_gid or not succ_gid:
                continue

            # Dependencies are stored from the successor's perspective
            # So task_gid should be the successor
            if str(task_gid) != succ_gid:
                continue

            key = (succ_gid, pred_gid, succ_gid)
            if key in existing_keys:
                continue

            # Map predecessor and successor to local task_ids
            pred_task_id = None
            if not tasks_df.empty and "asana_source_gid" in tasks_df.columns:
                pred_match = tasks_df[
                    tasks_df["asana_source_gid"].astype(str) == pred_gid
                ]
                if not pred_match.empty:
                    pred_task_id = str(pred_match.iloc[0]["task_id"])

            succ_task_id = task_id_val or succ_gid

            new_rows.append(
                {
                    "dependency_id": str(uuid.uuid4())[:16],
                    "task_id": succ_task_id,
                    "asana_task_gid": succ_gid,
                    "asana_workspace": workspace_name,
                    "predecessor_task_id": pred_task_id or pred_gid,
                    "predecessor_asana_gid": pred_gid,
                    "successor_task_id": succ_task_id,
                    "successor_asana_gid": succ_gid,
                    "created_at": datetime.now(),
                    "imported_at": date.today(),
                    "import_source_file": f"asana_dependencies_{workspace_name}",
                }
            )
            total_new += 1

    if new_rows:
        dependencies_df = pd.concat(
            [dependencies_df, pd.DataFrame(new_rows)], ignore_index=True
        )
        dependencies_df.to_parquet(DEPENDENCIES_FILE, index=False)

    return total_new


def import_stories_for_tasks(
    client: AsanaClientWrapper,
    workspace_name: str,
    task_gids: list[str],
) -> int:
    """Import all stories (not just comments) for the given Asana task GIDs.

    Returns number of new stories imported.
    """
    if not task_gids:
        return 0

    # Load tasks to map GIDs to task_ids
    tasks_df = pd.read_parquet(TASKS_FILE) if TASKS_FILE.exists() else pd.DataFrame()

    stories_df = _ensure_stories_df()
    (
        set(stories_df["asana_story_gid"].astype(str).tolist())
        if not stories_df.empty and "asana_story_gid" in stories_df.columns
        else set()
    )

    _snapshot_file(STORIES_FILE)

    total_new = 0
    updated_count = 0
    new_rows = []

    for task_gid in task_gids:
        # Map to local task_id
        task_id_val: str | None = None
        if not tasks_df.empty and "asana_source_gid" in tasks_df.columns:
            match = tasks_df[tasks_df["asana_source_gid"].astype(str) == str(task_gid)]
            if not match.empty:
                task_id_val = str(match.iloc[0]["task_id"])

        # Fetch all stories (not just comments)
        try:
            opts = {
                "opt_fields": "gid,type,text,html_text,created_at,created_by,created_by.name,created_by.gid"
            }
            stories_resp = client._with_retry(
                client.stories.get_stories_for_task,
                task_gid,
                opts,
            )
            stories = list(stories_resp) if stories_resp else []
        except Exception:
            continue

        # Download attachments for comment-type stories (for HTML rewriting)
        attachment_map: dict[str, str] = {}
        comment_stories = [s for s in stories if s.get("type") == "comment"]
        for story in comment_stories:
            story_html = story.get("html_text")
            # Reuse attachment download logic from comments import
            if story_html:
                try:
                    story_attachment_map = download_description_attachments(
                        client, task_gid, story_html
                    )
                    attachment_map.update(story_attachment_map)
                except Exception:
                    pass  # Skip if attachment download fails

        for story in stories:
            story_gid = str(story.get("gid") or "")
            if not story_gid:
                continue

            story_type = story.get("type", "")
            story_text = story.get("text") or ""
            story_html_remote = story.get("html_text")

            # Rewrite HTML for comment-type stories only
            story_html = None
            if story_type == "comment" and story_html_remote:
                story_html = rewrite_html_with_local_attachments(
                    story_html_remote, attachment_map
                )
            else:
                story_html = story_html_remote

            # Derive text from HTML if available
            story_text_local = story_text
            if story_html:
                story_text_local = html_to_local_text(story_html) or story_text

            created_by = story.get("created_by") or {}
            created_at_raw = story.get("created_at")
            created_at_ts = None
            if created_at_raw:
                try:
                    created_at_ts = datetime.fromisoformat(
                        created_at_raw.replace("Z", "+00:00")
                    )
                except Exception:
                    created_at_ts = None

            row_data = {
                "story_id": str(uuid.uuid4())[:16],
                "task_id": task_id_val or task_gid,
                "asana_task_gid": str(task_gid),
                "asana_story_gid": story_gid,
                "asana_workspace": workspace_name,
                "story_type": story_type,
                "author_name": created_by.get("name"),
                "author_gid": created_by.get("gid"),
                "text": story_text_local,
                "story_html": story_html,
                "story_html_remote": story_html_remote,
                "created_at": created_at_ts,
                "imported_at": date.today(),
                "import_source_file": f"asana_stories_{workspace_name}",
            }

            # Check if story exists and update if necessary
            existing_story_idx = stories_df[
                stories_df["asana_story_gid"] == story_gid
            ].index

            if not existing_story_idx.empty:
                # Update existing story
                idx = existing_story_idx[0]
                for key, value in row_data.items():
                    stories_df.loc[idx, key] = value
                updated_count += 1
            else:
                # Add new story
                new_rows.append(row_data)
                total_new += 1

    if new_rows:
        stories_df = pd.concat([stories_df, pd.DataFrame(new_rows)], ignore_index=True)

    if total_new > 0 or updated_count > 0:
        stories_df.to_parquet(STORIES_FILE, index=False)

    return total_new
