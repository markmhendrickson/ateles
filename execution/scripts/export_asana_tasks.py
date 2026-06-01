#!/usr/bin/env python3
"""
Post local tasks to Asana

Selects a subset of incomplete tasks from `data/tasks/tasks.parquet` and creates
corresponding tasks in the configured Asana **target workspace**, assigning them
(by default) to the configured fallback assignee.

Rules:
- Only tasks with status in {pending, in_progress, blocked}
- Ignores whether `task_id` already starts with `asana-` (safe because we are
  explicitly targeting a *different* workspace than the source import)
- Sorted by due date (soonest first)
- Posts up to N tasks (default 10)

Usage:
    python scripts/export_asana_tasks.py              # post up to 10 tasks
    python scripts/export_asana_tasks.py --limit 5    # post up to 5 tasks

Requires Asana config env vars (see `scripts/config.py`):
    ASANA_SOURCE_PAT
    SOURCE_WORKSPACE_GID
    TARGET_WORKSPACE_GID

Optional:
    ASANA_TARGET_PAT
    FALLBACK_ASSIGNEE_EMAIL   # used as "me" for assignment
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.client import AsanaClientWrapper
from scripts.config import DATA_DIR, AsanaConfig

TASKS_FILE = DATA_DIR / "tasks" / "tasks.parquet"
CUSTOM_FIELDS_FILE = DATA_DIR / "task_custom_fields" / "task_custom_fields.parquet"
DEPENDENCIES_FILE = DATA_DIR / "task_dependencies" / "task_dependencies.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
LOGS_DIR = DATA_DIR / "logs"
CHECKPOINT_FILE = LOGS_DIR / "export_checkpoint.json"

# Ensure directories exist
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def get_assignee_gid(client: AsanaClientWrapper, config: AsanaConfig) -> str | None:
    """Resolve the gid of the user to assign tasks to.

    Preference order:
    1. Get current user via "me" endpoint (most reliable)
    2. FALLBACK_ASSIGNEE_EMAIL in config, matched against users in target workspace
    3. If not set or not found, return None (tasks will be unassigned)
    """

    # First try: Get current user via "me" endpoint using direct HTTP
    try:
        import requests

        headers = {"Authorization": f"Bearer {client._pat}"}
        url = "https://app.asana.com/api/1.0/users/me"
        params = {"opt_fields": "gid,name,email"}
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        me = response.json().get("data", {})
        gid = me.get("gid")
        if gid:
            return gid
    except Exception as e:
        print(f"Warning: Could not get current user via 'me' endpoint: {e}")

    # Fallback: Try to find by email
    email = config.fallback_assignee_email
    if not email:
        return None

    try:
        opts = {
            "workspace": config.target_workspace_gid,
            "opt_fields": ["email", "name"],
        }
        users = list(client._with_retry(client.users.get_users, opts))
    except Exception as e:  # noqa: BLE001
        print(f"Warning: failed to list users in target workspace: {e}")
        return None

    for user in users:
        if user.get("email") == email:
            return user.get("gid")

    print(
        f"Warning: no user found with email {email!r} in target workspace; tasks will be unassigned"
    )
    return None


def select_tasks(
    limit: int,
    assignee_gid: str | None = None,
    all_assigned: bool = False,
    df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Select up to `limit` local tasks to post to Asana.

    Criteria:
    - status in {pending, in_progress, blocked} (unless all_assigned=True)
    - optionally filtered by assignee_gid
    - sorted by due date
    - we do NOT filter on `task_id` prefix, because we may be
      posting tasks into a different Asana workspace than the one
      they were originally imported from.

    Args:
        limit: Maximum number of tasks to return
        assignee_gid: Optional assignee GID to filter by (if None, no assignee filter)
        all_assigned: If True, export all tasks assigned to assignee_gid regardless of status or parent_task_id
        df: Optional DataFrame to use instead of reading from file
    """

    if df is None:
        if not TASKS_FILE.exists():
            raise FileNotFoundError(f"Tasks file not found: {TASKS_FILE}")
        df = pd.read_parquet(TASKS_FILE)

    if all_assigned:
        # Export all tasks assigned to the user, regardless of status or parent_task_id
        if assignee_gid:
            active = df[df["assignee_gid"].astype(str) == str(assignee_gid)]
        else:
            active = df.copy()
    else:
        active = df[df["status"].isin(["pending", "in_progress", "blocked"])]

        # Exclude tasks that have a parent_task_id - these should be created as subtasks, not main tasks
        active = active[active["parent_task_id"].isna()]

        # Filter by assignee if specified
        if assignee_gid:
            # Match assignee_gid exactly, or handle None/NaN values
            active = active[active["assignee_gid"].astype(str) == str(assignee_gid)]

    if active.empty:
        print("No incomplete tasks found to post.")
        return active

    # Order by due date (soonest first)
    active = active.sort_values(
        "due_date",
        ascending=True,
        na_position="last",
    )

    if limit > 0:
        active = active.head(limit)

    return active


def snapshot_tasks(df: pd.DataFrame) -> None:
    """Create a snapshot of the current tasks parquet before modification."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    snapshot_path = SNAPSHOTS_DIR / f"tasks-{timestamp}.parquet"
    df.to_parquet(snapshot_path, index=False)
    print(f"Created snapshot: {snapshot_path}")


def save_checkpoint(last_task_id: str, processed_count: int, total_count: int) -> None:
    """Save checkpoint with last processed task ID (atomic write)."""
    checkpoint_data = {
        "last_task_id": last_task_id,
        "processed_count": processed_count,
        "total_count": total_count,
        "timestamp": datetime.now().isoformat(),
    }
    # Atomic write: write to temp file, then rename
    temp_file = CHECKPOINT_FILE.with_suffix(".tmp")
    try:
        with open(temp_file, "w") as f:
            json.dump(checkpoint_data, f, indent=2)
        temp_file.replace(CHECKPOINT_FILE)  # Atomic rename
        print(f"  Checkpoint saved: processed {processed_count}/{total_count} tasks")
    except Exception as e:
        print(f"Warning: Could not save checkpoint: {e}")
        # Clean up temp file on error
        if temp_file.exists():
            temp_file.unlink()


def load_checkpoint() -> dict[str, Any] | None:
    """Load checkpoint if it exists."""
    if not CHECKPOINT_FILE.exists():
        return None
    try:
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Could not load checkpoint: {e}")
        return None


def clear_checkpoint() -> None:
    """Clear checkpoint file."""
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        print("Checkpoint cleared")


class ProjectManager:
    """Manage projects and sections in target workspace."""

    def __init__(self, client: AsanaClientWrapper, workspace_gid: str):
        self.client = client
        self.workspace_gid = workspace_gid
        # Cache: project_name -> project_gid
        self.project_cache = {}
        # Cache: (project_gid, section_name) -> section_gid
        self.section_cache = {}

    def get_or_create_project(self, project_name: str) -> str | None:
        """Get existing project by name or create it. Returns project GID."""
        if not project_name:
            return None

        # Check cache
        if project_name in self.project_cache:
            return self.project_cache[project_name]

        # Search for existing project
        try:
            opts = {
                "workspace": self.workspace_gid,
                "archived": False,
            }
            projects = list(
                self.client._with_retry(self.client.projects.get_projects, opts)
            )

            for project in projects:
                if project.get("name") == project_name:
                    project_gid = project.get("gid")
                    self.project_cache[project_name] = project_gid
                    return project_gid
        except Exception as e:
            print(f"Warning: Error searching for project '{project_name}': {e}")
            return None

        # Project doesn't exist - create it
        try:
            body = {
                "data": {
                    "name": project_name,
                    "workspace": self.workspace_gid,
                }
            }
            result = self.client._with_retry(
                self.client.projects.create_project, body, {}
            )
            project_gid = result.get("gid")
            self.project_cache[project_name] = project_gid
            print(f"  Created project: {project_name} ({project_gid})")
            return project_gid
        except Exception as e:
            print(f"Warning: Error creating project '{project_name}': {e}")
            return None

    def get_or_create_section(self, project_gid: str, section_name: str) -> str | None:
        """Get existing section in project by name or create it. Returns section GID."""
        if not project_gid or not section_name:
            return None

        # Skip placeholder section names
        if section_name in ["(no section)", "None", ""]:
            return None

        cache_key = (project_gid, section_name)

        # Check cache
        if cache_key in self.section_cache:
            return self.section_cache[cache_key]

        # Search for existing section
        try:
            opts = {}
            sections = list(
                self.client._with_retry(
                    self.client.sections.get_sections_for_project, project_gid, opts
                )
            )

            for section in sections:
                if section.get("name") == section_name:
                    section_gid = section.get("gid")
                    self.section_cache[cache_key] = section_gid
                    return section_gid
        except Exception as e:
            print(
                f"Warning: Error searching for section '{section_name}' in project {project_gid}: {e}"
            )
            return None

        # Section doesn't exist - create it
        try:
            opts = {
                "body": {
                    "data": {
                        "name": section_name,
                    }
                }
            }
            result = self.client._with_retry(
                self.client.sections.create_section_for_project, project_gid, opts
            )
            section_gid = result.get("gid")
            self.section_cache[cache_key] = section_gid
            print(f"    Created section: {section_name} ({section_gid})")
            return section_gid
        except Exception as e:
            print(
                f"Warning: Error creating section '{section_name}' in project {project_gid}: {e}"
            )
            return None

    def get_or_create_mytasks_section(
        self, workspace_gid: str, section_name: str
    ) -> str | None:
        """Get or create a section in My Tasks for the current user.

        Uses a workaround: gets user_task_list GID from an existing task's assignee_section.

        Returns section GID if successful, None otherwise.
        """
        if not section_name or section_name == "(no section)":
            return None

        # Cache the user_task_list_gid to avoid repeated lookups
        cache_key = f"user_task_list_{workspace_gid}"
        if not hasattr(self, "_mytasks_cache"):
            self._mytasks_cache = {}

        user_task_list_gid = self._mytasks_cache.get(cache_key)

        if not user_task_list_gid:
            try:
                # Workaround: Get user_task_list GID from an existing task's assignee_section
                # Get a task assigned to me in this workspace
                opts = {
                    "assignee": "me",
                    "workspace": workspace_gid,
                    "opt_fields": "gid,assignee_section,assignee_section.gid,assignee_section.project,assignee_section.project.gid",
                    "limit": 1,
                }
                tasks = list(self.client._with_retry(self.client.tasks.get_tasks, opts))

                if tasks:
                    task = tasks[0]
                    assignee_section = task.get("assignee_section", {})
                    if assignee_section:
                        # The section's project is the user_task_list
                        project = assignee_section.get("project", {})
                        user_task_list_gid = (
                            project.get("gid") if isinstance(project, dict) else project
                        )

                if not user_task_list_gid:
                    # Fallback: Try to get it from the section directly if we have a section GID
                    section_gid = (
                        assignee_section.get("gid") if assignee_section else None
                    )
                    if section_gid:
                        try:
                            section_opts = {"opt_fields": "project,project.gid"}
                            section_data = self.client._with_retry(
                                self.client.sections.get_section,
                                section_gid,
                                section_opts,
                            )
                            project = section_data.get("project", {})
                            user_task_list_gid = (
                                project.get("gid")
                                if isinstance(project, dict)
                                else project
                            )
                        except Exception:
                            pass
            except Exception:
                # If we can't get user_task_list_gid, we'll return None
                pass

                if not user_task_list_gid:
                    print(
                        f"  Warning: Could not find user task list for workspace {workspace_gid}"
                    )
                return None

                # Cache it
                self._mytasks_cache[cache_key] = user_task_list_gid

            except Exception as e:
                print(f"  Warning: Error finding user task list: {e}")
                return None

        # Now use the cached user_task_list_gid to get/create sections
        try:
            # Get sections in user task list
            sections_opts = {}
            sections = list(
                self.client._with_retry(
                    self.client.sections.get_sections_for_project,
                    user_task_list_gid,
                    sections_opts,
                )
            )

            # Check if section exists
            for section in sections:
                if section.get("name") == section_name:
                    return section.get("gid")

            # Section doesn't exist - create it
            opts = {
                "body": {
                    "data": {
                        "name": section_name,
                    }
                }
            }
            result = self.client._with_retry(
                self.client.sections.create_section_for_project,
                user_task_list_gid,
                opts,
            )
            section_gid = result.get("gid")
            print(f"    Created My Tasks section: {section_name} ({section_gid})")
            return section_gid

        except Exception as e:
            print(f"  Warning: Error creating My Tasks section '{section_name}': {e}")
            return None


def fetch_and_upload_attachments(
    source_client: AsanaClientWrapper,
    target_client: AsanaClientWrapper,
    source_task_gid: str,
    target_task_gid: str,
) -> int:
    """
    Fetch attachments from source task and upload to target task.

    Returns number of attachments successfully uploaded.
    """
    try:
        # Fetch attachments from source task
        opts = {
            "opt_fields": "attachments,attachments.gid,attachments.name,attachments.download_url"
        }
        source_task = source_client._with_retry(
            source_client.tasks.get_task, source_task_gid, opts
        )

        attachments = source_task.get("attachments", [])
        if not attachments:
            return 0

        print(f"    Found {len(attachments)} attachment(s) in source task")

        uploaded_count = 0

        for attachment in attachments:
            attachment_gid = attachment.get("gid")
            attachment_name = attachment.get("name", "attachment")
            download_url = attachment.get("download_url")

            if not download_url:
                # Need to get download URL separately
                try:
                    att_opts = {"opt_fields": "download_url"}
                    att_data = source_client._with_retry(
                        source_client.attachments.get_attachment,
                        attachment_gid,
                        att_opts,
                    )
                    download_url = att_data.get("download_url")
                except Exception as e:
                    print(
                        f"    Warning: Could not get download URL for attachment '{attachment_name}': {e}"
                    )
                    continue

            if not download_url:
                continue

            # Download attachment
            try:
                # S3 pre-signed URLs include authentication in query string, don't add Authorization header
                headers = {}

                # First, try to get attachment metadata to check size
                try:
                    att_meta_opts = {"opt_fields": "download_url,size,mimetype"}
                    att_meta = source_client._with_retry(
                        source_client.attachments.get_attachment,
                        attachment_gid,
                        att_meta_opts,
                    )
                    file_size = att_meta.get("size", 0)
                    mime_type = att_meta.get("mimetype", "application/octet-stream")

                    # Check file size (Asana has limits)
                    if file_size > 100 * 1024 * 1024:  # 100MB limit
                        print(
                            f"    Warning: Attachment '{attachment_name}' is too large ({file_size / 1024 / 1024:.1f}MB), skipping"
                        )
                        continue

                    # Update download_url if we got a fresh one
                    if att_meta.get("download_url"):
                        download_url = att_meta.get("download_url")
                except Exception as meta_error:
                    print(
                        f"    Warning: Could not get attachment metadata: {meta_error}"
                    )
                    mime_type = "application/octet-stream"

                # Download the file (with retry for expired URLs)
                tmp_path = None
                download_success = False

                for retry_attempt in range(2):  # Try original URL, then fresh URL
                    try:
                        response = requests.get(
                            download_url, headers=headers, timeout=120, stream=True
                        )
                        response.raise_for_status()

                        # Check content length
                        content_length = response.headers.get("Content-Length")
                        if content_length and int(content_length) > 100 * 1024 * 1024:
                            print(
                                f"    Warning: Attachment '{attachment_name}' is too large ({int(content_length) / 1024 / 1024:.1f}MB), skipping"
                            )
                            break

                        # Download to temp file
                        with tempfile.NamedTemporaryFile(
                            delete=False, suffix=Path(attachment_name).suffix
                        ) as tmp_file:
                            for chunk in response.iter_content(chunk_size=8192):
                                if chunk:
                                    tmp_file.write(chunk)
                            tmp_path = tmp_file.name

                        download_success = True
                        break

                    except requests.exceptions.HTTPError as http_error:
                        if (
                            http_error.response.status_code in [400, 403]
                            and retry_attempt == 0
                        ):
                            # URL expired or forbidden - try to get fresh one
                            print(
                                f"    ⚠ Download URL expired/forbidden for '{attachment_name}', fetching fresh URL..."
                            )
                            try:
                                fresh_opts = {"opt_fields": "download_url"}
                                fresh_att = source_client._with_retry(
                                    source_client.attachments.get_attachment,
                                    attachment_gid,
                                    fresh_opts,
                                )
                                fresh_url = fresh_att.get("download_url")
                                if fresh_url:
                                    download_url = fresh_url
                                    print("    Got fresh download URL, retrying...")
                                    continue  # Retry with fresh URL
                                else:
                                    print("    Warning: Fresh download URL is empty")
                                    break
                            except Exception as retry_error:
                                print(
                                    f"    Warning: Failed to refresh download URL: {retry_error}"
                                )
                                import traceback

                                traceback.print_exc()
                                break
                        else:
                            print(
                                f"    Warning: HTTP error downloading '{attachment_name}': {http_error}"
                            )
                            break
                    except Exception as download_error:
                        print(
                            f"    Warning: Could not download '{attachment_name}': {download_error}"
                        )
                        break

                if not download_success or not tmp_path:
                    print(
                        f"    ⚠ Skipping '{attachment_name}' - download failed (URL may be expired)"
                    )
                    continue

                # Upload to target task
                try:
                    # Persist a copy of the downloaded file in a stable local CDN-style path
                    try:
                        cdn_dir = (
                            DATA_DIR / "attachments" / "asana_tasks" / source_task_gid
                        )
                        cdn_dir.mkdir(parents=True, exist_ok=True)
                        cdn_path = cdn_dir / attachment_name
                        shutil.copy2(tmp_path, cdn_path)
                    except Exception as cdn_error:
                        print(
                            f"    Warning: Could not persist attachment '{attachment_name}' locally: {cdn_error}"
                        )

                    with open(tmp_path, "rb") as f:
                        # Determine MIME type from file extension if not provided
                        if mime_type == "application/octet-stream":
                            import mimetypes

                            guessed_type, _ = mimetypes.guess_type(attachment_name)
                            if guessed_type:
                                mime_type = guessed_type

                        files = {"file": (attachment_name, f, mime_type)}
                        data = {"name": attachment_name}

                        upload_headers = {
                            "Authorization": f"Bearer {target_client._pat}"
                        }
                        upload_url = f"https://app.asana.com/api/1.0/tasks/{target_task_gid}/attachments"

                        upload_response = requests.post(
                            upload_url,
                            headers=upload_headers,
                            files=files,
                            data=data,
                            timeout=300,  # Longer timeout for large files
                        )
                        upload_response.raise_for_status()

                        uploaded_count += 1
                        file_size_mb = Path(tmp_path).stat().st_size / 1024 / 1024
                        print(
                            f"    ✓ Uploaded attachment: {attachment_name} ({file_size_mb:.2f}MB)"
                        )
                except requests.exceptions.HTTPError as upload_http_error:
                    error_detail = (
                        upload_http_error.response.text
                        if upload_http_error.response
                        else str(upload_http_error)
                    )
                    print(
                        f"    Warning: HTTP error uploading '{attachment_name}': {upload_http_error.response.status_code}"
                    )
                    print(f"      Response: {error_detail[:200]}")
                except Exception as upload_error:
                    print(
                        f"    Warning: Could not upload '{attachment_name}': {upload_error}"
                    )
                finally:
                    # Clean up temp file
                    Path(tmp_path).unlink(missing_ok=True)

            except Exception as e:
                print(
                    f"    Warning: Could not process attachment '{attachment_name}': {e}"
                )
                continue

        return uploaded_count

    except Exception as e:
        print(f"    Warning: Could not fetch attachments from source task: {e}")
        return 0


def fetch_and_post_comments(
    source_client: AsanaClientWrapper,
    target_client: AsanaClientWrapper,
    source_task_gid: str,
    target_task_gid: str,
) -> int:
    """
    Fetch comments (stories) from source task and post to target task.

    Returns number of comments successfully posted.
    """
    try:
        # Fetch stories from source task (comments are story type "comment")
        opts = {"opt_fields": "gid,type,text,created_at,created_by,created_by.name"}
        stories_response = source_client._with_retry(
            source_client.stories.get_stories_for_task, source_task_gid, opts
        )
        stories = list(stories_response) if stories_response else []

        # Filter for comment-type stories
        comments = [s for s in stories if s.get("type") == "comment"]
        if not comments:
            return 0

        print(f"    Found {len(comments)} comment(s) in source task")

        posted_count = 0

        for comment in comments:
            comment_text = comment.get("text", "").strip()
            if not comment_text:
                continue

            # Get author info for attribution
            created_by = comment.get("created_by", {})
            author_name = created_by.get("name", "Unknown")
            created_at = comment.get("created_at", "")

            # Format comment with attribution
            if created_at:
                try:
                    from datetime import datetime

                    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    date_str = dt.strftime("%Y-%m-%d %H:%M")
                    formatted_comment = (
                        f"[From {author_name} on {date_str}]\n\n{comment_text}"
                    )
                except Exception:
                    formatted_comment = f"[From {author_name}]\n\n{comment_text}"
            else:
                formatted_comment = f"[From {author_name}]\n\n{comment_text}"

            try:
                # Post comment to target task
                body = {
                    "data": {
                        "text": formatted_comment,
                    }
                }
                opts = {}
                target_client._with_retry(
                    target_client.stories.create_story_for_task,
                    target_task_gid,
                    body,
                    opts,
                )

                posted_count += 1
                print(f"    ✓ Posted comment from {author_name}")
            except Exception as e:
                print(f"    Warning: Could not post comment from {author_name}: {e}")
                continue

        return posted_count

    except Exception as e:
        print(f"    Warning: Could not fetch comments from source task: {e}")
        return 0


def get_or_create_tag(
    client: AsanaClientWrapper, workspace_gid: str, tag_name: str
) -> str | None:
    """Get existing tag by name or create it. Returns tag GID."""
    if not tag_name:
        return None

    try:
        # Search for existing tags in workspace
        opts = {"workspace": workspace_gid, "opt_fields": "name"}
        tags = list(client._with_retry(client.tags.get_tags, opts))

        for tag in tags:
            if tag.get("name") == tag_name:
                return tag.get("gid")

        # Tag doesn't exist - create it
        body = {
            "data": {
                "name": tag_name,
                "workspace": workspace_gid,
            }
        }
        result = client._with_retry(client.tags.create_tag, body, {})
        tag_gid = result.get("gid")
        print(f"    Created tag: {tag_name} ({tag_gid})")
        return tag_gid
    except Exception as e:
        print(f"    Warning: Could not get or create tag '{tag_name}': {e}")
        return None


def add_tags_to_task(
    client: AsanaClientWrapper, task_gid: str, workspace_gid: str, tag_names: list[str]
) -> int:
    """Add tags to a task. Returns number of tags successfully added."""
    if not tag_names:
        return 0

    added_count = 0
    for tag_name in tag_names:
        if not tag_name or tag_name.strip() == "":
            continue

        tag_gid = get_or_create_tag(client, workspace_gid, tag_name.strip())
        if not tag_gid:
            continue

        try:
            # Add tag to task via HTTP POST
            url = f"https://app.asana.com/api/1.0/tasks/{task_gid}/addTag"
            headers = {"Authorization": f"Bearer {client._pat}"}
            data = {"data": {"tag": tag_gid}}
            response = requests.post(url, headers=headers, json=data, timeout=30)
            response.raise_for_status()
            added_count += 1
        except Exception as e:
            print(f"    Warning: Could not add tag '{tag_name}' to task: {e}")
            continue

    if added_count > 0:
        print(f"    Added {added_count} tag(s)")
    return added_count


def add_followers_to_task(
    client: AsanaClientWrapper, task_gid: str, follower_gids: list[str]
) -> int:
    """Add followers to a task. Returns number of followers successfully added."""
    if not follower_gids:
        return 0

    added_count = 0
    for follower_gid in follower_gids:
        if not follower_gid or follower_gid.strip() == "":
            continue

        try:
            # Add follower to task via HTTP POST
            url = f"https://app.asana.com/api/1.0/tasks/{task_gid}/addFollowers"
            headers = {"Authorization": f"Bearer {client._pat}"}
            data = {"data": {"followers": [follower_gid.strip()]}}
            response = requests.post(url, headers=headers, json=data, timeout=30)
            response.raise_for_status()
            added_count += 1
        except Exception as e:
            print(f"    Warning: Could not add follower {follower_gid} to task: {e}")
            continue

    if added_count > 0:
        print(f"    Added {added_count} follower(s)")
    return added_count


def set_custom_fields_on_task(
    client: AsanaClientWrapper, task_gid: str, task_id: str, workspace_gid: str
) -> int:
    """Set custom field values on a task from task_custom_fields.parquet.

    Returns number of custom fields successfully set.
    """
    if not CUSTOM_FIELDS_FILE.exists():
        return 0

    try:
        custom_fields_df = pd.read_parquet(CUSTOM_FIELDS_FILE)
        if custom_fields_df.empty:
            return 0

        # Filter for custom fields for this task
        task_custom_fields = custom_fields_df[
            custom_fields_df["task_id"].astype(str) == str(task_id)
        ]

        if task_custom_fields.empty:
            return 0

        set_count = 0

        for _, cf_row in task_custom_fields.iterrows():
            cf_gid = str(cf_row.get("asana_custom_field_gid", ""))
            cf_type = str(cf_row.get("custom_field_type", ""))

            if not cf_gid:
                continue

            # Build custom field value based on type
            cf_value = None

            if cf_type == "text":
                cf_value = {"text_value": str(cf_row.get("text_value", ""))}
            elif cf_type == "number":
                number_val = cf_row.get("number_value")
                if pd.notna(number_val):
                    cf_value = {"number_value": float(number_val)}
            elif cf_type == "enum":
                enum_val = str(cf_row.get("enum_value", ""))
                if enum_val:
                    # For enum, we need the enum option GID, not the name
                    # Try to find it by fetching the custom field definition
                    try:
                        cf_def_opts = {
                            "opt_fields": "enum_options,enum_options.gid,enum_options.name"
                        }
                        cf_def = client._with_retry(
                            client.custom_fields.get_custom_field, cf_gid, cf_def_opts
                        )
                        enum_options = cf_def.get("enum_options", [])
                        for opt in enum_options:
                            if opt.get("name") == enum_val:
                                cf_value = {"enum_value": opt.get("gid")}
                                break
                    except Exception:
                        pass
            elif cf_type == "date":
                date_val = cf_row.get("date_value")
                if pd.notna(date_val):
                    if hasattr(date_val, "isoformat"):
                        cf_value = {"date_value": date_val.isoformat()}
                    else:
                        cf_value = {"date_value": str(date_val)}
            elif cf_type == "people":
                people_gids_str = str(cf_row.get("people_value_gids", ""))
                if people_gids_str and people_gids_str != "nan":
                    people_gids = [
                        gid.strip() for gid in people_gids_str.split("|") if gid.strip()
                    ]
                    if people_gids:
                        cf_value = {"people_value": people_gids}
            elif cf_type == "multi_enum":
                multi_enum_str = str(cf_row.get("multi_enum_values", ""))
                if multi_enum_str and multi_enum_str != "nan":
                    # For multi_enum, we need option GIDs
                    # This is complex - for now, skip or try to match by name
                    pass

            if not cf_value:
                continue

            try:
                # Set custom field value via HTTP PUT
                url = f"https://app.asana.com/api/1.0/tasks/{task_gid}/setCustomField"
                headers = {"Authorization": f"Bearer {client._pat}"}
                data = {"data": {"custom_field": cf_gid, **cf_value}}
                response = requests.post(url, headers=headers, json=data, timeout=30)
                response.raise_for_status()
                set_count += 1
            except Exception as e:
                print(
                    f"    Warning: Could not set custom field {cf_gid} ({cf_type}): {e}"
                )
                continue

        if set_count > 0:
            print(f"    Set {set_count} custom field(s)")
        return set_count

    except Exception as e:
        print(f"    Warning: Could not set custom fields: {e}")
        return 0


def create_dependencies_for_task(
    client: AsanaClientWrapper,
    task_gid: str,
    task_id: str,
    target_task_gids_map: dict[str, str],
) -> int:
    """Create dependencies for a task from task_dependencies.parquet.

    Args:
        task_gid: Asana GID of the task in target workspace
        task_id: Local task_id
        target_task_gids_map: Map of local task_id -> target workspace Asana GID

    Returns number of dependencies successfully created.
    """
    if not DEPENDENCIES_FILE.exists():
        return 0

    try:
        dependencies_df = pd.read_parquet(DEPENDENCIES_FILE)
        if dependencies_df.empty:
            return 0

        # Filter for dependencies where this task is the successor
        task_deps = dependencies_df[
            dependencies_df["task_id"].astype(str) == str(task_id)
        ]

        if task_deps.empty:
            return 0

        created_count = 0

        for _, dep_row in task_deps.iterrows():
            predecessor_task_id = str(dep_row.get("predecessor_task_id", ""))

            if not predecessor_task_id:
                continue

            # Find predecessor's target GID
            predecessor_target_gid = target_task_gids_map.get(predecessor_task_id)
            if not predecessor_target_gid:
                # Predecessor not yet exported - skip for now
                continue

            try:
                # Create dependency: predecessor blocks this task (successor)
                url = f"https://app.asana.com/api/1.0/tasks/{task_gid}/addDependencies"
                headers = {"Authorization": f"Bearer {client._pat}"}
                data = {"data": {"dependencies": [predecessor_target_gid]}}
                response = requests.post(url, headers=headers, json=data, timeout=30)
                response.raise_for_status()
                created_count += 1
            except Exception as e:
                print(
                    f"    Warning: Could not create dependency on {predecessor_target_gid}: {e}"
                )
                continue

        if created_count > 0:
            print(f"    Created {created_count} dependency/dependencies")
        return created_count

    except Exception as e:
        print(f"    Warning: Could not create dependencies: {e}")
        return 0


def post_tasks(
    limit: int,
    only_my_tasks: bool = False,
    all_assigned: bool = False,
    resume: bool = False,
    checkpoint_interval: int = 10,
) -> None:
    """Post up to `limit` tasks to Asana target workspace with projects/sections.

    Args:
        limit: Maximum number of tasks to export
        only_my_tasks: If True, only export tasks assigned to the current user
        all_assigned: If True, export all tasks assigned to the user regardless of status or parent_task_id
        resume: If True, resume from last checkpoint (skips already exported tasks)
        checkpoint_interval: Save checkpoint every N tasks (default: 10)
    """

    # Set up Asana clients early to get assignee GID if needed
    config = AsanaConfig.from_env()
    target_client = AsanaClientWrapper.from_config_target(config)
    assignee_gid = get_assignee_gid(target_client, config) if only_my_tasks else None

    if only_my_tasks and not assignee_gid:
        print("Warning: Could not determine assignee GID. Exporting all tasks instead.")
        only_my_tasks = False
        assignee_gid = None

    # Load tasks and select candidates
    df = pd.read_parquet(TASKS_FILE)

    # Filter by sync_log='pending_export' if column exists, BEFORE calling select_tasks
    if "sync_log" in df.columns:
        pending_export_df = df[df["sync_log"] == "pending_export"]
        if not pending_export_df.empty:
            print(
                f"Found {len(pending_export_df)} tasks with sync_log='pending_export'"
            )
            # Pass filtered dataframe to select_tasks
            candidates = select_tasks(
                limit,
                assignee_gid=assignee_gid,
                all_assigned=all_assigned,
                df=pending_export_df,
            )
        else:
            print("No tasks with sync_log='pending_export' found")
            return
    else:
        candidates = select_tasks(
            limit, assignee_gid=assignee_gid, all_assigned=all_assigned
        )

    if candidates.empty:
        return

    # Check for resume checkpoint
    checkpoint = None
    if resume:
        checkpoint = load_checkpoint()
        if checkpoint:
            last_task_id = checkpoint.get("last_task_id")
            processed_count = checkpoint.get("processed_count", 0)
            total_count = checkpoint.get("total_count", len(candidates))
            print(
                f"Resuming from checkpoint: last processed task ID {last_task_id} ({processed_count} tasks)"
            )
            # Filter out already exported tasks (those with asana_target_gid)
            candidates = candidates[pd.isna(candidates["asana_target_gid"])]
            print(f"After resume filter: {len(candidates)} tasks remaining to export")
        else:
            print("No checkpoint found, starting from beginning")

    total_tasks = len(candidates)
    print(f"Selected {total_tasks} local tasks to post to Asana (limit={limit})")

    # Snapshot before modifying
    snapshot_tasks(df)

    # Set up source client (target client already set above)
    source_client = AsanaClientWrapper.from_config_source(config)
    # assignee_gid is the gid of the current API user in the target workspace

    # Set up project manager for creating projects/sections
    project_mgr = ProjectManager(target_client, config.target_workspace_gid)

    # Post tasks
    created = []
    today = date.today()

    # Track task_id -> target_gid mapping for dependencies
    target_task_gids_map: dict[str, str] = {}

    # Track original task_id -> target_gid mapping for subtask lookup
    # (task_id may be updated to Asana GID, so we need to preserve original)
    original_task_id_map: dict[int, str] = {}  # idx -> original_task_id

    # Track processed count for checkpoint
    processed_count = checkpoint.get("processed_count", 0) if checkpoint else 0
    start_position = processed_count + 1

    for task_idx, (idx, row) in enumerate(candidates.iterrows(), start=1):
        title = row["title"]
        status = row.get("status", "")

        # Skip completed tasks when creating new tasks (can still update existing ones)
        # Check if task already exists in target workspace
        task_id_val = str(row.get("task_id") or "")
        asana_target_gid = row.get("asana_target_gid")
        existing_target_gid = None
        if pd.notna(asana_target_gid) and asana_target_gid:
            existing_target_gid = str(asana_target_gid)
        elif str(row.get("import_source_file")) == "asana-post" and task_id_val:
            existing_target_gid = task_id_val

        # If task is completed and doesn't exist in target workspace, skip creating it
        if status == "completed" and not existing_target_gid:
            print(
                f"Skipping completed task '{title}' (not yet exported to target workspace)"
            )
            continue

        # Use HTML description if available, fallback to plain text
        description_html = row.get("description_html")
        description = row.get("description") or ""
        notes = (
            description_html
            if description_html and pd.notna(description_html)
            else (description or row.get("notes") or "")
        )

        # Determine whether this task has already been exported to the target
        # (We already checked this above, but need to recalculate for the rest of the logic)
        asana_target_gid = row.get("asana_target_gid")
        if pd.notna(asana_target_gid) and asana_target_gid:
            existing_target_gid = str(asana_target_gid)
        # 2. task_id if import_source_file indicates it was exported
        elif str(row.get("import_source_file")) == "asana-post" and task_id_val:
            existing_target_gid = task_id_val
        # 3. Check if task_id is already a target workspace GID (verify it exists)
        else:
            existing_target_gid = None
            # Get asana_source_gid first for comparison
            asana_source_gid_val = row.get("asana_source_gid")
            # Only verify if task_id looks like an Asana GID AND we're confident it's from target workspace
            # Don't verify source workspace GIDs - they'll cause 404 errors
            if (
                task_id_val and len(task_id_val) > 10
            ):  # Asana GIDs are typically long numbers
                # Only verify if import_source_file indicates it was exported to target
                # or if task_id doesn't match source workspace patterns
                should_verify = str(row.get("import_source_file")) == "asana-post" or (
                    pd.notna(asana_source_gid_val)
                    and task_id_val != str(asana_source_gid_val)
                )
                if should_verify:
                    try:
                        # Quick check: try to fetch the task from target workspace
                        verify_opts = {"opt_fields": "gid"}
                        target_client._with_retry(
                            target_client.tasks.get_task, task_id_val, verify_opts
                        )
                        # If we get here, task exists in target workspace
                        existing_target_gid = task_id_val
                    except Exception:
                        # Task doesn't exist in target workspace, will create new
                        existing_target_gid = None

            # 4. If still no existing target GID, check for duplicate by title in target workspace
            # This prevents creating duplicate tasks with the same title
            if not existing_target_gid and title:
                try:
                    # Search for tasks with the same title in target workspace
                    # Get current user GID for searching assigned tasks
                    headers = {"Authorization": f"Bearer {target_client._pat}"}
                    me_url = "https://app.asana.com/api/1.0/users/me"
                    me_response = requests.get(
                        me_url, headers=headers, params={"opt_fields": "gid"}, timeout=5
                    )
                    if me_response.status_code == 200:
                        user_gid = me_response.json().get("data", {}).get("gid")

                        # Search in tasks assigned to current user (most likely location for duplicates)
                        search_opts = {
                            "assignee": user_gid,
                            "workspace": config.target_workspace_gid,
                            "opt_fields": "gid,name",
                            "limit": 100,
                        }
                        search_tasks = list(
                            target_client._with_retry(
                                target_client.tasks.get_tasks, search_opts
                            )
                        )

                        # Also check in projects (if task has project associations)
                        project_names_str = row.get("project_names")
                        if pd.notna(project_names_str) and project_names_str:
                            project_names = [
                                p.strip()
                                for p in str(project_names_str).split("|")
                                if p.strip()
                                and p.strip() not in ["(no project)", "None", ""]
                            ]

                            # Get projects in target workspace
                            projects_opts = {
                                "workspace": config.target_workspace_gid,
                                "archived": False,
                            }
                            target_projects = list(
                                target_client._with_retry(
                                    target_client.projects.get_projects, projects_opts
                                )
                            )

                            # Check tasks in matching projects
                            for target_project in target_projects:
                                target_project_name = target_project.get("name", "")
                                if target_project_name in project_names:
                                    project_search_opts = {
                                        "project": target_project.get("gid"),
                                        "opt_fields": "gid,name",
                                        "limit": 100,
                                    }
                                    try:
                                        project_tasks = list(
                                            target_client._with_retry(
                                                target_client.tasks.get_tasks,
                                                project_search_opts,
                                            )
                                        )
                                        search_tasks.extend(project_tasks)
                                    except Exception:
                                        continue

                        # Check for exact title match
                        for search_task in search_tasks:
                            if search_task.get("name") == title:
                                existing_target_gid = search_task.get("gid")
                                print(
                                    f"    ⚠ Found duplicate task with same title '{title}' in target workspace: {existing_target_gid}"
                                )
                                print(
                                    "    → Will update existing task instead of creating duplicate"
                                )
                                break
                except Exception:
                    # If duplicate check fails, continue with creation (better to create than skip)
                    pass

        task_data = {
            "name": title,
            "workspace": config.target_workspace_gid,
        }

        # Use html_notes if we have HTML description, otherwise use plain notes
        # Note: html_notes can cause timeouts/500 errors, so we'll fall back to notes if needed
        use_html_notes = False
        if description_html and pd.notna(description_html):
            html_str = str(description_html)
            # Check for problematic content that causes timeouts:
            # - Embedded Asana task links with data-asana attributes
            # - Very large HTML (>50KB)
            # - Complex nested structures
            has_asana_links = (
                "data-asana-gid" in html_str or "data-asana-type" in html_str
            )
            is_too_large = len(html_str) > 50000

            # Drop html_notes support if it contains problematic content
            # Always use plain notes to avoid timeouts
            if has_asana_links or is_too_large:
                task_data["notes"] = notes
                use_html_notes = False
            else:
                # Only use html_notes for simple, small HTML
                task_data["html_notes"] = description_html
                use_html_notes = True
        else:
            task_data["notes"] = notes

        due_date = row.get("due_date")
        if pd.notna(due_date):
            try:
                # `due_date` may already be a date; ensure string
                if hasattr(due_date, "isoformat"):
                    task_data["due_on"] = due_date.isoformat()
                else:
                    task_data["due_on"] = str(due_date)
            except Exception:
                pass

        # Note: start_on (start_date) requires premium Asana account
        # We skip setting it to avoid 402 Payment Required errors
        # If start dates are needed, they should be set manually in Asana or
        # the workspace should be upgraded to a premium plan

        # Decide whether to assign this task to the current user in the target workspace.
        assign_to_me = False
        source_gid: str | None = None
        src_file = str(row.get("import_source_file") or "")

        # If we have an explicit source GID from a prior import/sync, use it.
        asana_source_gid = row.get("asana_source_gid")
        if isinstance(asana_source_gid, str) and asana_source_gid:
            source_gid = asana_source_gid
        else:
            # For legacy imports, derive source gid from task_id when it is an Asana task id.
            if task_id_val.startswith("asana-") and "asana" in src_file:
                source_gid = task_id_val.split("asana-", 1)[1]

        if source_gid:
            # Task originated in the source workspace. Only assign to self if the
            # source assignee matches the current user.
            try:
                src_opts = {"opt_fields": "assignee,assignee.gid"}
                src_task = source_client._with_retry(
                    source_client.tasks.get_task,
                    source_gid,
                    src_opts,
                )
                src_assignee = src_task.get("assignee")
                src_gid = (
                    src_assignee.get("gid") if isinstance(src_assignee, dict) else None
                )
                if assignee_gid and src_gid and str(src_gid) == str(assignee_gid):
                    assign_to_me = True
            except Exception as e:  # noqa: BLE001
                print(
                    f"    Warning: Could not determine source assignee for {task_id_val}: {e}"
                )
                # If we can't determine, leave unassigned to avoid incorrect attribution.
        else:
            # Local-only tasks (no source gid) should default to being assigned to the current user.
            if assignee_gid:
                assign_to_me = True

        if assign_to_me and assignee_gid:
            task_data["assignee"] = assignee_gid

        # Get source GID for fetching tags/attachments/comments
        # Use asana_source_gid if available (task from source workspace)
        # Otherwise fall back to task_id if it looks like a source GID
        source_task_gid = None
        asana_source_gid_val = row.get("asana_source_gid")
        if pd.notna(asana_source_gid_val) and asana_source_gid_val:
            source_task_gid = str(asana_source_gid_val)
        else:
            # Fallback: use task_id if it's not the target GID
            original_task_id = row.get("task_id", "")
            if original_task_id and original_task_id != existing_target_gid:
                source_task_gid = original_task_id

        # Get project and section names from task
        project_names_str = row.get("project_names")
        section_names_str = row.get("section_names")
        my_tasks_section_names_str = row.get("my_tasks_section_names")

        memberships = []
        mytasks_sections = []

        if pd.notna(project_names_str) and project_names_str:
            # Task has projects - use project-based memberships
            project_names = project_names_str.split("|")
            section_names = (
                section_names_str.split("|")
                if pd.notna(section_names_str) and section_names_str
                else []
            )

            # Get or create each project and section
            for i, project_name in enumerate(project_names):
                project_gid = project_mgr.get_or_create_project(project_name)
                if project_gid:
                    membership = {"project": project_gid}

                    # Add section if available (skip "(no section)" placeholders)
                    if i < len(section_names):
                        section_name = section_names[i]
                        if section_name and section_name not in [
                            "(no section)",
                            "None",
                            "",
                        ]:
                            section_gid = project_mgr.get_or_create_section(
                                project_gid, section_name
                            )
                            if section_gid:
                                membership["section"] = section_gid

                    memberships.append(membership)

        # Handle My Tasks sections (can exist alongside projects or standalone)
        if pd.notna(my_tasks_section_names_str) and my_tasks_section_names_str:
            my_tasks_section_names = my_tasks_section_names_str.split("|")
            mytasks_sections = [
                s
                for s in my_tasks_section_names
                if s and s not in ["(no section)", "None", ""]
            ]
        elif (
            pd.notna(section_names_str)
            and section_names_str
            and not (pd.notna(project_names_str) and project_names_str)
        ):
            # Task has no projects but has sections - these go to My Tasks (legacy fallback)
            section_names = section_names_str.split("|")
            mytasks_sections = [
                s for s in section_names if s and s not in ["(no section)", "None", ""]
            ]

        # For new tasks, try creating without memberships first to avoid 403 errors
        # Then add to projects/sections separately if needed
        # This works around potential permission issues with memberships during creation
        create_with_memberships = memberships and not existing_target_gid

        try:
            # Create or update task
            body = {"data": task_data}
            opts = {}

            # Only add memberships if we're creating a new task and have memberships
            # But first try without memberships to avoid 403 errors
            if create_with_memberships:
                # Try creating with memberships first
                body_with_memberships = {
                    "data": {**task_data, "memberships": memberships}
                }
            else:
                body_with_memberships = None

            if existing_target_gid:
                # Try to update existing task in target workspace
                # If task doesn't exist (404), create a new one instead
                try:
                    target_client._with_retry(
                        target_client.tasks.update_task,
                        task_gid=existing_target_gid,
                        body=body,
                        opts=opts,
                    )
                    target_task_gid = existing_target_gid
                    action = "Updated"
                    # Skip creation logic below
                    create_new_task = False
                except Exception as update_error:
                    # Check if it's a 404 error (task not found)
                    error_str = str(update_error)
                    status_code = None
                    if hasattr(update_error, "status"):
                        status_code = update_error.status
                    elif hasattr(update_error, "status_code"):
                        status_code = update_error.status_code

                    is_404 = (
                        (status_code == 404)
                        or ("404" in error_str)
                        or ("Not Found" in error_str)
                        or ("Unknown object" in error_str)
                    )

                    if is_404:
                        # Task doesn't exist, create a new one instead
                        print(
                            f"    ⚠ Task {existing_target_gid} not found (404), creating new task instead..."
                        )
                        create_new_task = True
                    else:
                        # Re-raise other errors
                        raise
            else:
                create_new_task = True

            if create_new_task:
                # Create new task in target workspace
                # Try with memberships first, fall back to without if 403 error
                try:
                    if body_with_memberships:
                        created_task = target_client._with_retry(
                            target_client.tasks.create_task,
                            body_with_memberships,
                            opts,
                        )
                    else:
                        created_task = target_client._with_retry(
                            target_client.tasks.create_task,
                            body,
                            opts,
                        )
                    target_task_gid = created_task.get("gid")
                    action = "Created"
                    # If we created without memberships but had them, we'll add separately below
                    if body_with_memberships and not create_with_memberships:
                        # Task was created without memberships, need to add them separately
                        memberships = memberships  # Keep original memberships for later
                except Exception as create_error:
                    # If creation fails, try fallback strategies
                    error_str = str(create_error)
                    # Check for error status code in ApiException
                    status_code = None
                    if hasattr(create_error, "status"):
                        status_code = create_error.status
                    elif hasattr(create_error, "status_code"):
                        status_code = create_error.status_code

                    is_403 = (
                        (status_code == 403)
                        or ("403" in error_str)
                        or ("Forbidden" in error_str)
                        or ("write_access_failure" in error_str)
                    )
                    is_500 = (
                        (status_code == 500)
                        or ("500" in error_str)
                        or ("Internal Server Error" in error_str)
                    )
                    is_timeout = (
                        ("timeout" in error_str.lower())
                        or ("timed out" in error_str.lower())
                        or ("TimeoutError" in error_str)
                    )

                    # Strategy 1: If html_notes was used and we got a 500 or timeout, try with plain notes first
                    if use_html_notes and (is_500 or is_timeout):
                        print(
                            f"    ⚠ Creation with html_notes failed ({'timeout' if is_timeout else '500'}), trying with plain notes..."
                        )
                        task_data_fallback = {
                            k: v for k, v in task_data.items() if k != "html_notes"
                        }
                        task_data_fallback["notes"] = notes
                        body_fallback = {"data": task_data_fallback}
                        # If we had memberships, try with fallback + memberships first
                        if body_with_memberships:
                            try:
                                body_fallback_with_memberships = {
                                    "data": {
                                        **task_data_fallback,
                                        "memberships": memberships,
                                    }
                                }
                                created_task = target_client._with_retry(
                                    target_client.tasks.create_task,
                                    body_fallback_with_memberships,
                                    opts,
                                )
                                target_task_gid = created_task.get("gid")
                                action = "Created"
                                create_with_memberships = False
                            except Exception:
                                # If that fails, try without memberships
                                created_task = target_client._with_retry(
                                    target_client.tasks.create_task,
                                    body_fallback,
                                    opts,
                                )
                                target_task_gid = created_task.get("gid")
                                action = "Created"
                                create_with_memberships = False
                        else:
                            created_task = target_client._with_retry(
                                target_client.tasks.create_task,
                                body_fallback,
                                opts,
                            )
                            target_task_gid = created_task.get("gid")
                            action = "Created"
                    # Strategy 2: If creation with memberships failed, try without memberships
                    elif body_with_memberships and is_403:
                        print(
                            "    ⚠ Creation with memberships failed (403), trying without memberships..."
                        )
                        try:
                            created_task = target_client._with_retry(
                                target_client.tasks.create_task,
                                body,
                                opts,
                            )
                            target_task_gid = created_task.get("gid")
                            action = "Created"
                            create_with_memberships = False
                        except Exception as retry_error:
                            # Strategy 2: If html_notes was used and failed, try with plain notes
                            if use_html_notes and (is_403 or is_500):
                                print(
                                    "    ⚠ Creation with html_notes failed, trying with plain notes..."
                                )
                                task_data_fallback = {
                                    k: v
                                    for k, v in task_data.items()
                                    if k != "html_notes"
                                }
                                task_data_fallback["notes"] = notes
                                body_fallback = {"data": task_data_fallback}
                                try:
                                    created_task = target_client._with_retry(
                                        target_client.tasks.create_task,
                                        body_fallback,
                                        opts,
                                    )
                                    target_task_gid = created_task.get("gid")
                                    action = "Created"
                                    create_with_memberships = False
                                except Exception as fallback_error:
                                    print(
                                        f"    ⚠ All fallback strategies failed: {fallback_error}"
                                    )
                                    raise create_error
                            else:
                                print(
                                    f"    ⚠ Retry without memberships also failed: {retry_error}"
                                )
                                raise create_error
                    # Strategy 2: If html_notes was used and failed, try with plain notes
                    elif use_html_notes and (is_403 or is_500):
                        print(
                            "    ⚠ Creation with html_notes failed, trying with plain notes..."
                        )
                        task_data_fallback = {
                            k: v for k, v in task_data.items() if k != "html_notes"
                        }
                        task_data_fallback["notes"] = notes
                        body_fallback = {"data": task_data_fallback}
                        try:
                            created_task = target_client._with_retry(
                                target_client.tasks.create_task,
                                body_fallback,
                                opts,
                            )
                            target_task_gid = created_task.get("gid")
                            action = "Created"
                        except Exception as fallback_error:
                            print(
                                f"    ⚠ Fallback to plain notes also failed: {fallback_error}"
                            )
                            raise create_error
                    else:
                        raise

            project_info = (
                f" → {project_names}"
                if (memberships and create_with_memberships)
                else ""
            )
            print(f"{action} task {target_task_gid}: {title}{project_info}")

            # Store mapping for dependencies
            local_task_id = str(row.get("task_id", ""))
            if local_task_id:
                target_task_gids_map[local_task_id] = target_task_gid
                # Store original task_id before any updates
                original_task_id_map[idx] = local_task_id

            # Add task to project sections if needed (for updates, or if creation without memberships, or if sections weren't in memberships)
            if (
                pd.notna(project_names_str)
                and project_names_str
                and pd.notna(section_names_str)
                and section_names_str
            ):
                project_names = project_names_str.split("|")
                section_names = section_names_str.split("|")

                # For each project/section pair, ensure task is in the section
                for i, project_name in enumerate(project_names):
                    if i < len(section_names):
                        section_name = section_names[i]
                        if section_name and section_name not in [
                            "(no section)",
                            "None",
                            "",
                        ]:
                            project_gid = project_mgr.get_or_create_project(
                                project_name
                            )
                            if project_gid:
                                section_gid = project_mgr.get_or_create_section(
                                    project_gid, section_name
                                )
                                if section_gid:
                                    try:
                                        # Add task to section via HTTP POST (works for both new and existing tasks)
                                        upload_url = f"https://app.asana.com/api/1.0/sections/{section_gid}/addTask"
                                        upload_headers = {
                                            "Authorization": f"Bearer {target_client._pat}"
                                        }
                                        upload_data = {
                                            "data": {"task": target_task_gid}
                                        }
                                        response = requests.post(
                                            upload_url,
                                            headers=upload_headers,
                                            json=upload_data,
                                            timeout=30,
                                        )
                                        response.raise_for_status()
                                        print(
                                            f"    Added to section: {section_name} in {project_name}"
                                        )
                                    except Exception as e:
                                        # Task might already be in section, which is fine
                                        if "already" not in str(e).lower():
                                            print(
                                                f"    Warning: Could not add to section '{section_name}' in project '{project_name}': {e}"
                                            )

            # Handle My Tasks sections (tasks without projects)
            if mytasks_sections:
                for section_name in mytasks_sections:
                    section_gid = project_mgr.get_or_create_mytasks_section(
                        config.target_workspace_gid, section_name
                    )
                    if section_gid:
                        try:
                            # Add task to My Tasks section via HTTP POST
                            upload_url = f"https://app.asana.com/api/1.0/sections/{section_gid}/addTask"
                            upload_headers = {
                                "Authorization": f"Bearer {target_client._pat}"
                            }
                            upload_data = {"data": {"task": target_task_gid}}
                            response = requests.post(
                                upload_url,
                                headers=upload_headers,
                                json=upload_data,
                                timeout=30,
                            )
                            response.raise_for_status()
                            print(f"    Added to My Tasks section: {section_name}")
                        except Exception as e:
                            print(
                                f"    Warning: Could not add to My Tasks section '{section_name}': {e}"
                            )

            # Add tags from source task if available
            if source_task_gid:
                try:
                    src_tag_opts = {"opt_fields": "tags,tags.name"}
                    src_task_with_tags = source_client._with_retry(
                        source_client.tasks.get_task, source_task_gid, src_tag_opts
                    )
                    source_tags = src_task_with_tags.get("tags", [])
                    if source_tags:
                        tag_names = [
                            tag.get("name") for tag in source_tags if tag.get("name")
                        ]
                        if tag_names:
                            add_tags_to_task(
                                target_client,
                                target_task_gid,
                                config.target_workspace_gid,
                                tag_names,
                            )
                except Exception as e:
                    print(
                        f"    Warning: Could not fetch/add tags from source task: {e}"
                    )

            # Add followers if present
            followers_gids_str = row.get("followers_gids")
            if pd.notna(followers_gids_str) and followers_gids_str:
                follower_gids = [
                    gid.strip()
                    for gid in str(followers_gids_str).split("|")
                    if gid.strip()
                ]
                if follower_gids:
                    add_followers_to_task(target_client, target_task_gid, follower_gids)

            # Set custom fields
            set_custom_fields_on_task(
                target_client,
                target_task_gid,
                local_task_id,
                config.target_workspace_gid,
            )

            # Fetch and upload attachments from source task if available
            if source_task_gid:
                # Fetch and upload attachments
                attachment_count = fetch_and_upload_attachments(
                    source_client, target_client, source_task_gid, target_task_gid
                )
                if attachment_count > 0:
                    print(f"    Uploaded {attachment_count} attachment(s)")

                # Fetch and post comments
                comment_count = fetch_and_post_comments(
                    source_client, target_client, source_task_gid, target_task_gid
                )
                if comment_count > 0:
                    print(f"    Posted {comment_count} comment(s)")
            else:
                # Task doesn't have source GID - might be locally created
                pass

            # is_new = True if task was created (not updated)
            is_new = not existing_target_gid
            created.append((idx, target_task_gid, is_new))

            # Update processed count and save checkpoint
            processed_count += 1
            current_task_id = str(row.get("task_id") or target_task_gid)

            # Save checkpoint periodically
            if processed_count % checkpoint_interval == 0:
                save_checkpoint(current_task_id, processed_count, total_tasks)
        except Exception as e:  # noqa: BLE001
            # Check error type
            error_str = str(e)

            # Check error status code
            status_code = None
            if hasattr(e, "status"):
                status_code = e.status
            elif hasattr(e, "status_code"):
                status_code = e.status_code

            is_500 = (
                (status_code == 500)
                or ("500" in error_str)
                or ("Internal Server Error" in error_str)
            )
            is_timeout = (
                ("timeout" in error_str.lower())
                or ("timed out" in error_str.lower())
                or ("TimeoutError" in error_str)
            )

            # If html_notes was used and we got a 500 or timeout error, try with plain notes
            if use_html_notes and (is_500 or is_timeout):
                error_type = "timeout" if is_timeout else "500"
                print(
                    f"⚠ Task creation with html_notes failed ({error_type}), retrying with plain notes..."
                )
                try:
                    task_data_fallback = {
                        k: v for k, v in task_data.items() if k != "html_notes"
                    }
                    task_data_fallback["notes"] = notes
                    body_fallback = {"data": task_data_fallback}

                    # Try creating without memberships first
                    if body_with_memberships:
                        try:
                            created_task = target_client._with_retry(
                                target_client.tasks.create_task,
                                body_fallback,
                                opts,
                            )
                        except Exception:
                            # If that fails, try with memberships
                            body_fallback_with_memberships = {
                                "data": {
                                    **task_data_fallback,
                                    "memberships": memberships,
                                }
                            }
                            created_task = target_client._with_retry(
                                target_client.tasks.create_task,
                                body_fallback_with_memberships,
                                opts,
                            )
                    else:
                        created_task = target_client._with_retry(
                            target_client.tasks.create_task,
                            body_fallback,
                            opts,
                        )

                    target_task_gid = created_task.get("gid")
                    action = "Created"
                    print(
                        f"{action} task {target_task_gid}: {title} (with plain notes fallback)"
                    )

                    # Continue with rest of task setup
                    local_task_id = str(row.get("task_id", ""))
                    if local_task_id:
                        target_task_gids_map[local_task_id] = target_task_gid
                        original_task_id_map[idx] = local_task_id

                    # Skip to after exception handler to continue with project/section setup
                    # We'll handle this by setting a flag and continuing
                    created.append((idx, target_task_gid, True))
                    processed_count += 1
                    current_task_id = str(row.get("task_id") or target_task_gid)
                    if processed_count % checkpoint_interval == 0:
                        save_checkpoint(current_task_id, processed_count, total_tasks)
                    continue
                except Exception as fallback_error:
                    print(f"⚠ Fallback to plain notes also failed: {fallback_error}")
                    # Fall through to general error handling

            # Check for premium-only features (402 Payment Required)
            if (
                "402" in error_str
                or "premium_only" in error_str
                or "Payment Required" in error_str
            ):
                # Try creating without premium features (start_date, etc.)
                print(
                    f"⚠ Premium feature required for task '{title}' - retrying without premium features..."
                )
                try:
                    # Remove premium features and retry
                    task_data_no_premium = {
                        k: v for k, v in task_data.items() if k not in ["start_on"]
                    }
                    body_no_premium = {"data": task_data_no_premium}
                    created_task = target_client._with_retry(
                        target_client.tasks.create_task,
                        body_no_premium,
                        opts,
                    )
                    target_task_gid = created_task.get("gid")
                    print(
                        f"Created task {target_task_gid}: {title} (without premium features)"
                    )
                    created.append((idx, target_task_gid, True))
                    processed_count += 1
                    # Continue with rest of task setup (projects, sections, etc.)
                    # Store mapping
                    local_task_id = str(row.get("task_id", ""))
                    if local_task_id:
                        target_task_gids_map[local_task_id] = target_task_gid
                        original_task_id_map[idx] = local_task_id
                    # Skip to after the exception handler to continue with project/section setup
                    continue
                except Exception as retry_error:
                    print(
                        f"⚠ Still failed after removing premium features: {retry_error}"
                    )
                    # Fall through to general error handling

            # Check if it's a 401 Unauthorized error
            if (
                "401" in error_str
                or "Unauthorized" in error_str
                or "Not Authorized" in error_str
            ):
                # 401 errors indicate authentication/authorization issues
                # This might be from source task read or target task creation
                print(
                    f"⚠ Unauthorized (401) for task '{title}' - may be due to source task access or workspace permissions"
                )
                # Don't increment processed_count - task wasn't successfully processed
                # The task will be retried on next run
                continue

            # Check if it's a 403 Forbidden error
            status_code = None
            if hasattr(e, "status"):
                status_code = e.status
            elif hasattr(e, "status_code"):
                status_code = e.status_code

            is_403 = (
                (status_code == 403)
                or ("403" in error_str)
                or ("Forbidden" in error_str)
                or ("write_access_failure" in error_str)
            )

            if is_403:
                # 403 errors indicate permission issues - log details but continue
                print(f"⚠ Permission denied (403) for task '{title}' - skipping")
                print("   This may be due to workspace permissions or task properties")
                # Update sync_log for permission errors
                if "sync_log" in df.columns:
                    df.loc[idx, "sync_log"] = "export_failed_403"
                if "sync_datetime" in df.columns:
                    df.loc[idx, "sync_datetime"] = pd.Timestamp.now(tz="UTC")
                # Don't increment processed_count for permission errors - they're not "processed"
                # The task will be retried on next run if permissions are fixed
            else:
                print(f"Error creating task '{title}': {e}")
                # Update sync_log for other errors
                error_type = "export_failed"
                if status_code:
                    error_type = f"export_failed_{status_code}"
                if "sync_log" in df.columns:
                    df.loc[idx, "sync_log"] = error_type
                if "sync_datetime" in df.columns:
                    df.loc[idx, "sync_datetime"] = pd.Timestamp.now(tz="UTC")
                # Increment processed count for other errors (task was attempted)
                processed_count += 1

    # Update children's parent_task_id to Asana GID before exporting subtasks
    # This ensures subtask lookup works correctly for already-exported parents
    if created:
        for idx, asana_gid, _ in created:
            original_task_id = original_task_id_map.get(idx)
            candidate_task_id = str(candidates.loc[idx, "task_id"])

            if not original_task_id:
                original_task_id = candidate_task_id

            # Find children by matching against:
            # 1. Original task_id (for new exports)
            # 2. Asana GID (if parent_task_id was already updated)
            # 3. For already-exported parents: find children where parent_task_id matches
            #    any task that has asana_target_gid == this parent's asana_target_gid
            children = df[
                (df["parent_task_id"].astype(str) == str(original_task_id))
                | (df["parent_task_id"].astype(str) == str(asana_gid))
            ]

            # For already-exported parents, also check if any task's parent_task_id
            # matches a task where task_id == asana_gid (parent was exported, task_id updated)
            if candidate_task_id == str(asana_gid):
                # Parent was already exported - look for children by checking if
                # any task's parent_task_id matches any task_id that has asana_target_gid == asana_gid
                # This handles the case where parent's task_id was updated to Asana GID
                # but children's parent_task_id still references the original local ID
                # We need to find the original task_id by reverse lookup
                # For now, try matching against all possible parent references
                # (This is a workaround - ideally we'd have the original task_id)
                pass  # Already covered by the match above

            if not children.empty:
                df.loc[children.index, "parent_task_id"] = asana_gid
                print(
                    f"    Updated {len(children)} child task(s) parent_task_id to {asana_gid}"
                )

    # Create dependencies after all tasks are created (so we have all target GIDs)
    if created and target_task_gids_map:
        print("\nCreating dependencies...")
        for idx, target_task_gid, _ in created:
            local_task_id = str(candidates.loc[idx, "task_id"])
            create_dependencies_for_task(
                target_client, target_task_gid, local_task_id, target_task_gids_map
            )

    # Export subtasks recursively
    if created:
        print("\nExporting subtasks...")
        subtask_created = []
        subtask_parent_map = {}  # Map local task_id -> target_gid for subtasks

        def export_subtasks_recursive(
            parent_local_id: str, parent_target_gid: str, depth: int = 0
        ):
            """Recursively export subtasks for a given parent."""
            if depth > 10:  # Safety limit to prevent infinite loops
                return

            # Find subtasks (tasks with this task as parent)
            # Match against parent_task_id, which may reference either:
            # 1. Original local task_id (for new tasks)
            # 2. Asana GID if task_id was updated to GID in previous export
            subtasks = df[
                (df["parent_task_id"].astype(str) == str(parent_local_id))
                | (df["parent_task_id"].astype(str) == str(parent_target_gid))
            ]

            if not subtasks.empty:
                indent = "  " * (depth + 1)
                print(
                    f"{indent}Found {len(subtasks)} subtask(s) for task {parent_local_id}"
                )

                for _, subtask_row in subtasks.iterrows():
                    if subtask_row["status"] not in [
                        "pending",
                        "in_progress",
                        "blocked",
                    ]:
                        continue

                    # Create subtask with parent reference
                    subtask_title = subtask_row["title"]
                    subtask_description_html = subtask_row.get("description_html")
                    subtask_description = subtask_row.get("description") or ""
                    subtask_notes = (
                        subtask_description_html
                        if subtask_description_html
                        and pd.notna(subtask_description_html)
                        else (subtask_description or subtask_row.get("notes") or "")
                    )

                    subtask_data = {
                        "name": subtask_title,
                        "workspace": config.target_workspace_gid,
                        "parent": parent_target_gid,
                    }

                    if subtask_description_html and pd.notna(subtask_description_html):
                        subtask_data["html_notes"] = subtask_description_html
                    else:
                        subtask_data["notes"] = subtask_notes

                    # Add dates
                    subtask_due_date = subtask_row.get("due_date")
                    if pd.notna(subtask_due_date):
                        try:
                            if hasattr(subtask_due_date, "isoformat"):
                                subtask_data["due_on"] = subtask_due_date.isoformat()
                            else:
                                subtask_data["due_on"] = str(subtask_due_date)
                        except Exception:
                            pass

                    # Note: start_on (start_date) requires premium Asana account
                    # We skip setting it to avoid 402 Payment Required errors
                    # If start dates are needed, they should be set manually in Asana or
                    # the workspace should be upgraded to a premium plan

                    try:
                        body = {"data": subtask_data}
                        created_subtask = target_client._with_retry(
                            target_client.tasks.create_task, body, {}
                        )
                        subtask_target_gid = created_subtask.get("gid")
                        indent = "  " * (depth + 1)
                        print(
                            f"{indent}Created subtask {subtask_target_gid}: {subtask_title}"
                        )
                        # Store the subtask index for updating the dataframe
                        subtask_created.append((subtask_row.name, subtask_target_gid))

                        # Store mapping for dependencies
                        subtask_local_id = str(subtask_row.get("task_id", ""))
                        if subtask_local_id:
                            target_task_gids_map[subtask_local_id] = subtask_target_gid
                            subtask_parent_map[subtask_local_id] = subtask_target_gid

                        # Recursively export subtasks of this subtask
                        export_subtasks_recursive(
                            subtask_local_id, subtask_target_gid, depth + 1
                        )
                    except Exception as e:
                        indent = "  " * (depth + 1)
                        print(
                            f"{indent}Warning: Could not create subtask '{subtask_title}': {e}"
                        )

        # Export subtasks for all created main tasks
        for idx, parent_target_gid, _ in created:
            # Use original task_id (before any updates) for subtask lookup
            local_task_id = original_task_id_map.get(idx)
            if not local_task_id:
                # Fallback to candidates if not in map
                # For already-exported tasks, try to find original task_id by looking up
                # tasks where asana_target_gid matches the parent_target_gid
                candidate_task_id = str(candidates.loc[idx, "task_id"])
                if candidate_task_id == str(parent_target_gid):
                    # Task was already exported - find original task_id by reverse lookup
                    # Look for tasks where asana_target_gid matches parent_target_gid
                    parent_row = df[
                        df["asana_target_gid"].astype(str) == str(parent_target_gid)
                    ]
                    if not parent_row.empty:
                        # Try to find original task_id from import_source_file or other metadata
                        # For now, use the target_gid for matching (will match if parent_task_id was updated)
                        local_task_id = str(parent_target_gid)
                    else:
                        local_task_id = candidate_task_id
                else:
                    local_task_id = candidate_task_id
            export_subtasks_recursive(local_task_id, parent_target_gid)

        # Update subtasks in local file
        if subtask_created:
            for subtask_idx, subtask_gid in subtask_created:
                df.loc[subtask_idx, "task_id"] = subtask_gid
                df.loc[subtask_idx, "import_source_file"] = "asana-post"
                df.loc[subtask_idx, "updated_at"] = pd.Timestamp.now(tz="UTC")
                df.loc[subtask_idx, "import_date"] = today

    # Update local tasks with target Asana ids and metadata
    if created:
        now_ts = pd.Timestamp.now(tz="UTC")
        for idx, asana_gid, is_new in created:
            # Always update asana_target_gid to track export to target workspace
            df.loc[idx, "asana_target_gid"] = asana_gid
            # Update sync_log and sync_datetime on successful export
            if "sync_log" in df.columns:
                df.loc[idx, "sync_log"] = "exported_success"
            if "sync_datetime" in df.columns:
                df.loc[idx, "sync_datetime"] = pd.Timestamp(now_ts)
            df.loc[idx, "updated_at"] = now_ts

            if is_new:
                # New export: update task_id and import metadata
                df.loc[idx, "task_id"] = asana_gid
                df.loc[idx, "import_source_file"] = "asana-post"
                df.loc[idx, "import_date"] = today
            # For updates, preserve original task_id (might be source workspace GID)
            # asana_target_gid already updated above

            # Update child tasks' parent_task_id to reference the Asana GID
            # This ensures subtask lookup works for already-exported tasks
            original_task_id = original_task_id_map.get(idx)
            candidate_task_id = str(candidates.loc[idx, "task_id"])

            # For already-exported tasks, task_id might be the Asana GID
            # In that case, we need to find children by looking for tasks where
            # the parent's asana_target_gid matches the child's parent_task_id
            # OR where the parent's task_id (if it's the original) matches
            if not original_task_id:
                original_task_id = candidate_task_id

            # Find all tasks that have this task as parent
            # Match against:
            # 1. Original task_id (for new exports)
            # 2. Asana GID (for already-exported parents where parent_task_id was updated)
            # 3. Parent's asana_target_gid (for already-exported parents where we need to update children)
            children = df[
                (df["parent_task_id"].astype(str) == str(original_task_id))
                | (df["parent_task_id"].astype(str) == str(asana_gid))
                | (df["parent_task_id"].astype(str) == str(candidate_task_id))
            ]
            # Also check if any tasks have this task as parent by matching asana_target_gid
            # (for cases where parent was exported but children weren't updated yet)
            if candidate_task_id == str(asana_gid):
                # Parent was already exported - also look for children by checking
                # if any task's parent_task_id matches any previous asana_target_gid
                # This is a fallback for tasks exported in previous runs
                pass  # Already covered by the match above

            if not children.empty:
                # Update their parent_task_id to the Asana GID
                df.loc[children.index, "parent_task_id"] = asana_gid
                print(
                    f"    Updated {len(children)} child task(s) parent_task_id to {asana_gid}"
                )

        df.to_parquet(TASKS_FILE, index=False)
        total_exported = len(created) + (
            len(subtask_created) if "subtask_created" in locals() else 0
        )
        print(
            f"\nSynced {total_exported} task(s) to Asana target workspace ({len(created)} main tasks + {len(subtask_created) if 'subtask_created' in locals() else 0} subtasks)."
        )

        # Save final checkpoint
        if created:
            last_task_id = str(
                candidates.iloc[-1].get("task_id")
                or candidates.iloc[-1].get("asana_target_gid")
                or ""
            )
            if last_task_id:
                save_checkpoint(last_task_id, processed_count, total_tasks)

        # Clear checkpoint on successful completion
        if processed_count >= total_tasks:
            clear_checkpoint()
            print("Export completed successfully, checkpoint cleared")
    else:
        print("\nNo tasks were created or updated in Asana; local file unchanged.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Post local tasks to Asana target workspace"
    )
    parser.add_argument(
        "--limit", type=int, default=10, help="Maximum number of tasks to post"
    )
    parser.add_argument(
        "--only-my-tasks",
        action="store_true",
        help="Only export tasks assigned to the current user",
    )
    parser.add_argument(
        "--all-assigned",
        action="store_true",
        help="Export all tasks assigned to user, regardless of status or parent_task_id",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last checkpoint (skips already exported tasks)",
    )
    parser.add_argument(
        "--checkpoint-interval",
        type=int,
        default=10,
        help="Save checkpoint every N tasks (default: 10)",
    )
    args = parser.parse_args()

    post_tasks(
        limit=args.limit,
        only_my_tasks=args.only_my_tasks,
        all_assigned=args.all_assigned,
        resume=args.resume,
        checkpoint_interval=args.checkpoint_interval,
    )


if __name__ == "__main__":
    main()
