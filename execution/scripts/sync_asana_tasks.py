#!/usr/bin/env python3
"""
Bidirectional sync between Asana workspaces (source/target) and local tasks.parquet

SYNC STRATEGY:
- Ensures all tasks exist in all three locations: local parquet, source workspace, target workspace
- Polls both source and target workspaces for tasks modified since last sync
- Uses modified_at timestamps to determine sync direction
- Syncs bidirectionally:
  * Asana → Local: Update local when Asana task modified_at > local updated_at
  * Local → Asana: Update Asana when local updated_at > Asana modified_at
  * Cross-workspace: Create tasks in workspace where they don't exist yet
- Conflict resolution: Most recent modified_at wins
- Tracks workspace association (source/target) per task with separate GIDs

EFFICIENCY:
- Only fetches tasks modified since last sync timestamp
- Skips unchanged tasks (modified_at comparison)
- Uses incremental sync to minimize API calls
- Creates missing tasks only when needed

Usage:
    python scripts/sync_asana_tasks.py              # One-time sync
    python scripts/sync_asana_tasks.py --daemon       # Continuous polling (default: 60s interval)
    python scripts/sync_asana_tasks.py --interval 300 # Custom polling interval (seconds)
    python scripts/sync_asana_tasks.py --dry-run      # Preview changes without applying
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.client import AsanaClientWrapper

# Configuration
from scripts.config import DATA_DIR, AsanaConfig
from scripts.import_asana_task_comments import (
    download_description_attachments,
    html_to_local_text,
    import_attachments_for_tasks,
    import_comments_for_tasks,
    rewrite_html_with_local_attachments,
)
from scripts.import_asana_task_metadata import (
    import_custom_fields_for_tasks,
    import_dependencies_for_tasks,
    import_stories_for_tasks,
)

TASKS_DIR = DATA_DIR / "tasks"
TASKS_FILE = TASKS_DIR / "tasks.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
LOGS_DIR = DATA_DIR / "logs"
SYNC_STATE_FILE = DATA_DIR / "logs" / "asana_sync_state.json"

# Ensure directories exist
TASKS_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Configure logging
SYNC_LOG_FILE = LOGS_DIR / "asana_sync.log"
SYNC_ERROR_LOG_FILE = LOGS_DIR / "asana_sync.error.log"


def setup_logging(debug: bool = False, daemon: bool = False):
    """Configure logging for sync operations."""
    log_level = logging.DEBUG if debug else logging.INFO

    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Root logger
    logger = logging.getLogger("asana_sync")
    logger.setLevel(log_level)
    logger.handlers.clear()  # Remove existing handlers

    # Console handler (always show INFO+)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (all levels, with rotation)
    file_handler = RotatingFileHandler(
        SYNC_LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Error file handler (ERROR+ only)
    error_handler = RotatingFileHandler(
        SYNC_ERROR_LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)

    # Suppress verbose logging from libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asana").setLevel(logging.WARNING)

    return logger


class SyncState:
    """Tracks last sync timestamps for each workspace."""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        """Load sync state from file."""
        if not self.state_file.exists():
            return {
                "source_last_sync": None,
                "target_last_sync": None,
                "last_full_sync": None,
            }

        try:
            with open(self.state_file) as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Could not load sync state: {e}")
            return {
                "source_last_sync": None,
                "target_last_sync": None,
                "last_full_sync": None,
            }

    def save(self):
        """Save sync state to file."""
        self.state["last_updated"] = datetime.now().isoformat()
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def get_last_sync(self, workspace: str) -> datetime | None:
        """Get last sync timestamp for workspace."""
        key = f"{workspace}_last_sync"
        timestamp_str = self.state.get(key)
        if not timestamp_str:
            return None
        try:
            return datetime.fromisoformat(timestamp_str)
        except Exception:
            return None

    def set_last_sync(self, workspace: str, timestamp: datetime):
        """Set last sync timestamp for workspace."""
        key = f"{workspace}_last_sync"
        self.state[key] = timestamp.isoformat()


class AsanaTaskSyncer:
    """Bidirectional sync between Asana workspaces and local parquet."""

    def __init__(
        self,
        config: AsanaConfig,
        dry_run: bool = False,
        sync_scope: str = "both",  # "both", "source", or "target"
    ):
        self.config = config
        self.dry_run = dry_run
        # sync_scope controls which remote workspaces participate in sync:
        # - "both": full bidirectional sync across source, target, and local (default)
        # - "source": sync only between local and source workspace
        # - "target": sync only between local and target workspace
        self.sync_scope = sync_scope
        self.source_client = AsanaClientWrapper.from_config_source(config)
        self.target_client = AsanaClientWrapper.from_config_target(config)
        self.sync_state = SyncState(SYNC_STATE_FILE)

        # Domain classification (reused from import script)
        self.domain_keywords = {
            "finance": [
                "tax",
                "fbar",
                "portfolio",
                "investment",
                "bank",
                "crypto",
                "money",
                "payment",
                "invoice",
                "transfer",
                "schwab",
                "coinbase",
                "kraken",
                "ibercaja",
                "capital one",
                "wealth",
                "estate",
                "notary",
                "liability",
                "insurance",
                "audit",
                "filing",
                "cadastre",
                "modelo",
                "dividend",
            ],
            "admin": [
                "utility",
                "bill",
                "subscription",
                "document",
                "certificate",
                "form",
                "registration",
                "license",
                "passport",
                "visa",
                "immigration",
                "movistar",
                "aigües",
                "electricity",
                "water",
                "gas",
                "internet",
                "phone",
            ],
            "health": [
                "workout",
                "exercise",
                "doctor",
                "medical",
                "health",
                "fitness",
                "gym",
                "yoga",
                "diet",
                "nutrition",
                "sleep",
                "checkup",
                "appointment",
                "vet",
                "therapy",
                "mental",
                "physical",
            ],
            "work": [
                "startup",
                "neotoma",
                "project",
                "meeting",
                "presentation",
                "deadline",
                "contract",
                "client",
                "team",
                "hire",
                "product",
                "launch",
                "development",
            ],
            "social": [
                "family",
                "friend",
                "gift",
                "party",
                "wedding",
                "birthday",
                "visit",
                "call",
                "dinner",
                "lunch",
                "trip",
                "vacation",
                "travel",
            ],
        }

    def fetch_tasks_modified_since(
        self, client: AsanaClientWrapper, workspace_gid: str, since: datetime | None
    ) -> list[dict]:
        """Fetch tasks modified since timestamp from workspace."""
        logger = logging.getLogger("asana_sync")
        logger.info(
            f"Fetching tasks modified since {since or 'beginning'} from workspace {workspace_gid[:8]}..."
        )

        tasks = []
        task_gids = set()
        opt_fields = [
            "gid",
            "name",
            "notes",
            "html_notes",
            "completed",
            "completed_at",
            "due_on",
            "due_at",
            "start_on",
            "created_at",
            "modified_at",
            "assignee",
            "assignee.gid",
            "assignee.name",
            "projects",
            "projects.gid",
            "projects.name",
            "projects.color",
            "projects.archived",
            "projects.public",
            "projects.icon",
            "projects.notes",
            "projects.html_notes",
            "projects.due_date",
            "projects.start_on",
            "projects.owner",
            "projects.owner.gid",
            "projects.owner.name",
            "projects.followers",
            "projects.followers.gid",
            "projects.followers.name",
            "projects.members",
            "projects.members.gid",
            "projects.members.name",
            "projects.custom_fields",
            "projects.default_view",
            "memberships",
            "memberships.project.gid",
            "memberships.project.name",
            "memberships.section.gid",
            "memberships.section.name",
            "assignee_section",
            "assignee_section.gid",
            "assignee_section.name",
            "tags",
            "tags.name",
            "permalink_url",
            "followers",
            "followers.gid",
            "followers.name",
            "custom_fields",
            "custom_fields.gid",
            "custom_fields.name",
            "custom_fields.type",
            "custom_fields.text_value",
            "custom_fields.number_value",
            "custom_fields.enum_value",
            "custom_fields.enum_value.name",
            "custom_fields.date_value",
            "custom_fields.people_value",
            "custom_fields.people_value.gid",
            "custom_fields.people_value.name",
            "custom_fields.multi_enum_values",
            "custom_fields.multi_enum_values.name",
            "dependencies",
            "dependencies.predecessor.gid",
            "dependencies.successor.gid",
        ]

        try:
            # Get all projects in workspace
            projects_opts = {"workspace": workspace_gid, "archived": False}
            projects = list(
                client._with_retry(client.projects.get_projects, projects_opts)
            )

            # Fetch tasks from each project
            for project in projects:
                project_gid = project.get("gid")
                opts = {"project": project_gid, "opt_fields": ",".join(opt_fields)}

                try:
                    project_tasks = list(
                        client._with_retry(client.tasks.get_tasks, opts)
                    )

                    for task_data in project_tasks:
                        task_gid = task_data.get("gid")
                        if task_gid in task_gids:
                            continue

                        # Filter by modified_at if since timestamp provided
                        if since:
                            modified_at_str = task_data.get("modified_at")
                            if modified_at_str:
                                try:
                                    modified_at = datetime.fromisoformat(
                                        modified_at_str.replace("Z", "+00:00")
                                    )
                                    if modified_at <= since:
                                        continue  # Skip tasks not modified since last sync
                                except Exception:
                                    pass  # Include if can't parse timestamp

                        tasks.append(task_data)
                        task_gids.add(task_gid)

                except Exception as e:
                    logger.warning(
                        f"Error fetching tasks from project {project.get('name', '')}: {e}"
                    )
                    continue

            logger.info(f"Found {len(tasks)} tasks modified since last sync")
            return tasks

        except Exception as e:
            logger.error(f"Error fetching tasks: {e}", exc_info=True)
            raise

    def fetch_subtasks_for_task(
        self, client: AsanaClientWrapper, task_gid: str
    ) -> list[dict]:
        """Fetch all subtasks for a given task."""
        opt_fields = [
            "gid",
            "name",
            "notes",
            "html_notes",
            "completed",
            "completed_at",
            "due_on",
            "due_at",
            "start_on",
            "created_at",
            "modified_at",
            "assignee",
            "assignee.gid",
            "assignee.name",
            "projects",
            "projects.gid",
            "projects.name",
            "projects.color",
            "projects.archived",
            "projects.public",
            "projects.icon",
            "projects.notes",
            "projects.html_notes",
            "projects.due_date",
            "projects.start_on",
            "projects.owner",
            "projects.owner.gid",
            "projects.owner.name",
            "projects.followers",
            "projects.followers.gid",
            "projects.followers.name",
            "projects.members",
            "projects.members.gid",
            "projects.members.name",
            "projects.custom_fields",
            "projects.default_view",
            "memberships",
            "memberships.project.gid",
            "memberships.project.name",
            "memberships.section.gid",
            "memberships.section.name",
            "assignee_section",
            "assignee_section.gid",
            "assignee_section.name",
            "tags",
            "tags.name",
            "permalink_url",
            "followers",
            "followers.gid",
            "followers.name",
            "custom_fields",
            "custom_fields.gid",
            "custom_fields.name",
            "custom_fields.type",
            "custom_fields.text_value",
            "custom_fields.number_value",
            "custom_fields.enum_value",
            "custom_fields.enum_value.name",
            "custom_fields.date_value",
            "custom_fields.people_value",
            "custom_fields.people_value.gid",
            "custom_fields.people_value.name",
            "custom_fields.multi_enum_values",
            "custom_fields.multi_enum_values.name",
            "dependencies",
            "dependencies.predecessor.gid",
            "dependencies.successor.gid",
        ]
        opts = {"opt_fields": ",".join(opt_fields)}
        try:
            subtasks = list(
                client._with_retry(client.tasks.get_subtasks_for_task, task_gid, opts)
            )
            return subtasks
        except Exception:
            # If task has no subtasks or error occurs, return empty list
            return []

    def fetch_all_subtasks_recursive(
        self,
        client: AsanaClientWrapper,
        task_gid: str,
        parent_task_id: str | None = None,
    ) -> list[tuple[dict, str]]:
        """Recursively fetch all subtasks at all levels.

        Returns list of tuples: (subtask_data, parent_task_id)
        """
        all_subtasks = []
        direct_subtasks = self.fetch_subtasks_for_task(client, task_gid)

        for subtask_data in direct_subtasks:
            subtask_gid = subtask_data.get("gid")
            if not subtask_gid:
                continue

            # Add this subtask with its parent
            all_subtasks.append((subtask_data, parent_task_id or task_gid))

            # Recursively fetch subtasks of this subtask
            nested_subtasks = self.fetch_all_subtasks_recursive(
                client, subtask_gid, parent_task_id=subtask_gid
            )
            all_subtasks.extend(nested_subtasks)

        return all_subtasks

    def normalize_asana_task(
        self, task_data: dict, workspace: str, parent_task_id: str | None = None
    ) -> dict:
        """Normalize Asana task to tasks schema."""
        gid = task_data.get("gid", "")
        title = task_data.get("name", "")
        notes = task_data.get("notes", "") or ""
        html_notes = task_data.get("html_notes") or None
        completed = task_data.get("completed", False)

        # Always ensure title is set - generate from description if empty
        if not title or title.strip() == "":
            # Try to generate title from description/notes
            if notes and notes.strip():
                # Use first sentence or first 60 chars of description
                first_sentence = notes.split(".")[0].strip()
                if first_sentence and len(first_sentence) > 3:
                    if len(first_sentence) > 60:
                        title = first_sentence[:57] + "..."
                    else:
                        title = first_sentence
                else:
                    # If first sentence is too short, use first 60 chars of full notes
                    title = (
                        notes.strip()[:60]
                        if len(notes.strip()) > 3
                        else "Untitled Task"
                    )
            elif html_notes:
                # Extract text from HTML and use first sentence
                # Simple HTML tag removal for title generation
                text = re.sub(r"<[^>]+>", "", html_notes).strip()
                if text and len(text) > 3:
                    first_sentence = text.split(".")[0].strip()
                    if first_sentence and len(first_sentence) > 3:
                        if len(first_sentence) > 60:
                            title = first_sentence[:57] + "..."
                        else:
                            title = first_sentence
                    else:
                        # Use first 60 chars if no sentence break
                        title = text[:60] if len(text) > 3 else "Untitled Task"
                else:
                    title = "Untitled Task"
            else:
                title = "Untitled Task"

        # Parse dates
        due_on = task_data.get("due_on")
        due_date = datetime.fromisoformat(due_on).date() if due_on else None

        start_on = task_data.get("start_on")
        start_date = datetime.fromisoformat(start_on).date() if start_on else None

        completed_at = task_data.get("completed_at")
        completed_date = (
            datetime.fromisoformat(completed_at).date() if completed_at else None
        )

        created_at_str = task_data.get("created_at")
        created_at = (
            datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if created_at_str
            else None
        )

        modified_at_str = task_data.get("modified_at")
        updated_at = (
            datetime.fromisoformat(modified_at_str.replace("Z", "+00:00"))
            if modified_at_str
            else None
        )

        # Extract tags
        [tag.get("name", "") for tag in task_data.get("tags", [])]

        # Extract project/section info
        projects = task_data.get("projects", [])
        project_ids = [p.get("gid") for p in projects if p.get("gid")]
        project_names = [p.get("name") for p in projects if p.get("name")]

        # Project sections from memberships
        memberships = task_data.get("memberships", [])
        section_ids = []
        section_names = []
        for m in memberships:
            section = m.get("section")
            if not section:
                continue
            section_gid = section.get("gid")
            section_name = section.get("name")
            if section_gid:
                section_ids.append(section_gid)
            if section_name:
                section_names.append(section_name)

        # Extract assignee information
        assignee = task_data.get("assignee") or {}
        assignee_gid = assignee.get("gid") if assignee else None
        assignee_name = assignee.get("name") if assignee else None

        # Assignee-specific My Tasks section
        my_tasks_section_ids = []
        my_tasks_section_names = []
        assignee_section = task_data.get("assignee_section") or {}
        if assignee_section:
            if assignee_section.get("gid"):
                my_tasks_section_ids.append(assignee_section["gid"])
            if assignee_section.get("name"):
                my_tasks_section_names.append(assignee_section["name"])

        # Extract followers
        followers = task_data.get("followers", [])
        follower_gids = [f.get("gid") for f in followers if f.get("gid")]
        follower_names = [f.get("name") for f in followers if f.get("name")]

        # Extract permalink URL
        permalink_url = task_data.get("permalink_url")

        # Compute classifications
        domain = self._classify_domain(title, notes)

        # Determine status
        if completed:
            status = "completed"
        else:
            status = "pending"

        # Track GIDs for both workspaces
        asana_source_gid = gid if workspace == "source" else None
        asana_target_gid = gid if workspace == "target" else None

        # Determine actual workspace GID
        if workspace == "source":
            actual_workspace_gid = self.config.source_workspace_gid
        elif workspace == "target":
            actual_workspace_gid = self.config.target_workspace_gid
        else:
            actual_workspace_gid = None

        # Use literal Asana GID; no prefixes. Asana-specific identity is tracked
        # via asana_source_gid / asana_target_gid.
        return {
            "task_id": gid,
            "title": title,
            "description": notes,
            "description_html": None,  # Will be rewritten to local paths after download
            "description_html_remote": html_notes,
            "domain": domain,
            "status": status,
            "due_date": due_date,
            "start_date": start_date,
            "completed_date": completed_date,
            "recurrence": None,
            "execution_plan_path": None,
            "notes": f"Synced from Asana {workspace} workspace (gid: {gid})",
            "project_ids": "|".join(project_ids) if project_ids else None,
            "project_names": "|".join(project_names) if project_names else None,
            "section_ids": "|".join(section_ids) if section_ids else None,
            "section_names": "|".join(section_names) if section_names else None,
            "my_tasks_section_ids": (
                "|".join(my_tasks_section_ids) if my_tasks_section_ids else None
            ),
            "my_tasks_section_names": (
                "|".join(my_tasks_section_names) if my_tasks_section_names else None
            ),
            "assignee_gid": assignee_gid,
            "assignee_name": assignee_name,
            "created_at": created_at,
            "updated_at": updated_at,
            "asana_workspace": actual_workspace_gid,  # Store actual workspace GID
            "asana_source_gid": asana_source_gid,
            "asana_target_gid": asana_target_gid,
            "parent_task_id": parent_task_id,
            "permalink_url": permalink_url,
            "followers_gids": "|".join(follower_gids) if follower_gids else None,
            "follower_names": "|".join(follower_names) if follower_names else None,
            "import_date": datetime.now().date(),
            "import_source_file": f"asana_sync_{workspace}",
        }

    def _classify_domain(self, title: str, notes: str) -> str:
        """Classify task domain."""
        text = f"{title} {notes}".lower()
        domain_scores = {}
        for domain, keywords in self.domain_keywords.items():
            score = sum(1 for keyword in keywords if keyword in text)
            if score > 0:
                domain_scores[domain] = score
        return max(domain_scores, key=domain_scores.get) if domain_scores else "other"

    def create_asana_task(
        self, client: AsanaClientWrapper, workspace_gid: str, local_task: dict
    ) -> str | None:
        """Create Asana task from local task data. Returns task GID if successful."""
        try:
            # Build create payload
            task_data = {
                "name": local_task.get("title", ""),
                "notes": local_task.get("description", "") or "",
                "workspace": workspace_gid,
            }

            # Add due date if present
            due_date = local_task.get("due_date")
            if due_date and pd.notna(due_date):
                if isinstance(due_date, str):
                    task_data["due_on"] = due_date
                elif hasattr(due_date, "isoformat"):
                    task_data["due_on"] = due_date.isoformat()

            # Add start date if present
            start_date = local_task.get("start_date")
            if start_date and pd.notna(start_date):
                if isinstance(start_date, str):
                    task_data["start_on"] = start_date
                elif hasattr(start_date, "isoformat"):
                    task_data["start_on"] = start_date.isoformat()

            # Set completion status
            status = local_task.get("status", "pending")
            if status == "completed":
                task_data["completed"] = True

            body = {"data": task_data}
            opts = {}

            if not self.dry_run:
                created_task = client._with_retry(client.tasks.create_task, body, opts)
                return created_task.get("gid")
            else:
                return f"dry-run-gid-{local_task.get('task_id', 'unknown')}"

        except Exception as e:
            print(f"Error creating Asana task '{local_task.get('title', '')}': {e}")
            return None

    def update_asana_task(
        self, client: AsanaClientWrapper, task_gid: str, local_task: dict
    ) -> bool:
        """Update Asana task with local changes."""
        try:
            # Build update payload
            update_data = {
                "name": local_task.get("title", ""),
                "notes": local_task.get("description", "") or "",
            }

            # Add due date if present
            due_date = local_task.get("due_date")
            if due_date and pd.notna(due_date):
                if isinstance(due_date, str):
                    update_data["due_on"] = due_date
                elif hasattr(due_date, "isoformat"):
                    update_data["due_on"] = due_date.isoformat()
            else:
                # Clear due date if not set
                update_data["due_on"] = None

            # Add start date if present
            start_date = local_task.get("start_date")
            if start_date and pd.notna(start_date):
                if isinstance(start_date, str):
                    update_data["start_on"] = start_date
                elif hasattr(start_date, "isoformat"):
                    update_data["start_on"] = start_date.isoformat()
            else:
                # Clear start date if not set
                update_data["start_on"] = None

            # Update completion status
            status = local_task.get("status", "pending")
            if status == "completed":
                update_data["completed"] = True
            else:
                update_data["completed"] = False

            body = {"data": update_data}
            opts = {}

            if not self.dry_run:
                client._with_retry(client.tasks.update_task, task_gid, body, opts)

            return True
        except Exception as e:
            logger = logging.getLogger("asana_sync")
            logger.error(f"Error updating Asana task {task_gid}: {e}", exc_info=True)
            return False

    def sync_workspace_to_local(
        self, client: AsanaClientWrapper, workspace_gid: str, workspace_name: str
    ) -> tuple[int, int]:
        """Sync tasks from Asana workspace to local parquet."""
        last_sync = self.sync_state.get_last_sync(workspace_name)

        # Fetch modified tasks
        asana_tasks = self.fetch_tasks_modified_since(client, workspace_gid, last_sync)

        if not asana_tasks:
            return 0, 0

        # Load local tasks
        if not TASKS_FILE.exists():
            local_df = pd.DataFrame()
        else:
            local_df = pd.read_parquet(TASKS_FILE)

        updated_count = 0
        new_count = 0
        latest_modified = last_sync
        # Track tasks we actually touched so we can import their comments
        touched_task_gids: set[str] = set()

        # Normalize and merge Asana tasks
        for task_data in asana_tasks:
            normalized = self.normalize_asana_task(task_data, workspace_name)
            task_id = normalized["task_id"]
            task_gid = task_data.get("gid", "")
            asana_modified = normalized["updated_at"]

            # Download description attachments and rewrite HTML
            description_attachment_map = download_description_attachments(
                client, task_gid, normalized.get("description_html_remote")
            )
            normalized["description_html"] = rewrite_html_with_local_attachments(
                normalized.get("description_html_remote"), description_attachment_map
            )
            if normalized.get("description_html"):
                normalized["description"] = html_to_local_text(
                    normalized["description_html"]
                )

            if asana_modified and (
                not latest_modified or asana_modified > latest_modified
            ):
                latest_modified = asana_modified

            # Check if task exists locally by GID
            if not local_df.empty and task_id in local_df["task_id"].values:
                # Compare timestamps
                local_row = local_df[local_df["task_id"] == task_id].iloc[0]
                local_updated = local_row.get("updated_at")

                if isinstance(local_updated, str):
                    try:
                        local_updated = datetime.fromisoformat(local_updated)
                    except Exception:
                        local_updated = None

                # Conflict resolution: most recent wins
                if local_updated and asana_modified:
                    if local_updated > asana_modified:
                        # Local is newer - skip Asana update, will sync to Asana later
                        continue

                # Update local task
                idx = local_df[local_df["task_id"] == task_id].index[0]
                local_df.loc[idx, "title"] = normalized["title"]
                local_df.loc[idx, "description"] = normalized["description"]
                local_df.loc[idx, "description_html"] = normalized["description_html"]
                local_df.loc[idx, "description_html_remote"] = normalized[
                    "description_html_remote"
                ]
                local_df.loc[idx, "status"] = normalized["status"]
                local_df.loc[idx, "due_date"] = normalized["due_date"]
                local_df.loc[idx, "start_date"] = normalized["start_date"]
                local_df.loc[idx, "completed_date"] = normalized["completed_date"]
                local_df.loc[idx, "updated_at"] = normalized["updated_at"]
                if workspace_name == "source":
                    local_df.loc[
                        idx, "asana_workspace"
                    ] = self.config.source_workspace_gid
                elif workspace_name == "target":
                    local_df.loc[
                        idx, "asana_workspace"
                    ] = self.config.target_workspace_gid
                local_df.loc[idx, "project_ids"] = normalized["project_ids"]
                local_df.loc[idx, "project_names"] = normalized["project_names"]
                local_df.loc[idx, "section_ids"] = normalized["section_ids"]
                local_df.loc[idx, "section_names"] = normalized["section_names"]

                # Update GID tracking for this workspace
                if workspace_name == "source":
                    local_df.loc[idx, "asana_source_gid"] = task_gid
                elif workspace_name == "target":
                    local_df.loc[idx, "asana_target_gid"] = task_gid

                updated_count += 1
                if task_gid:
                    touched_task_gids.add(str(task_gid))
            else:
                # Check if this task exists in the other workspace (by title/content matching)
                # This handles the case where a task exists in both workspaces
                matching_task = None
                if not local_df.empty:
                    title_match = local_df[
                        (local_df["title"] == normalized["title"])
                        & (local_df["title"].notna())
                    ]
                    # Prefer exact title match, but could also match by content similarity
                    if not title_match.empty:
                        # Check if it's in the other workspace
                        other_workspace = (
                            "target" if workspace_name == "source" else "source"
                        )
                        other_workspace_tasks = title_match[
                            (
                                title_match["asana_workspace"]
                                == (
                                    self.config.source_workspace_gid
                                    if other_workspace == "source"
                                    else self.config.target_workspace_gid
                                )
                            )
                            | (
                                (workspace_name == "source")
                                & (title_match["asana_source_gid"].notna())
                            )
                            | (
                                (workspace_name == "target")
                                & (title_match["asana_target_gid"].notna())
                            )
                        ]
                        if not other_workspace_tasks.empty:
                            matching_task = other_workspace_tasks.iloc[0]

                if matching_task is not None:
                    # Task exists in other workspace - update to track both GIDs
                    idx = matching_task.name
                    local_df.loc[idx, "title"] = normalized["title"]
                    local_df.loc[idx, "description"] = normalized["description"]
                    local_df.loc[idx, "status"] = normalized["status"]
                    local_df.loc[idx, "due_date"] = normalized["due_date"]
                    local_df.loc[idx, "start_date"] = normalized["start_date"]
                    local_df.loc[idx, "completed_date"] = normalized["completed_date"]
                    local_df.loc[idx, "updated_at"] = normalized["updated_at"]
                    # Keep existing workspace GID if already set, or set to this one
                    if pd.isna(local_df.loc[idx, "asana_workspace"]):
                        if workspace_name == "source":
                            local_df.loc[
                                idx, "asana_workspace"
                            ] = self.config.source_workspace_gid
                        elif workspace_name == "target":
                            local_df.loc[
                                idx, "asana_workspace"
                            ] = self.config.target_workspace_gid

                    # Update GID tracking
                    if workspace_name == "source":
                        local_df.loc[idx, "asana_source_gid"] = task_gid
                        # Update task_id to use source GID if not set
                        if not str(local_df.loc[idx, "task_id"]).startswith("asana-"):
                            local_df.loc[idx, "task_id"] = f"asana-{task_gid}"
                    elif workspace_name == "target":
                        local_df.loc[idx, "asana_target_gid"] = task_gid

                    updated_count += 1
                    if task_gid:
                        touched_task_gids.add(str(task_gid))
                else:
                    # Truly new task
                    new_row = pd.DataFrame([normalized])
                    local_df = pd.concat([local_df, new_row], ignore_index=True)
                    new_count += 1
                    if task_gid:
                        touched_task_gids.add(str(task_gid))

            # Recursively fetch and process all subtasks at all levels
            try:
                all_subtasks = self.fetch_all_subtasks_recursive(client, task_gid)
                for subtask_data, parent_id in all_subtasks:
                    subtask_normalized = self.normalize_asana_task(
                        subtask_data, workspace_name, parent_task_id=parent_id
                    )
                    subtask_id = subtask_normalized["task_id"]
                    subtask_gid = subtask_data.get("gid", "")
                    subtask_modified = subtask_normalized["updated_at"]

                    # Download description attachments and rewrite HTML for subtask
                    subtask_attachment_map = download_description_attachments(
                        client,
                        subtask_gid,
                        subtask_normalized.get("description_html_remote"),
                    )
                    subtask_normalized[
                        "description_html"
                    ] = rewrite_html_with_local_attachments(
                        subtask_normalized.get("description_html_remote"),
                        subtask_attachment_map,
                    )
                    if subtask_normalized.get("description_html"):
                        subtask_normalized["description"] = html_to_local_text(
                            subtask_normalized["description_html"]
                        )

                    if subtask_modified and (
                        not latest_modified or subtask_modified > latest_modified
                    ):
                        latest_modified = subtask_modified

                    # Check if subtask exists locally
                    if not local_df.empty and subtask_id in local_df["task_id"].values:
                        # Update existing subtask
                        idx = local_df[local_df["task_id"] == subtask_id].index[0]
                        local_df.loc[idx, "title"] = subtask_normalized["title"]
                        local_df.loc[idx, "description"] = subtask_normalized[
                            "description"
                        ]
                        local_df.loc[idx, "description_html"] = subtask_normalized[
                            "description_html"
                        ]
                        local_df.loc[
                            idx, "description_html_remote"
                        ] = subtask_normalized["description_html_remote"]
                        local_df.loc[idx, "status"] = subtask_normalized["status"]
                        local_df.loc[idx, "due_date"] = subtask_normalized["due_date"]
                        local_df.loc[idx, "start_date"] = subtask_normalized[
                            "start_date"
                        ]
                        local_df.loc[idx, "completed_date"] = subtask_normalized[
                            "completed_date"
                        ]
                        local_df.loc[idx, "updated_at"] = subtask_normalized[
                            "updated_at"
                        ]
                        local_df.loc[idx, "parent_task_id"] = subtask_normalized[
                            "parent_task_id"
                        ]
                        updated_count += 1
                        if subtask_gid:
                            touched_task_gids.add(str(subtask_gid))
                    else:
                        # New subtask
                        subtask_row = pd.DataFrame([subtask_normalized])
                        local_df = pd.concat([local_df, subtask_row], ignore_index=True)
                        new_count += 1
                        if subtask_gid:
                            touched_task_gids.add(str(subtask_gid))
            except Exception as e:
                logger = logging.getLogger("asana_sync")
                logger.warning(f"Error fetching subtasks for task {task_gid}: {e}")
                continue

        # Save local tasks
        if not self.dry_run and (updated_count > 0 or new_count > 0):
            # Create snapshot
            if TASKS_FILE.exists():
                timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
                snapshot_file = SNAPSHOTS_DIR / f"tasks-{timestamp}.parquet"
                pd.read_parquet(TASKS_FILE).to_parquet(snapshot_file, index=False)

            local_df.to_parquet(TASKS_FILE, index=False)

            # Update sync state
            if latest_modified:
                self.sync_state.set_last_sync(workspace_name, latest_modified)
                self.sync_state.save()

            # Always import comments for any tasks we touched this sync
            if touched_task_gids:
                logger = logging.getLogger("asana_sync")
                try:
                    comment_count = import_comments_for_tasks(
                        client, workspace_name, sorted(touched_task_gids)
                    )
                    logger.info(
                        f"Imported {comment_count} comment(s) for "
                        f"{len(touched_task_gids)} task(s) in workspace {workspace_name}"
                    )
                except Exception as e:
                    logger.error(
                        f"Error importing comments for tasks in workspace {workspace_name}: {e}",
                        exc_info=True,
                    )
                # Import attachments for the same set of tasks
                try:
                    attachment_count = import_attachments_for_tasks(
                        client, workspace_name, sorted(touched_task_gids)
                    )
                    logger.info(
                        f"Imported {attachment_count} attachment(s) for "
                        f"{len(touched_task_gids)} task(s) in workspace {workspace_name}"
                    )
                except Exception as e:
                    logger.error(
                        f"Error importing attachments for tasks in workspace {workspace_name}: {e}",
                        exc_info=True,
                    )
                # Import custom fields, dependencies, and all stories
                try:
                    custom_fields_count = import_custom_fields_for_tasks(
                        client, workspace_name, sorted(touched_task_gids)
                    )
                    logger.info(
                        f"Imported {custom_fields_count} custom field(s) for "
                        f"{len(touched_task_gids)} task(s) in workspace {workspace_name}"
                    )
                except Exception as e:
                    logger.error(
                        f"Error importing custom fields for tasks in workspace {workspace_name}: {e}",
                        exc_info=True,
                    )
                try:
                    dependencies_count = import_dependencies_for_tasks(
                        client, workspace_name, sorted(touched_task_gids)
                    )
                    logger.info(
                        f"Imported {dependencies_count} dependency/dependencies for "
                        f"{len(touched_task_gids)} task(s) in workspace {workspace_name}"
                    )
                except Exception as e:
                    logger.error(
                        f"Error importing dependencies for tasks in workspace {workspace_name}: {e}",
                        exc_info=True,
                    )
                try:
                    stories_count = import_stories_for_tasks(
                        client, workspace_name, sorted(touched_task_gids)
                    )
                    logger.info(
                        f"Imported {stories_count} story/stories for "
                        f"{len(touched_task_gids)} task(s) in workspace {workspace_name}"
                    )
                except Exception as e:
                    logger.error(
                        f"Error importing stories for tasks in workspace {workspace_name}: {e}",
                        exc_info=True,
                    )

        return updated_count, new_count

    def sync_local_to_workspace(
        self, client: AsanaClientWrapper, workspace_gid: str, workspace_name: str
    ) -> tuple[int, int]:
        """Sync local tasks to Asana workspace.

        Returns: (updated_count, created_count)
        Creates tasks in this workspace if they don't exist yet.
        """
        if not TASKS_FILE.exists():
            return 0, 0

        local_df = pd.read_parquet(TASKS_FILE)

        # Get all Asana-backed tasks (those with workspace GIDs)
        asana_tasks = local_df[
            (local_df["asana_source_gid"].notna())
            | (local_df["asana_target_gid"].notna())
        ]

        if asana_tasks.empty:
            return 0, 0

        if workspace_name == "source":
            gid_col = "asana_source_gid"
        else:  # target
            gid_col = "asana_target_gid"

        updated_count = 0
        created_count = 0

        for _, row in asana_tasks.iterrows():
            # Get GID for this workspace
            task_gid = None
            if pd.notna(row.get(gid_col)):
                task_gid = str(row[gid_col])

            local_updated = row.get("updated_at")

            if isinstance(local_updated, str):
                try:
                    local_updated = datetime.fromisoformat(local_updated)
                except Exception:
                    local_updated = None

            if task_gid:
                # Task exists in this workspace - update if needed
                if not local_updated:
                    continue

                # Fetch current Asana task to compare timestamps
                try:
                    opts = {"opt_fields": "modified_at"}
                    asana_task = client._with_retry(
                        client.tasks.get_task, task_gid, opts
                    )
                    asana_modified_str = asana_task.get("modified_at")

                    if asana_modified_str:
                        asana_modified = datetime.fromisoformat(
                            asana_modified_str.replace("Z", "+00:00")
                        )

                        # Only sync if local is newer
                        if local_updated <= asana_modified:
                            continue  # Asana is newer, skip

                    # Update Asana task
                    if self.update_asana_task(client, task_gid, row.to_dict()):
                        updated_count += 1
                        logger = logging.getLogger("asana_sync")
                        logger.info(
                            f"Updated local → Asana {workspace_name}: {row['title'][:50]}"
                        )

                except Exception as e:
                    logger = logging.getLogger("asana_sync")
                    logger.warning(
                        f"Could not update task {task_gid} in {workspace_name}: {e}",
                        exc_info=True,
                    )
                    continue
            else:
                # Task doesn't exist in this workspace - create it
                # Only create if task has a title
                if pd.isna(row.get("title")) or not str(row.get("title", "")).strip():
                    continue

                new_gid = self.create_asana_task(client, workspace_gid, row.to_dict())
                if new_gid:
                    created_count += 1
                    logger = logging.getLogger("asana_sync")
                    logger.info(
                        f"Created task in {workspace_name}: {row['title'][:50]}"
                    )

                    # Update local record with new GID
                    if not self.dry_run:
                        idx = row.name
                        local_df.loc[idx, gid_col] = new_gid
                        # If task_id is empty, set it to this workspace's GID
                        if (
                            workspace_name == "source"
                            or not str(local_df.loc[idx, "task_id"]).strip()
                        ):
                            local_df.loc[idx, "task_id"] = new_gid
                        if workspace_name == "source":
                            local_df.loc[
                                idx, "asana_workspace"
                            ] = self.config.source_workspace_gid
                        elif workspace_name == "target":
                            local_df.loc[
                                idx, "asana_workspace"
                            ] = self.config.target_workspace_gid

        # Save updated local tasks if any were created
        if not self.dry_run and created_count > 0:
            # Create snapshot
            if TASKS_FILE.exists():
                timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
                snapshot_file = SNAPSHOTS_DIR / f"tasks-{timestamp}.parquet"
                pd.read_parquet(TASKS_FILE).to_parquet(snapshot_file, index=False)

            local_df.to_parquet(TASKS_FILE, index=False)

        return updated_count, created_count

    def ensure_cross_workspace_sync(self) -> dict[str, int]:
        """Ensure tasks exist in both workspaces.

        After syncing from workspaces, check if tasks need to be created
        in the other workspace to ensure all tasks exist everywhere.
        """
        if not TASKS_FILE.exists():
            return {"source_created": 0, "target_created": 0}

        local_df = pd.read_parquet(TASKS_FILE)

        # Find tasks that exist in one workspace but not the other
        source_only = local_df[
            (local_df["asana_source_gid"].notna())
            & (local_df["asana_target_gid"].isna())
        ]

        target_only = local_df[
            (local_df["asana_target_gid"].notna())
            & (local_df["asana_source_gid"].isna())
        ]

        source_created = 0
        target_created = 0

        # Create missing tasks in target workspace
        for _, row in source_only.iterrows():
            if pd.isna(row.get("title")) or not str(row.get("title", "")).strip():
                continue

            new_gid = self.create_asana_task(
                self.target_client, self.config.target_workspace_gid, row.to_dict()
            )
            if new_gid:
                target_created += 1
                logger = logging.getLogger("asana_sync")
                logger.info(f"Created in target: {row['title'][:50]}")

                if not self.dry_run:
                    idx = row.name
                    local_df.loc[idx, "asana_target_gid"] = new_gid

        # Create missing tasks in source workspace
        for _, row in target_only.iterrows():
            if pd.isna(row.get("title")) or not str(row.get("title", "")).strip():
                continue

            new_gid = self.create_asana_task(
                self.source_client, self.config.source_workspace_gid, row.to_dict()
            )
            if new_gid:
                source_created += 1
                logger = logging.getLogger("asana_sync")
                logger.info(f"Created in source: {row['title'][:50]}")

                if not self.dry_run:
                    idx = row.name
                    local_df.loc[idx, "asana_source_gid"] = new_gid
                    # If task_id is empty, set it to source GID
                    if not str(local_df.loc[idx, "task_id"]).strip():
                        local_df.loc[idx, "task_id"] = new_gid
                    local_df.loc[idx, "asana_workspace"] = "source"

        # Save updated local tasks if any were created
        if not self.dry_run and (source_created > 0 or target_created > 0):
            # Create snapshot
            if TASKS_FILE.exists():
                timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
                snapshot_file = SNAPSHOTS_DIR / f"tasks-{timestamp}.parquet"
                pd.read_parquet(TASKS_FILE).to_parquet(snapshot_file, index=False)

            local_df.to_parquet(TASKS_FILE, index=False)

        return {"source_created": source_created, "target_created": target_created}

    def sync(self) -> dict[str, Any]:
        """Perform bidirectional sync ensuring all tasks exist in local, source, and target."""
        logger = logging.getLogger("asana_sync")
        logger.info("=== Starting Asana Task Sync ===")

        stats: dict[str, Any] = {
            "source_to_local": {"updated": 0, "new": 0},
            "target_to_local": {"updated": 0, "new": 0},
            "local_to_source": {"updated": 0, "created": 0},
            "local_to_target": {"updated": 0, "created": 0},
            "cross_workspace": {"source_created": 0, "target_created": 0},
            "sync_scope": self.sync_scope,
        }

        try:
            # Determine which directions to sync based on scope
            do_source = self.sync_scope in ("both", "source")
            do_target = self.sync_scope in ("both", "target")

            # Sync source workspace ↔ local
            if do_source:
                logger.info("Syncing source workspace → local...")
                updated, new = self.sync_workspace_to_local(
                    self.source_client, self.config.source_workspace_gid, "source"
                )
                stats["source_to_local"] = {"updated": updated, "new": new}
                logger.info(f"Source → Local: {updated} updated, {new} new")

            # Sync target workspace ↔ local
            if do_target:
                logger.info("Syncing target workspace → local...")
                updated, new = self.sync_workspace_to_local(
                    self.target_client, self.config.target_workspace_gid, "target"
                )
                stats["target_to_local"] = {"updated": updated, "new": new}
                logger.info(f"Target → Local: {updated} updated, {new} new")

            # Ensure tasks exist in both workspaces (cross-workspace sync) only when both are in scope
            if do_source and do_target:
                logger.info("Ensuring tasks exist in both workspaces...")
                cross_stats = self.ensure_cross_workspace_sync()
                stats["cross_workspace"] = cross_stats
                logger.info(
                    f"Cross-workspace: {cross_stats['source_created']} created in source, "
                    f"{cross_stats['target_created']} created in target"
                )
            else:
                logger.info("Cross-workspace sync skipped due to sync_scope != 'both'")

            # Sync local → source workspace
            if do_source:
                logger.info("Syncing local → source workspace...")
                updated, created = self.sync_local_to_workspace(
                    self.source_client, self.config.source_workspace_gid, "source"
                )
                stats["local_to_source"] = {"updated": updated, "created": created}
                logger.info(f"Local → Source: {updated} updated, {created} created")

            # Sync local → target workspace
            if do_target:
                logger.info("Syncing local → target workspace...")
                updated, created = self.sync_local_to_workspace(
                    self.target_client, self.config.target_workspace_gid, "target"
                )
                stats["local_to_target"] = {"updated": updated, "created": created}
                logger.info(f"Local → Target: {updated} updated, {created} created")

            logger.info("=== Sync Complete ===")

        except Exception as e:
            logger.error(f"Error during sync: {e}", exc_info=True)
            raise

        return stats


def main():
    parser = argparse.ArgumentParser(
        description="Bidirectional sync between Asana workspaces and local tasks"
    )
    parser.add_argument(
        "--daemon", action="store_true", help="Run continuously with polling"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Polling interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview changes without applying"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--sync-scope",
        choices=["both", "source", "target"],
        default="both",
        help="Which remote workspaces to sync with local: "
        '"both" (default), "source" (local+source only), or "target" (local+target only)',
    )

    args = parser.parse_args()

    # Setup logging
    logger = setup_logging(debug=args.debug, daemon=args.daemon)

    try:
        config = AsanaConfig.from_env()
        syncer = AsanaTaskSyncer(
            config, dry_run=args.dry_run, sync_scope=args.sync_scope
        )

        if args.daemon:
            logger.info(f"Starting daemon mode (interval: {args.interval}s)")
            logger.info("Press Ctrl+C to stop")
            logger.info(f"Logs: {SYNC_LOG_FILE}")
            logger.info(f"Errors: {SYNC_ERROR_LOG_FILE}")

            try:
                while True:
                    syncer.sync()
                    logger.debug(f"Waiting {args.interval} seconds until next sync...")
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                logger.info("Stopping daemon...")
        else:
            stats = syncer.sync()
            logger.info("=== Sync Summary ===")
            logger.info(f"Sync scope: {stats.get('sync_scope')}")
            logger.info(
                f"Source → Local: {stats['source_to_local']['updated']} updated, {stats['source_to_local']['new']} new"
            )
            logger.info(
                f"Target → Local: {stats['target_to_local']['updated']} updated, {stats['target_to_local']['new']} new"
            )
            logger.info(
                f"Cross-workspace: {stats['cross_workspace']['source_created']} created in source, {stats['cross_workspace']['target_created']} created in target"
            )
            logger.info(
                f"Local → Source: {stats['local_to_source']['updated']} updated, {stats['local_to_source']['created']} created"
            )
            logger.info(
                f"Local → Target: {stats['local_to_target']['updated']} updated, {stats['local_to_target']['created']} created"
            )

    except Exception as e:
        logger.error(f"Error during sync: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
