#!/usr/bin/env python3
"""
Import Asana task comments (stories of type 'comment') into task_comments.parquet.

Designed for one-off/backfill runs or targeted imports by task GID.
Creates snapshots of task_comments.parquet before modification.
"""

import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from scripts.config import DATA_DIR

TASKS_FILE = DATA_DIR / "tasks" / "tasks.parquet"
COMMENTS_DIR = DATA_DIR / "task_comments"
COMMENTS_FILE = COMMENTS_DIR / "task_comments.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
ATTACHMENTS_BASE_DIR = DATA_DIR / "attachments" / "asana_tasks"
ATTACHMENTS_TABLE_DIR = DATA_DIR / "task_attachments"
ATTACHMENTS_TABLE_FILE = ATTACHMENTS_TABLE_DIR / "task_attachments.parquet"

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig


def _download_single_attachment(download_url: str, target_path: Path) -> str | None:
    """Download a single attachment file. Returns content_type on success, None on failure."""
    try:
        resp = requests.get(download_url, timeout=120, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in (content_type or "").lower():
            return None  # Skip HTML/login responses

        with open(target_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return content_type
    except Exception:
        return None


def _ensure_comments_df() -> pd.DataFrame:
    """Load existing comments parquet or create empty with schema columns."""
    COMMENTS_DIR.mkdir(parents=True, exist_ok=True)
    if COMMENTS_FILE.exists():
        return pd.read_parquet(COMMENTS_FILE)

    # Create empty frame with expected columns
    columns = [
        "comment_id",
        "task_id",
        "asana_task_gid",
        "asana_story_gid",
        "asana_workspace",
        "author_name",
        "author_gid",
        "text",
        "comment_html",
        "comment_html_remote",
        "created_at",
        "imported_at",
        "import_source_file",
    ]
    return pd.DataFrame(columns=columns)


def _ensure_attachments_df() -> pd.DataFrame:
    """Load existing task_attachments parquet or create empty with schema columns."""
    ATTACHMENTS_TABLE_DIR.mkdir(parents=True, exist_ok=True)
    if ATTACHMENTS_TABLE_FILE.exists():
        return pd.read_parquet(ATTACHMENTS_TABLE_FILE)

    columns = [
        "attachment_id",
        "task_id",
        "asana_task_gid",
        "asana_attachment_gid",
        "asana_workspace",
        "name",
        "resource_subtype",
        "content_type",
        "size_bytes",
        "local_path",
        "download_url",
        "created_at",
        "imported_at",
        "import_source_file",
    ]
    return pd.DataFrame(columns=columns)


def _snapshot_comments(df: pd.DataFrame) -> None:
    """Create timestamped snapshot of task_comments.parquet."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    snapshot_path = SNAPSHOTS_DIR / f"task_comments-{ts}.parquet"
    df.to_parquet(snapshot_path, index=False)


def _snapshot_attachments(df: pd.DataFrame) -> None:
    """Create timestamped snapshot of task_attachments.parquet."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    snapshot_path = SNAPSHOTS_DIR / f"task_attachments-{ts}.parquet"
    df.to_parquet(snapshot_path, index=False)


def _download_comment_attachments(
    client: AsanaClientWrapper,
    task_gid: str,
    comment_text: str | None,
    comment_html: str | None = None,
) -> dict:
    """
    Download attachments referenced in comment text/HTML.

    Extracts attachment GIDs from:
    - asset_id= in URLs (from plain text)
    - data-asana-gid= in HTML attributes

    Returns: {attachment_gid: absolute_path_to_local_file}
    """
    downloaded: dict[str, str] = {}  # {gid: local_path}
    ATTACHMENTS_BASE_DIR.mkdir(parents=True, exist_ok=True)
    task_dir = ATTACHMENTS_BASE_DIR / str(task_gid) / "comments"
    task_dir.mkdir(parents=True, exist_ok=True)

    # Extract GIDs from text URLs
    asset_ids = set()
    if comment_text:
        asset_ids.update(re.findall(r"asset_id=([0-9]+)", comment_text))

    # Extract GIDs from HTML
    if comment_html:
        asset_ids.update(re.findall(r'data-asana-gid="([^"]+)"', comment_html))

    if not asset_ids:
        return {}

    for asset_id in asset_ids:
        try:
            # Resolve attachment to get direct download_url and name
            att = client._with_retry(
                client.attachments.get_attachment,
                asset_id,
                {"opt_fields": "gid,name,download_url"},
            )
        except Exception:
            continue

        att_gid = att.get("gid") or asset_id
        download_url = att.get("download_url")
        name = att.get("name") or str(att_gid)
        if not download_url:
            continue

        target_path = task_dir / name
        if target_path.exists() and target_path.stat().st_size > 0:
            downloaded[str(att_gid)] = str(target_path)
            continue

        try:
            resp = requests.get(download_url, timeout=120, stream=True)
            resp.raise_for_status()

            # Skip if this is clearly an HTML/login response
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type.lower():
                continue

            with open(target_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            downloaded[str(att_gid)] = str(target_path)
        except Exception:
            continue

    return downloaded


def import_attachments_for_tasks(
    client: AsanaClientWrapper,
    workspace_name: str,
    task_gids: list[str],
) -> int:
    """
    Import all file attachments for the given Asana task GIDs into task_attachments.parquet.

    This covers attachments that may not be referenced in description_html or comment_html.
    """
    if not task_gids:
        return 0

    # Load local tasks to map Asana GIDs → task_id
    if TASKS_FILE.exists():
        tasks_df = pd.read_parquet(TASKS_FILE)
    else:
        tasks_df = pd.DataFrame()

    attachments_df = _ensure_attachments_df()
    if not attachments_df.empty and "asana_attachment_gid" in attachments_df.columns:
        existing_attachment_ids = set(
            attachments_df["asana_attachment_gid"].astype(str).tolist()
        )
    else:
        existing_attachment_ids = set()

    # Snapshot before modifications
    _snapshot_attachments(attachments_df)

    total_new = 0

    for task_gid in task_gids:
        # Map to local task_id if present
        task_id_val: str | None = None
        if not tasks_df.empty and "asana_source_gid" in tasks_df.columns:
            match = tasks_df[tasks_df["asana_source_gid"].astype(str) == str(task_gid)]
            if not match.empty:
                task_id_val = str(match.iloc[0]["task_id"])

        ATTACHMENTS_BASE_DIR.mkdir(parents=True, exist_ok=True)
        task_dir = ATTACHMENTS_BASE_DIR / str(task_gid) / "attachments"
        task_dir.mkdir(parents=True, exist_ok=True)

        try:
            asana_attachments = client._with_retry(
                client.attachments.get_attachments_for_object,
                task_gid,
                {
                    "opt_fields": "gid,name,resource_subtype,download_url,created_at,host,view_url,size"
                },
            )
        except Exception:
            continue

        # Collect attachments to download in batch
        attachments_to_download = []
        new_rows = []
        for att in asana_attachments or []:
            att_gid = str(att.get("gid") or "")
            if not att_gid:
                continue
            if att_gid in existing_attachment_ids:
                continue

            name = att.get("name") or att_gid
            download_url = att.get("download_url")
            content_type = None
            size_bytes = att.get("size")
            created_at_raw = att.get("created_at")
            created_at_ts = None
            if created_at_raw:
                try:
                    created_at_ts = datetime.fromisoformat(
                        created_at_raw.replace("Z", "+00:00")
                    )
                except Exception:
                    created_at_ts = None

            target_path = task_dir / name

            # Queue for batch download if needed
            needs_download = False
            if download_url and (
                not target_path.exists() or target_path.stat().st_size == 0
            ):
                attachments_to_download.append((download_url, target_path, att_gid))
                needs_download = True
            elif target_path.exists() and target_path.stat().st_size > 0:
                # File already exists
                content_type = ""

            local_path = None
            if target_path.exists() and target_path.stat().st_size > 0:
                try:
                    local_path = str(target_path.relative_to(PROJECT_ROOT))
                except Exception:
                    local_path = str(target_path)

            row = {
                "attachment_id": str(uuid.uuid4())[:16],
                "task_id": task_id_val,
                "asana_task_gid": str(task_gid),
                "asana_attachment_gid": att_gid,
                "asana_workspace": workspace_name,
                "name": name,
                "resource_subtype": att.get("resource_subtype"),
                "content_type": content_type,
                "size_bytes": size_bytes,
                "local_path": local_path,
                "download_url": download_url,
                "created_at": created_at_ts,
                "imported_at": date.today(),
                "import_source_file": f"asana_attachments_{workspace_name}",
            }
            new_rows.append((row, att_gid, target_path, needs_download))

        # Batch download attachments using ThreadPoolExecutor
        if attachments_to_download:
            download_results = {}  # {att_gid: content_type}
            executor = ThreadPoolExecutor(max_workers=5)
            try:
                future_to_att = {
                    executor.submit(_download_single_attachment, url, path): (
                        path,
                        att_gid,
                    )
                    for url, path, att_gid in attachments_to_download
                }
                for future in as_completed(future_to_att):
                    path, att_gid = future_to_att[future]
                    try:
                        content_type = future.result()
                        if content_type:
                            download_results[att_gid] = content_type
                            # Update local_path
                            if path.exists() and path.stat().st_size > 0:
                                try:
                                    local_path = str(path.relative_to(PROJECT_ROOT))
                                except Exception:
                                    local_path = str(path)
                            else:
                                local_path = None
                    except Exception:
                        pass
            finally:
                # Explicitly shutdown executor to prevent semaphore leaks
                executor.shutdown(wait=True, cancel_futures=False)

            # Update rows with download results
            final_rows = []
            for row, att_gid, target_path, needs_download in new_rows:
                if needs_download and att_gid in download_results:
                    row["content_type"] = download_results[att_gid]
                    if target_path.exists() and target_path.stat().st_size > 0:
                        try:
                            row["local_path"] = str(
                                target_path.relative_to(PROJECT_ROOT)
                            )
                        except Exception:
                            row["local_path"] = str(target_path)
                final_rows.append(row)
            new_rows = final_rows
        else:
            # No downloads needed, extract rows
            new_rows = [row for row, _, _, _ in new_rows]

        if new_rows:
            total_new += len(new_rows)
            attachments_df = pd.concat(
                [attachments_df, pd.DataFrame(new_rows)], ignore_index=True
            )
            existing_attachment_ids.update(
                r["asana_attachment_gid"] for r in new_rows if r["asana_attachment_gid"]
            )

    if total_new > 0:
        ATTACHMENTS_TABLE_DIR.mkdir(parents=True, exist_ok=True)
        attachments_df.to_parquet(ATTACHMENTS_TABLE_FILE, index=False)

    return total_new


def download_description_attachments(
    client: AsanaClientWrapper,
    task_gid: str,
    description_html: str | None,
) -> dict:
    """
    Download attachments referenced in description_html via data-asana-gid.

    Looks for tags like:
      data-asana-type="attachment" data-asana-gid="12345"
    and saves them under:
      data/attachments/asana_tasks/<task_gid>/description/<name>

    Returns: {attachment_gid: absolute_path_to_local_file}
    """
    if not description_html:
        return {}

    # Use Attachments API to resolve and download all attachments for this task
    downloaded: dict[str, str] = {}  # {gid: local_path}
    ATTACHMENTS_BASE_DIR.mkdir(parents=True, exist_ok=True)
    task_dir = ATTACHMENTS_BASE_DIR / str(task_gid) / "description"
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        attachments = client._with_retry(
            client.attachments.get_attachments_for_object,
            task_gid,
            {"opt_fields": "gid,name,download_url"},
        )
    except Exception:
        return {}

    for att in attachments or []:
        att_gid = att.get("gid")
        download_url = att.get("download_url")
        if not att_gid or not download_url:
            continue

        name = att.get("name") or att.get("gid") or "attachment"
        target_path = task_dir / str(name)

        if target_path.exists() and target_path.stat().st_size > 0:
            downloaded[str(att_gid)] = str(target_path)
            continue

        try:
            resp = requests.get(download_url, timeout=120, stream=True)
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type.lower():
                continue

            with open(target_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            downloaded[str(att_gid)] = str(target_path)
        except Exception:
            continue

    return downloaded


def rewrite_html_with_local_attachments(
    html: str | None,
    attachment_map: dict,
) -> str | None:
    """
    Rewrite Asana-hosted asset URLs in HTML to point at local attachment files.

    attachment_map: {asana_attachment_gid: absolute_path_to_local_file}

    Finds img tags with data-asana-gid attributes and replaces their src URLs
    with local file paths. Handles attributes in any order.
    """
    if not html or not attachment_map:
        return html

    import re

    rewritten = html

    # Find all img tags with data-asana-gid attributes
    # Pattern matches img tags that have both data-asana-gid and src attributes (in any order)
    pattern = r"<img([^>]*?)>"

    def replace_src(match):
        img_content = match.group(1)

        # Extract data-asana-gid
        gid_match = re.search(r'data-asana-gid="([^"]+)"', img_content)
        if not gid_match:
            return match.group(0)  # No GID, no change

        gid = gid_match.group(1)
        if gid not in attachment_map:
            return match.group(0)  # GID not in map, no change

        # Get local path
        local_path = attachment_map[gid]
        try:
            rel_path = str(Path(local_path).relative_to(PROJECT_ROOT))
        except Exception:
            rel_path = local_path

        # Replace src URL (find src="..." and replace the URL part)
        src_pattern = r'src="([^"]+)"'

        def replace_url(m):
            return f'src="{rel_path}"'

        new_img_content = re.sub(src_pattern, replace_url, img_content)
        return f"<img{new_img_content}>"

    rewritten = re.sub(pattern, replace_src, rewritten)

    return rewritten


def html_to_local_text(html: str | None) -> str | None:
    """
    Convert HTML (already rewritten to local paths) into a readable text form.

    Rules:
    - <a href="url">label</a> → "label (url)" (preserve label, keep URL visible)
    - <img src="path"...> → "[attachment: path]" (use local path)
    - <br> and block boundaries → newlines
    - Strip remaining tags
    """
    if not html:
        return None

    import re
    from html import unescape

    text = html

    # Normalize line breaks
    text = re.sub(r"<br\\s*/?>", "\n", text, flags=re.IGNORECASE)

    # Links: preserve label and URL
    def replace_link(m: re.Match) -> str:
        url = m.group(1)
        label = m.group(2).strip() or url
        return f"{label} ({url})"

    text = re.sub(
        r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        replace_link,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Images: show local path
    def replace_img(m: re.Match) -> str:
        src = m.group(1)
        return f"[attachment: {src}]"

    text = re.sub(
        r'<img[^>]*src="([^"]+)"[^>]*>',
        replace_img,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", "", text)

    # Unescape HTML entities and normalize whitespace
    text = unescape(text)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def import_comments_for_tasks(
    client: AsanaClientWrapper,
    workspace_name: str,
    task_gids: list[str],
) -> int:
    """Import comments for the given Asana task GIDs.

    Returns number of new comments imported.
    """
    if not task_gids:
        return 0

    # Load local tasks to map Asana GIDs → task_id
    if TASKS_FILE.exists():
        tasks_df = pd.read_parquet(TASKS_FILE)
    else:
        tasks_df = pd.DataFrame()

    comments_df = _ensure_comments_df()
    if COMMENTS_FILE.exists():
        existing_df = pd.read_parquet(COMMENTS_FILE)
        # Ensure all required columns exist
        required_cols = _ensure_comments_df().columns.tolist()
        for col in required_cols:
            if col not in existing_df.columns:
                existing_df[col] = None
        comments_df = existing_df

    if not comments_df.empty and "asana_story_gid" in comments_df.columns:
        existing_story_ids = set(comments_df["asana_story_gid"].astype(str).tolist())
    else:
        existing_story_ids = set()

    # Snapshot before modifications
    _snapshot_comments(comments_df)

    total_new = 0

    for task_gid in task_gids:
        # Map to local task_id if present
        task_id_val: str | None = None
        if not tasks_df.empty and "asana_source_gid" in tasks_df.columns:
            match = tasks_df[tasks_df["asana_source_gid"].astype(str) == str(task_gid)]
            if not match.empty:
                task_id_val = str(match.iloc[0]["task_id"])

        # Fetch stories from Asana (include HTML if available)
        opts = {
            "opt_fields": "gid,type,text,html_text,created_at,created_by,created_by.name"
        }
        stories_resp = client._with_retry(
            client.stories.get_stories_for_task,
            task_gid,
            opts,
        )
        stories = list(stories_resp) if stories_resp else []
        comments = [s for s in stories if s.get("type") == "comment"]
        if not comments:
            continue

        new_rows = []
        # Build attachment map for all comments first
        attachment_map: dict[str, str] = {}
        for story in comments:
            story_text = story.get("text")
            story_html = story.get("html_text")
            story_map = _download_comment_attachments(
                client, task_gid, story_text, story_html
            )
            attachment_map.update(story_map)

        for story in comments:
            story_gid = str(story.get("gid") or "")

            created_at_raw = story.get("created_at")
            created_at_ts = None
            if created_at_raw:
                try:
                    created_at_ts = datetime.fromisoformat(
                        created_at_raw.replace("Z", "+00:00")
                    )
                except Exception:
                    created_at_ts = None

            created_by = story.get("created_by") or {}
            comment_html_remote = story.get("html_text")
            comment_html = rewrite_html_with_local_attachments(
                comment_html_remote,
                attachment_map,
            )
            text_local = html_to_local_text(comment_html or comment_html_remote) or (
                story.get("text") or ""
            )

            # Check if comment already exists
            if story_gid and story_gid in existing_story_ids:
                # Update existing comment (especially HTML fields if missing)
                idx = comments_df[comments_df["asana_story_gid"] == story_gid].index[0]
                comments_df.loc[idx, "comment_html"] = comment_html
                comments_df.loc[idx, "comment_html_remote"] = comment_html_remote
                comments_df.loc[idx, "text"] = text_local
                comments_df.loc[idx, "imported_at"] = date.today()
                updated_existing = True
                continue

            # New comment
            row = {
                "comment_id": str(uuid.uuid4())[:16],
                "task_id": task_id_val,
                "asana_task_gid": str(task_gid),
                "asana_story_gid": story_gid,
                "asana_workspace": workspace_name,
                "author_name": created_by.get("name"),
                "author_gid": created_by.get("gid"),
                "text": text_local,
                "comment_html": comment_html,
                "comment_html_remote": comment_html_remote,
                "created_at": created_at_ts,
                "imported_at": date.today(),
                "import_source_file": f"asana_comments_{workspace_name}",
            }
            new_rows.append(row)

        if new_rows:
            total_new += len(new_rows)
            comments_df = pd.concat(
                [comments_df, pd.DataFrame(new_rows)], ignore_index=True
            )
            existing_story_ids.update(
                r["asana_story_gid"] for r in new_rows if r["asana_story_gid"]
            )

    # Save if we added new comments or updated existing ones
    if total_new > 0 or updated_existing:
        comments_df.to_parquet(COMMENTS_FILE, index=False)

    return total_new


def main() -> None:
    """CLI entry point to import comments for specific tasks or all tasks with Asana GIDs."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Import Asana task comments to parquet"
    )
    parser.add_argument(
        "--workspace",
        choices=["source", "target"],
        default="source",
        help="Workspace to pull comments from (default: source)",
    )
    parser.add_argument(
        "--task-gid",
        action="append",
        help="Asana task GID to import comments for (can be specified multiple times). "
        "If omitted, imports comments for all tasks with asana_source_gid/asana_target_gid in tasks.parquet.",
    )

    args = parser.parse_args()

    config = AsanaConfig.from_env()
    client = (
        AsanaClientWrapper.from_config_source(config)
        if args.workspace == "source"
        else AsanaClientWrapper.from_config_target(config)
    )

    if args.task_gid:
        task_gids = args.task_gid
    else:
        if not TASKS_FILE.exists():
            print("No tasks.parquet found; nothing to import.")
            return
        tasks_df = pd.read_parquet(TASKS_FILE)
        col = "asana_source_gid" if args.workspace == "source" else "asana_target_gid"
        if col not in tasks_df.columns:
            print(f"No {col} column in tasks.parquet; nothing to import.")
            return
        task_gids = tasks_df[col].dropna().astype(str).unique().tolist()

    print(
        f"Importing comments for {len(task_gids)} task(s) from {args.workspace} workspace..."
    )
    new_count = import_comments_for_tasks(client, args.workspace, task_gids)
    print(f"Imported {new_count} new comment(s).")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error importing comments: {exc}", file=sys.stderr)
        sys.exit(1)
