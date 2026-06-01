#!/usr/bin/env python3
"""
Import tasks directly from Asana API to tasks.parquet

Fetches tasks from Asana workspace, normalizes them with domain classification,
and intelligently merges with existing tasks in data/tasks/tasks.parquet.

INTELLIGENT MERGE STRATEGY:
- Asana source fields (always updated): title, description, status, dates, project/section info
- Classification fields (preserved unless --recalculate): domain
- Local-only fields (never overwritten): execution_plan_path, manually modified notes, recurrence
- Unfetched tasks: Preserved unchanged (partial imports don't delete unfetched tasks)

BEHAVIOR:
- Creates parquet snapshot before any modifications
- Fetches from both non-archived and archived projects by default
- Deduplicates tasks across multiple projects
- Merges fetched tasks with existing data field-by-field
- Preserves all existing tasks not fetched in current run
- Saves checkpoint every N tasks (default: 100) for resume capability
- Supports batch downloading of attachments using ThreadPoolExecutor

Usage:
    python scripts/import_asana_tasks.py [options]

Options:
    --only-incomplete       Only fetch incomplete tasks from Asana
    --assignee-gid GID      Only fetch tasks assigned to this user
    --max-tasks N           Limit number of tasks to fetch (for testing)
    --recalculate           Force recalculation of domain for fetched tasks
    --resume                Resume from last checkpoint (saves progress every 100 tasks)
    --checkpoint-interval N Save checkpoint every N tasks (default: 100)

Examples:
    # Full import with intelligent merge
    python scripts/import_asana_tasks.py

    # Import excluding archived projects (only non-archived)
    python scripts/import_asana_tasks.py --exclude-archived

    # Partial import (updates only these tasks, preserves others)
    python scripts/import_asana_tasks.py --only-incomplete --max-tasks 50

    # Refresh classifications for all tasks
    python scripts/import_asana_tasks.py --recalculate
"""

import argparse
import json
import re
import sys
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

# Configuration
TASKS_DIR = DATA_DIR / "tasks"
TASKS_FILE = TASKS_DIR / "tasks.parquet"
PROJECTS_DIR = DATA_DIR / "projects"
PROJECTS_FILE = PROJECTS_DIR / "projects.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
LOGS_DIR = DATA_DIR / "logs"
IMPORT_LOG = LOGS_DIR / "import_log.jsonl"
ATTACHMENTS_DIR = DATA_DIR / "attachments" / "asana_tasks"
CHECKPOINT_FILE = LOGS_DIR / "import_checkpoint.json"
TASKS_CACHE_FILE = LOGS_DIR / "import_tasks_cache.json"
TASKS_CACHE_MAX_AGE_HOURS = 24  # Cache valid for 24 hours

# Ensure directories exist
TASKS_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


class AsanaDirectImporter:
    """Import tasks directly from Asana API to parquet."""

    def __init__(
        self,
        config: AsanaConfig,
        only_incomplete: bool = False,
        assignee_gid: str | None = None,
        max_tasks: int | None = None,
        recalculate: bool = False,
        download_attachments: bool = False,
        include_archived: bool = True,
        task_gid: str | None = None,
        resume: bool = False,
        checkpoint_interval: int = 100,
    ):
        self.config = config
        self.client = AsanaClientWrapper.from_config_source(config)
        self.only_incomplete = only_incomplete
        self.assignee_gid = assignee_gid
        self.max_tasks = max_tasks
        self.recalculate = recalculate
        # When True, download attachments for imported tasks into
        # data/attachments/asana_tasks/<task_gid>/, skipping files that already exist.
        self.download_attachments = download_attachments
        # When True, also fetch tasks from archived projects
        self.include_archived = include_archived
        # If set, only import this specific task GID
        self.task_gid = task_gid
        # When True, resume from last checkpoint
        self.resume = resume
        # Save checkpoint every N tasks
        self.checkpoint_interval = checkpoint_interval
        # When True, resume from last checkpoint
        self.resume = resume
        # Save checkpoint every N tasks
        self.checkpoint_interval = checkpoint_interval

        # Domain classification keywords
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

    def fetch_tasks_from_workspace(self) -> list[dict]:
        """Fetch all tasks from workspace using Asana API."""
        print(f"Fetching tasks from workspace {self.config.source_workspace_gid}...")

        tasks = []
        task_gids = set()  # Track unique tasks
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

        # First, fetch tasks assigned to current user (prioritize assigned tasks)
        print("Fetching tasks assigned to current user (not in projects)...")
        current_user_gid = None  # Initialize for use in My Tasks section
        try:
            # Get current user GID using direct HTTP (more reliable than API client for "me")
            headers = {"Authorization": f"Bearer {self.client._pat}"}
            url = "https://app.asana.com/api/1.0/users/me"
            params = {"opt_fields": "gid,name,email"}
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            user_info = response.json().get("data", {})
            current_user_gid = user_info.get("gid")

            if current_user_gid:
                # Fetch tasks assigned to current user (workspace-wide)
                assignee_opts = {
                    "assignee": current_user_gid,
                    "workspace": self.config.source_workspace_gid,
                    "opt_fields": ",".join(opt_fields),
                }

                assignee_tasks = list(
                    self.client._with_retry(self.client.tasks.get_tasks, assignee_opts)
                )

                standalone_count = 0
                for task_data in assignee_tasks:
                    task_gid = task_data.get("gid")
                    if task_gid in task_gids:
                        continue  # Skip duplicates

                    # Only include tasks that aren't in any projects
                    # Check both 'projects' array and 'memberships' for project associations
                    task_projects = task_data.get("projects", [])
                    memberships = task_data.get("memberships", [])
                    has_projects = bool(task_projects) or any(
                        m.get("project", {}).get("gid")
                        for m in memberships
                        if m.get("project")
                    )

                    if has_projects:
                        continue  # Skip tasks that are in projects

                    # Apply filters
                    if self.only_incomplete and task_data.get("completed"):
                        continue

                    if (
                        self.assignee_gid
                        and task_data.get("assignee", {}).get("gid")
                        != self.assignee_gid
                    ):
                        continue

                    tasks.append(task_data)
                    task_gids.add(task_gid)
                    standalone_count += 1

                    if self.max_tasks and len(tasks) >= self.max_tasks:
                        break

                print(f"Found {standalone_count} standalone tasks (not in projects)")

                if self.max_tasks and len(tasks) >= self.max_tasks:
                    print(f"Reached max_tasks limit ({self.max_tasks})")
                    return tasks

        except Exception as e:
            print(f"Warning: Error fetching standalone tasks: {e}")

        # Also fetch tasks from user's "My Tasks" list (user_task_list)
        # Note: My Tasks are likely already included via standalone tasks above,
        # but we try to fetch via user_task_list for completeness
        print("Fetching tasks from My Tasks list...")
        try:
            # Reuse current_user_gid from standalone tasks section if available
            # Otherwise fetch it
            headers = {"Authorization": f"Bearer {self.client._pat}"}
            if not current_user_gid:
                user_url = "https://app.asana.com/api/1.0/users/me"
                user_params = {"opt_fields": "gid"}
                user_response = requests.get(
                    user_url, headers=headers, params=user_params, timeout=30
                )
                user_response.raise_for_status()
                current_user_gid = user_response.json().get("data", {}).get("gid")

            if not current_user_gid:
                print("Warning: Could not get current user GID for My Tasks")
            else:
                # Try using user GID in URL first
                url = f"https://app.asana.com/api/1.0/users/{current_user_gid}/user_task_lists"
                params = {
                    "workspace": self.config.source_workspace_gid,
                    "opt_fields": "gid,workspace",
                }
                response = requests.get(url, headers=headers, params=params, timeout=30)

                # If 404, try with "me" endpoint as fallback
                if response.status_code == 404:
                    url = "https://app.asana.com/api/1.0/users/me/user_task_lists"
                    response = requests.get(
                        url, headers=headers, params=params, timeout=30
                    )

                if response.status_code == 200:
                    user_task_lists = response.json().get("data", [])

                    user_task_list_gid = None
                    for utl in user_task_lists:
                        workspace_data = utl.get("workspace", {})
                        if isinstance(workspace_data, dict):
                            if (
                                workspace_data.get("gid")
                                == self.config.source_workspace_gid
                            ):
                                user_task_list_gid = utl.get("gid")
                                break
                        elif workspace_data == self.config.source_workspace_gid:
                            user_task_list_gid = utl.get("gid")
                            break

                    if user_task_list_gid:
                        my_tasks_opts = {
                            "project": user_task_list_gid,
                            "opt_fields": ",".join(opt_fields),
                        }

                        my_tasks = list(
                            self.client._with_retry(
                                self.client.tasks.get_tasks, my_tasks_opts
                            )
                        )

                        my_tasks_count = 0
                        for task_data in my_tasks:
                            task_gid = task_data.get("gid")
                            if task_gid in task_gids:
                                continue  # Skip duplicates

                            # Apply filters
                            if self.only_incomplete and task_data.get("completed"):
                                continue

                            if (
                                self.assignee_gid
                                and task_data.get("assignee", {}).get("gid")
                                != self.assignee_gid
                            ):
                                continue

                            tasks.append(task_data)
                            task_gids.add(task_gid)
                            my_tasks_count += 1

                            if self.max_tasks and len(tasks) >= self.max_tasks:
                                break

                        print(
                            f"Found {my_tasks_count} additional tasks from My Tasks list"
                        )

                        if self.max_tasks and len(tasks) >= self.max_tasks:
                            print(f"Reached max_tasks limit ({self.max_tasks})")
                            return tasks
                    else:
                        print("Warning: Could not find user_task_list for workspace")
                else:
                    print(
                        f"Warning: Could not fetch user_task_lists (status {response.status_code}). My Tasks may not be available via this endpoint."
                    )

        except Exception as e:
            print(f"Warning: Error fetching My Tasks: {e}")

        # Now fetch tasks from projects
        try:
            # Get all projects in workspace (non-archived)
            print("Fetching non-archived projects...")
            projects_opts = {
                "workspace": self.config.source_workspace_gid,
                "archived": False,
            }
            projects = list(
                self.client._with_retry(
                    self.client.projects.get_projects, projects_opts
                )
            )
            print(f"Found {len(projects)} non-archived projects")

            # Fetch tasks from each non-archived project
            for idx, project in enumerate(projects, 1):
                project_gid = project.get("gid")
                project_name = project.get("name", "")
                print(
                    f"Fetching tasks from project {idx}/{len(projects)}: {project_name}"
                )

                opts = {"project": project_gid, "opt_fields": ",".join(opt_fields)}

                try:
                    project_tasks = list(
                        self.client._with_retry(self.client.tasks.get_tasks, opts)
                    )

                    for task_data in project_tasks:
                        task_gid = task_data.get("gid")
                        if task_gid in task_gids:
                            continue  # Skip duplicates

                        # Apply filters
                        if self.only_incomplete and task_data.get("completed"):
                            continue

                        if self.assignee_gid:
                            assignee = task_data.get("assignee")
                            if not assignee or assignee.get("gid") != self.assignee_gid:
                                continue

                        tasks.append(task_data)
                        task_gids.add(task_gid)

                        if self.max_tasks and len(tasks) >= self.max_tasks:
                            break

                    if self.max_tasks and len(tasks) >= self.max_tasks:
                        break

                except Exception as e:
                    print(
                        f"Warning: Error fetching tasks from project {project_name}: {e}"
                    )
                    continue

            print(f"Fetched {len(tasks)} tasks from non-archived projects")

            # Also fetch tasks from archived projects if requested
            if self.include_archived:
                print("Fetching archived projects...")
                archived_projects_opts = {
                    "workspace": self.config.source_workspace_gid,
                    "archived": True,
                }
                archived_projects = list(
                    self.client._with_retry(
                        self.client.projects.get_projects, archived_projects_opts
                    )
                )
                print(f"Found {len(archived_projects)} archived projects")

                # Fetch tasks from each archived project
                for idx, project in enumerate(archived_projects, 1):
                    project_gid = project.get("gid")
                    project_name = project.get("name", "")
                    print(
                        f"Fetching tasks from archived project {idx}/{len(archived_projects)}: {project_name}"
                    )

                    opts = {"project": project_gid, "opt_fields": ",".join(opt_fields)}

                    try:
                        project_tasks = list(
                            self.client._with_retry(self.client.tasks.get_tasks, opts)
                        )

                        for task_data in project_tasks:
                            task_gid = task_data.get("gid")
                            if task_gid in task_gids:
                                continue  # Skip duplicates

                            # Apply filters
                            if self.only_incomplete and task_data.get("completed"):
                                continue

                            if self.assignee_gid:
                                assignee = task_data.get("assignee")
                                if (
                                    not assignee
                                    or assignee.get("gid") != self.assignee_gid
                                ):
                                    continue

                            tasks.append(task_data)
                            task_gids.add(task_gid)

                            if self.max_tasks and len(tasks) >= self.max_tasks:
                                break

                        if self.max_tasks and len(tasks) >= self.max_tasks:
                            break

                    except Exception as e:
                        print(
                            f"Warning: Error fetching tasks from archived project {project_name}: {e}"
                        )
                        continue

                print(
                    f"Fetched {len(tasks)} total tasks from projects (including archived)"
                )

            # Also fetch tasks assigned to current user that aren't in projects
            print("Fetching tasks assigned to current user (not in projects)...")
            current_user_gid = None  # Initialize for use in My Tasks section
            try:
                # Get current user GID using direct HTTP (more reliable than API client for "me")
                headers = {"Authorization": f"Bearer {self.client._pat}"}
                url = "https://app.asana.com/api/1.0/users/me"
                params = {"opt_fields": "gid,name,email"}
                response = requests.get(url, headers=headers, params=params, timeout=30)
                response.raise_for_status()
                user_info = response.json().get("data", {})
                current_user_gid = user_info.get("gid")

                if current_user_gid:
                    # Fetch tasks assigned to current user (workspace-wide)
                    assignee_opts = {
                        "assignee": current_user_gid,
                        "workspace": self.config.source_workspace_gid,
                        "opt_fields": ",".join(opt_fields),
                    }

                    assignee_tasks = list(
                        self.client._with_retry(
                            self.client.tasks.get_tasks, assignee_opts
                        )
                    )

                    standalone_count = 0
                    for task_data in assignee_tasks:
                        task_gid = task_data.get("gid")
                        if task_gid in task_gids:
                            continue  # Skip duplicates (already fetched from projects)

                        # Only include tasks that aren't in any projects
                        # Check both 'projects' array and 'memberships' for project associations
                        task_projects = task_data.get("projects", [])
                        memberships = task_data.get("memberships", [])
                        has_projects = bool(task_projects) or any(
                            m.get("project", {}).get("gid")
                            for m in memberships
                            if m.get("project")
                        )

                        if has_projects:
                            continue  # Skip tasks that are in projects

                        # Apply filters
                        if self.only_incomplete and task_data.get("completed"):
                            continue

                        if (
                            self.assignee_gid
                            and task_data.get("assignee", {}).get("gid")
                            != self.assignee_gid
                        ):
                            continue

                        tasks.append(task_data)
                        task_gids.add(task_gid)
                        standalone_count += 1

                        if self.max_tasks and len(tasks) >= self.max_tasks:
                            break

                    print(
                        f"Found {standalone_count} standalone tasks (not in projects)"
                    )

                    if self.max_tasks and len(tasks) >= self.max_tasks:
                        print(f"Reached max_tasks limit ({self.max_tasks})")
                        return tasks

            except Exception as e:
                print(f"Warning: Error fetching standalone tasks: {e}")

            # Also fetch tasks from user's "My Tasks" list (user_task_list)
            # Note: My Tasks are likely already included via standalone tasks above,
            # but we try to fetch via user_task_list for completeness
            print("Fetching tasks from My Tasks list...")
            try:
                # Reuse current_user_gid from standalone tasks section if available
                # Otherwise fetch it
                headers = {"Authorization": f"Bearer {self.client._pat}"}
                if not current_user_gid:
                    user_url = "https://app.asana.com/api/1.0/users/me"
                    user_params = {"opt_fields": "gid"}
                    user_response = requests.get(
                        user_url, headers=headers, params=user_params, timeout=30
                    )
                    user_response.raise_for_status()
                    current_user_gid = user_response.json().get("data", {}).get("gid")

                if not current_user_gid:
                    print("Warning: Could not get current user GID for My Tasks")
                else:
                    # Try using user GID in URL first
                    url = f"https://app.asana.com/api/1.0/users/{current_user_gid}/user_task_lists"
                    params = {
                        "workspace": self.config.source_workspace_gid,
                        "opt_fields": "gid,workspace",
                    }
                    response = requests.get(
                        url, headers=headers, params=params, timeout=30
                    )

                    # If 404, try with "me" endpoint as fallback
                    if response.status_code == 404:
                        url = "https://app.asana.com/api/1.0/users/me/user_task_lists"
                        response = requests.get(
                            url, headers=headers, params=params, timeout=30
                        )

                    if response.status_code == 200:
                        user_task_lists = response.json().get("data", [])

                        user_task_list_gid = None
                        for utl in user_task_lists:
                            workspace_data = utl.get("workspace", {})
                            if isinstance(workspace_data, dict):
                                if (
                                    workspace_data.get("gid")
                                    == self.config.source_workspace_gid
                                ):
                                    user_task_list_gid = utl.get("gid")
                                    break
                            elif workspace_data == self.config.source_workspace_gid:
                                user_task_list_gid = utl.get("gid")
                                break

                        if user_task_list_gid:
                            my_tasks_opts = {
                                "project": user_task_list_gid,
                                "opt_fields": ",".join(opt_fields),
                            }

                            my_tasks = list(
                                self.client._with_retry(
                                    self.client.tasks.get_tasks, my_tasks_opts
                                )
                            )

                            my_tasks_count = 0
                            for task_data in my_tasks:
                                task_gid = task_data.get("gid")
                                if task_gid in task_gids:
                                    continue  # Skip duplicates

                                # Apply filters
                                if self.only_incomplete and task_data.get("completed"):
                                    continue

                                if (
                                    self.assignee_gid
                                    and task_data.get("assignee", {}).get("gid")
                                    != self.assignee_gid
                                ):
                                    continue

                                tasks.append(task_data)
                                task_gids.add(task_gid)
                                my_tasks_count += 1

                                if self.max_tasks and len(tasks) >= self.max_tasks:
                                    break

                            print(
                                f"Found {my_tasks_count} additional tasks from My Tasks list"
                            )

                            if self.max_tasks and len(tasks) >= self.max_tasks:
                                print(f"Reached max_tasks limit ({self.max_tasks})")
                                return tasks
                        else:
                            print(
                                "Warning: Could not find user_task_list for workspace"
                            )
                    else:
                        print(
                            f"Warning: Could not fetch user_task_lists (status {response.status_code}). My Tasks may not be available via this endpoint."
                        )

            except Exception as e:
                print(f"Warning: Error fetching My Tasks: {e}")

            print(
                f"Fetched {len(tasks)} total tasks from Asana (projects + standalone + My Tasks)"
            )
            return tasks

        except Exception as e:
            print(f"Error fetching tasks: {e}", file=sys.stderr)
            raise

    def fetch_single_task(self, task_gid: str) -> list[dict]:
        """Fetch a single task by GID."""
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
            task = self.client._with_retry(self.client.tasks.get_task, task_gid, opts)
            return [task]
        except Exception as e:
            print(f"Error fetching task {task_gid}: {e}")
            return []

    def fetch_subtasks_for_task(self, task_gid: str) -> list[dict]:
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
                self.client._with_retry(
                    self.client.tasks.get_subtasks_for_task, task_gid, opts
                )
            )
            return subtasks
        except Exception:
            # If task has no subtasks or error occurs, return empty list
            return []

    def fetch_all_subtasks_recursive(
        self, task_gid: str, parent_task_id: str | None = None
    ) -> list[tuple[dict, str]]:
        """Recursively fetch all subtasks at all levels.

        Returns list of tuples: (subtask_data, parent_task_id)
        """
        all_subtasks = []
        direct_subtasks = self.fetch_subtasks_for_task(task_gid)

        for subtask_data in direct_subtasks:
            subtask_gid = subtask_data.get("gid")
            if not subtask_gid:
                continue

            # Add this subtask with its parent
            all_subtasks.append((subtask_data, parent_task_id or task_gid))

            # Recursively fetch subtasks of this subtask
            nested_subtasks = self.fetch_all_subtasks_recursive(
                subtask_gid, parent_task_id=subtask_gid
            )
            all_subtasks.extend(nested_subtasks)

        return all_subtasks

    def classify_domain(self, title: str, notes: str) -> str:
        """Classify task domain based on title and notes content."""
        text = f"{title} {notes}".lower()

        domain_scores = {}
        for domain, keywords in self.domain_keywords.items():
            score = sum(1 for keyword in keywords if keyword in text)
            if score > 0:
                domain_scores[domain] = score

        if not domain_scores:
            return "other"

        return max(domain_scores, key=domain_scores.get)

    def download_attachments_for_task(self, task_gid: str) -> int:
        """Download attachments for a task into ATTACHMENTS_DIR / <task_gid>.

        Idempotent: skips attachments whose files already exist and are non-empty.
        Returns number of attachments newly downloaded.
        """
        downloaded = 0
        try:
            # Fetch task with attachments metadata
            opts = {
                "opt_fields": "attachments,attachments.gid,attachments.name,attachments.download_url"
            }
            task = self.client._with_retry(
                self.client.tasks.get_task,
                task_gid,
                opts,
            )
            attachments = task.get("attachments", []) or []
            if not attachments:
                return 0

            # Ensure base dir exists
            ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
            task_dir = ATTACHMENTS_DIR / task_gid
            task_dir.mkdir(parents=True, exist_ok=True)

            headers = {"Authorization": f"Bearer {self.client._pat}"}

            for attachment in attachments:
                att_gid = attachment.get("gid")
                name = attachment.get("name") or f"attachment-{att_gid or 'unknown'}"
                download_url = attachment.get("download_url")

                target_path = task_dir / name
                # Skip if already present and non-empty
                if target_path.exists() and target_path.stat().st_size > 0:
                    continue

                # If no download_url, fetch via attachment endpoint
                if not download_url and att_gid:
                    try:
                        att_opts = {"opt_fields": "download_url"}
                        att_data = self.client._with_retry(
                            self.client.attachments.get_attachment,
                            att_gid,
                            att_opts,
                        )
                        download_url = att_data.get("download_url")
                    except Exception:
                        continue

                if not download_url:
                    continue

                try:
                    resp = requests.get(
                        download_url, headers=headers, timeout=120, stream=True
                    )
                    resp.raise_for_status()

                    # Optional: basic size check (100MB)
                    content_length = resp.headers.get("Content-Length")
                    if content_length and int(content_length) > 100 * 1024 * 1024:
                        # Too large, skip
                        continue

                    with open(target_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)

                    downloaded += 1
                except Exception:
                    # Best-effort; skip on failure
                    continue

            return downloaded
        except Exception:
            return 0

    def normalize_task(
        self, task_data: dict, parent_task_id: str | None = None
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
        created_at = datetime.fromisoformat(created_at_str) if created_at_str else None

        modified_at_str = task_data.get("modified_at")
        updated_at = (
            datetime.fromisoformat(modified_at_str) if modified_at_str else None
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

        # Assignee-specific My Tasks section (Today / This week / Later, etc.)
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
        domain = self.classify_domain(title, notes)

        # Determine status
        if completed:
            status = "completed"
        else:
            status = "pending"

        # Use literal Asana GID; no prefixes. Asana-specific identity lives in
        # asana_source_gid/asana_workspace; task_id stays 1:1 with the source GID.
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
            "notes": f"Imported from Asana (gid: {gid})",
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
            "asana_workspace": self.config.source_workspace_gid,  # Store actual workspace GID
            "asana_source_gid": gid,
            "asana_target_gid": None,
            "parent_task_id": parent_task_id,
            "permalink_url": permalink_url,
            "followers_gids": "|".join(follower_gids) if follower_gids else None,
            "follower_names": "|".join(follower_names) if follower_names else None,
            "import_date": date.today(),
            "import_source_file": "asana_api_direct",
        }

    def create_parquet_snapshot(self):
        """Create timestamped snapshot of tasks.parquet before modification."""
        if not TASKS_FILE.exists():
            print("No existing tasks.parquet to snapshot")
            return

        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        snapshot_file = SNAPSHOTS_DIR / f"tasks-{timestamp}.parquet"

        df = pd.read_parquet(TASKS_FILE)
        df.to_parquet(snapshot_file, index=False)
        print(f"Created snapshot: {snapshot_file.name}")

    def save_tasks_cache(self, tasks: list[dict], metadata: dict | None = None):
        """Save fetched tasks to cache file."""
        cache_data = {
            "tasks": tasks,
            "timestamp": datetime.now().isoformat(),
            "total_count": len(tasks),
            "metadata": metadata or {},
        }
        try:
            with open(TASKS_CACHE_FILE, "w") as f:
                json.dump(cache_data, f, indent=2)
            print(f"Cached {len(tasks)} tasks for resume")
        except Exception as e:
            print(f"Warning: Could not save tasks cache: {e}")

    def load_tasks_cache(self) -> tuple[list[dict], dict] | None:
        """Load tasks from cache if valid.

        Returns:
            Tuple of (tasks_list, cache_metadata) if cache is valid, None otherwise
        """
        if not TASKS_CACHE_FILE.exists():
            return None

        try:
            with open(TASKS_CACHE_FILE) as f:
                cache_data = json.load(f)

            # Check cache age
            cache_timestamp_str = cache_data.get("timestamp")
            age_hours = None
            if cache_timestamp_str:
                cache_timestamp = datetime.fromisoformat(cache_timestamp_str)
                age_hours = (datetime.now() - cache_timestamp).total_seconds() / 3600

                if age_hours > TASKS_CACHE_MAX_AGE_HOURS:
                    print(
                        f"Tasks cache expired ({age_hours:.1f} hours old, max {TASKS_CACHE_MAX_AGE_HOURS} hours)"
                    )
                    return None

            tasks = cache_data.get("tasks", [])
            metadata = cache_data.get("metadata", {})

            if tasks:
                age_str = (
                    f"{age_hours:.1f} hours" if age_hours is not None else "unknown age"
                )
                print(f"Loaded {len(tasks)} tasks from cache (age: {age_str})")
                return tasks, metadata

        except Exception as e:
            print(f"Warning: Could not load tasks cache: {e}")

        return None

    def clear_tasks_cache(self):
        """Clear tasks cache file."""
        if TASKS_CACHE_FILE.exists():
            TASKS_CACHE_FILE.unlink()
            print("Tasks cache cleared")

    def save_checkpoint(
        self, last_task_gid: str, processed_count: int, total_count: int
    ):
        """Save checkpoint with last processed task GID (atomic write)."""
        checkpoint_data = {
            "last_task_gid": last_task_gid,
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
            print(
                f"  Checkpoint saved: processed {processed_count}/{total_count} tasks"
            )
        except Exception as e:
            print(f"Warning: Could not save checkpoint: {e}")
            # Clean up temp file on error
            if temp_file.exists():
                temp_file.unlink()

    def load_checkpoint(self) -> dict[str, Any] | None:
        """Load checkpoint if it exists."""
        if not CHECKPOINT_FILE.exists():
            return None
        try:
            with open(CHECKPOINT_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not load checkpoint: {e}")
            return None

    def clear_checkpoint(self):
        """Clear checkpoint file."""
        if CHECKPOINT_FILE.exists():
            CHECKPOINT_FILE.unlink()

    def merge_task_fields(self, existing: dict, new_from_asana: dict) -> dict:
        """
        Intelligently merge existing task with new Asana data.

        Merge strategy:
        - Asana source fields (always update from Asana): title, description, completed status, dates, project/section
        - Local enrichments (preserve unless recalculate): domain
        - Local-only fields (never overwrite): execution_plan_path, notes (if manually modified)
        - Metadata (always update): import_date, updated_date
        """
        merged = existing.copy()

        # ASANA SOURCE FIELDS - Always take from Asana (source of truth)
        asana_source_fields = [
            "title",
            "description",
            "status",
            "completed_date",
            "due_date",
            "start_date",
            "project_ids",
            "project_names",
            "section_ids",
            "section_names",
            "created_at",
            "updated_at",
            "asana_source_gid",
            "asana_workspace",
            "asana_target_gid",
            "parent_task_id",
        ]

        for field in asana_source_fields:
            if field in new_from_asana:
                merged[field] = new_from_asana[field]

        # CLASSIFICATION FIELDS - Update from Asana if recalculate, else preserve local
        classification_fields = ["domain"]

        if self.recalculate:
            for field in classification_fields:
                if field in new_from_asana:
                    merged[field] = new_from_asana[field]
        # else: keep existing values (local modifications preserved)

        # LOCAL-ONLY FIELDS - Never overwrite from Asana
        # - execution_plan_path: locally created
        # - notes: preserve if it's not the default import message
        # - recurrence: local enhancement

        existing_notes = existing.get("notes", "")
        new_notes = new_from_asana.get("notes", "")

        # Only update notes if existing notes were just the default import message
        if existing_notes and not existing_notes.startswith("Imported from Asana"):
            # Keep local notes - they've been manually enriched
            pass
        else:
            # Update to new import message
            merged["notes"] = new_notes

        # METADATA - Always update
        merged["import_date"] = date.today()
        merged["import_source_file"] = "asana_api_direct"

        return merged

    def import_projects_from_tasks(self, raw_tasks: list[dict]) -> int:
        """Extract and import unique projects from task data into projects.parquet."""
        import uuid

        # Collect unique projects from all tasks
        projects_seen = {}
        for task in raw_tasks:
            task_projects = task.get("projects", [])
            for proj in task_projects:
                proj_gid = proj.get("gid")
                if not proj_gid or proj_gid in projects_seen:
                    continue
                projects_seen[proj_gid] = proj

        if not projects_seen:
            return 0

        # Load existing projects
        if PROJECTS_FILE.exists():
            projects_df = pd.read_parquet(PROJECTS_FILE)
        else:
            # Create empty dataframe with schema columns
            projects_df = pd.DataFrame(
                columns=[
                    "project_id",
                    "asana_project_gid",
                    "name",
                    "goals",
                    "status",
                    "priority",
                    "start_date",
                    "end_date",
                    "notes",
                    "html_notes",
                    "color",
                    "archived",
                    "public",
                    "icon",
                    "due_date",
                    "owner_gid",
                    "owner_name",
                    "followers_gids",
                    "follower_names",
                    "members_gids",
                    "member_names",
                    "custom_fields",
                    "default_view",
                    "created_at",
                    "modified_at",
                    "import_date",
                    "import_source_file",
                ]
            )

        # Create snapshot
        if not projects_df.empty:
            SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            snapshot_path = SNAPSHOTS_DIR / f"projects-{ts}.parquet"
            projects_df.to_parquet(snapshot_path, index=False)

        # Normalize and merge projects
        existing_gids = (
            set(projects_df["asana_project_gid"].astype(str).tolist())
            if "asana_project_gid" in projects_df.columns and not projects_df.empty
            else set()
        )

        new_projects = []
        updated_count = 0

        for proj_gid, proj_data in projects_seen.items():
            # Extract project fields
            owner = proj_data.get("owner", {})
            followers = proj_data.get("followers", [])
            members = proj_data.get("members", [])
            custom_fields = proj_data.get("custom_fields", [])

            # Parse dates
            due_date_raw = proj_data.get("due_date")
            due_date = (
                datetime.fromisoformat(due_date_raw).date() if due_date_raw else None
            )

            start_on_raw = proj_data.get("start_on")
            start_date = (
                datetime.fromisoformat(start_on_raw).date() if start_on_raw else None
            )

            project_row = {
                "asana_project_gid": proj_gid,
                "name": proj_data.get("name", ""),
                "color": proj_data.get("color"),
                "archived": proj_data.get("archived", False),
                "public": proj_data.get("public", False),
                "icon": proj_data.get("icon"),
                "notes": proj_data.get("notes", "") or "",
                "html_notes": proj_data.get("html_notes", "") or "",
                "due_date": due_date,
                "start_date": start_date,
                "owner_gid": owner.get("gid") if owner else None,
                "owner_name": owner.get("name") if owner else None,
                "followers_gids": (
                    "|".join([f.get("gid") for f in followers if f.get("gid")])
                    if followers
                    else None
                ),
                "follower_names": (
                    "|".join([f.get("name") for f in followers if f.get("name")])
                    if followers
                    else None
                ),
                "members_gids": (
                    "|".join([m.get("gid") for m in members if m.get("gid")])
                    if members
                    else None
                ),
                "member_names": (
                    "|".join([m.get("name") for m in members if m.get("name")])
                    if members
                    else None
                ),
                "custom_fields": json.dumps(custom_fields) if custom_fields else None,
                "default_view": (
                    ",".join(proj_data.get("default_view", []))
                    if isinstance(proj_data.get("default_view"), list)
                    else proj_data.get("default_view")
                ),
                "import_date": date.today(),
                "import_source_file": "asana_api_direct",
            }

            # Generate project_id if new, or use existing
            if proj_gid in existing_gids:
                # Update existing project
                idx = projects_df[projects_df["asana_project_gid"] == proj_gid].index
                if not idx.empty:
                    for key, value in project_row.items():
                        projects_df.loc[idx[0], key] = value
                    updated_count += 1
            else:
                # New project - generate project_id
                project_row["project_id"] = str(uuid.uuid4())[:16]
                project_row["goals"] = None
                project_row["status"] = None
                project_row["priority"] = None
                project_row["end_date"] = None
                project_row["created_at"] = None
                project_row["modified_at"] = None
                new_projects.append(project_row)

        # Add new projects
        if new_projects:
            new_df = pd.DataFrame(new_projects)
            projects_df = pd.concat([projects_df, new_df], ignore_index=True)

        # Save
        projects_df.to_parquet(PROJECTS_FILE, index=False)

        return len(new_projects) + updated_count

    def merge_and_save_tasks(
        self, normalized_tasks: list[dict], log_individual_tasks: bool = False
    ) -> tuple[pd.DataFrame, dict[str, int]]:
        """
        Merge normalized tasks with existing parquet file and save incrementally.

        Args:
            normalized_tasks: List of normalized task dictionaries
            log_individual_tasks: If True, log each task's save/skip status

        Returns:
            Tuple of (merged_dataframe, stats_dict with tasks_new, tasks_updated, tasks_skipped)
        """
        new_df = pd.DataFrame(normalized_tasks)

        # Load existing tasks or create new
        if TASKS_FILE.exists():
            existing_df = pd.read_parquet(TASKS_FILE)

            # Separate Asana and non-Asana tasks
            existing_asana_mask = (
                existing_df["asana_source_gid"].notna()
                if "asana_source_gid" in existing_df.columns
                else pd.Series(False, index=existing_df.index)
            )
            non_asana_df = existing_df[~existing_asana_mask]
            existing_asana_df = existing_df[existing_asana_mask]

            # Create lookup for existing Asana tasks by task_id
            existing_asana_dict = {
                row["task_id"]: row.to_dict() for _, row in existing_asana_df.iterrows()
            }

            # Also create lookup by asana_source_gid for tasks that may have been created
            # locally first and later linked to an Asana GID.
            existing_by_gid = {}
            if "asana_source_gid" in existing_df.columns:
                for _, row in existing_df.iterrows():
                    gid = row.get("asana_source_gid")
                    if gid:
                        existing_by_gid[gid] = row.to_dict()

            # Create lookup for newly fetched tasks
            fetched_task_ids = {task["task_id"] for task in normalized_tasks}

            merged_tasks = []
            tasks_new = 0
            tasks_updated = 0
            tasks_skipped = 0

            # Add/update fetched tasks
            for new_task in normalized_tasks:
                task_id = new_task["task_id"]
                asana_gid = new_task.get("asana_source_gid")

                # Try to match by task_id first
                if task_id in existing_asana_dict:
                    existing_task = existing_asana_dict[task_id]
                # Fallback: match by asana_source_gid for manually created tasks
                elif asana_gid and asana_gid in existing_by_gid:
                    existing_task = existing_by_gid[asana_gid]
                    # Normalize local task_id to literal Asana GID
                    existing_task["task_id"] = task_id
                else:
                    existing_task = None

                if existing_task:
                    # Check if task has changed in Asana (incremental sync optimization)
                    existing_updated_at = existing_task.get("updated_at")
                    new_updated_at = new_task.get("updated_at")

                    if (
                        existing_updated_at
                        and new_updated_at
                        and existing_updated_at == new_updated_at
                    ):
                        # No changes in Asana - skip merge, keep existing
                        merged_tasks.append(existing_task)
                        tasks_skipped += 1
                        if log_individual_tasks:
                            task_title = new_task.get("title", "Unknown")[:50]
                            print(f"      Skipped (unchanged): {task_title}")
                    else:
                        # Task changed - merge intelligently
                        merged_task = self.merge_task_fields(existing_task, new_task)
                        merged_tasks.append(merged_task)
                        tasks_updated += 1
                        if log_individual_tasks:
                            task_title = new_task.get("title", "Unknown")[:50]
                            print(f"      Updated: {task_title}")
                else:
                    # New Asana task - add as-is
                    merged_tasks.append(new_task)
                    tasks_new += 1
                    if log_individual_tasks:
                        task_title = new_task.get("title", "Unknown")[:50]
                        print(f"      Added: {task_title}")

            # Preserve existing Asana-backed tasks that weren't fetched this time
            for task_id, existing_task in existing_asana_dict.items():
                if task_id not in fetched_task_ids:
                    merged_tasks.append(existing_task)

            # Combine non-Asana tasks + all Asana tasks (updated/new/preserved)
            merged_asana_df = pd.DataFrame(merged_tasks)
            merged_df = pd.concat([non_asana_df, merged_asana_df], ignore_index=True)

        else:
            merged_df = new_df
            tasks_new = len(normalized_tasks)
            tasks_updated = 0
            tasks_skipped = 0

        # Write to parquet
        merged_df.to_parquet(TASKS_FILE, index=False)

        stats = {
            "tasks_new": tasks_new,
            "tasks_updated": tasks_updated,
            "tasks_skipped": tasks_skipped,
        }

        return merged_df, stats

    def import_tasks(self) -> dict[str, Any]:
        """Fetch from Asana and import directly to parquet with intelligent merging."""
        # Create snapshot first
        self.create_parquet_snapshot()

        # If task_gid is specified, fetch only that task (no caching for single task)
        if self.task_gid:
            print(f"Fetching single task: {self.task_gid}")
            raw_tasks = self.fetch_single_task(self.task_gid)
        else:
            # Check for resume and try to load from cache first
            use_cache = False
            if self.resume:
                cached_result = self.load_tasks_cache()
                if cached_result:
                    raw_tasks, cache_metadata = cached_result
                    use_cache = True
                    print(
                        f"Using cached tasks (fetched at {cache_metadata.get('timestamp', 'unknown')})"
                    )

            # Fetch fresh if not using cache
            if not use_cache:
                print("Fetching tasks from Asana API...")
                raw_tasks = self.fetch_tasks_from_workspace()
                # Save to cache for future resume
                if raw_tasks:
                    self.save_tasks_cache(
                        raw_tasks,
                        {
                            "workspace_gid": self.config.source_workspace_gid,
                            "only_incomplete": self.only_incomplete,
                            "assignee_gid": self.assignee_gid,
                            "include_archived": self.include_archived,
                        },
                    )

        if not raw_tasks:
            print("No tasks to import")
            return {
                "tasks_imported": 0,
                "tasks_updated": 0,
                "tasks_new": 0,
                "tasks_total": 0,
            }

        # Check for resume checkpoint
        resume_from_gid = None
        checkpoint = None
        if self.resume:
            checkpoint = self.load_checkpoint()
            if checkpoint:
                resume_from_gid = checkpoint.get("last_task_gid")
                processed_count = checkpoint.get("processed_count", 0)
                print(
                    f"Resuming from checkpoint: last processed task GID {resume_from_gid} ({processed_count} tasks)"
                )
            else:
                print("No checkpoint found, starting from beginning")

        # Skip already processed tasks if resuming
        if resume_from_gid:
            checkpoint_found = False
            checkpoint_idx = -1

            # Find checkpoint task position in the fetched list
            for idx, task in enumerate(raw_tasks):
                if task.get("gid") == resume_from_gid:
                    checkpoint_found = True
                    checkpoint_idx = idx
                    break

            if checkpoint_found:
                # Process tasks starting AFTER the checkpoint task
                tasks_to_process = raw_tasks[checkpoint_idx + 1 :]
                remaining_count = len(raw_tasks) - checkpoint_idx - 1
                print(
                    f"Found checkpoint task at position {checkpoint_idx + 1}/{len(raw_tasks)}, processing {remaining_count} remaining tasks"
                )
            else:
                # Checkpoint task not found - might have been deleted or order changed
                # Check if we've already processed enough tasks by comparing counts
                checkpoint_processed = (
                    checkpoint.get("processed_count", 0) if checkpoint else 0
                )
                if checkpoint_processed >= len(raw_tasks):
                    print(
                        f"Checkpoint shows {checkpoint_processed} tasks processed, but only {len(raw_tasks)} tasks fetched. All tasks may already be processed."
                    )
                    tasks_to_process = []
                else:
                    print(
                        f"Warning: Checkpoint task GID {resume_from_gid} not found in fetched tasks. Starting from beginning."
                    )
                    tasks_to_process = raw_tasks

            raw_tasks = tasks_to_process

        # Normalize tasks and download attachments
        normalized_tasks = []
        subtask_gids = []  # Track subtask GIDs for comment/attachment import
        total_tasks = len(raw_tasks)

        # Calculate starting position for display
        start_position = (
            checkpoint.get("processed_count", 0) + 1
            if (resume_from_gid and checkpoint)
            else 1
        )
        total_all_tasks = (
            checkpoint.get("total_count", total_tasks)
            if (resume_from_gid and checkpoint)
            else total_tasks
        )

        print(f"\nProcessing {len(raw_tasks)} tasks...")

        for idx, task in enumerate(raw_tasks, 1):
            task_name = task.get("name", "Unknown")[:50]  # Truncate long names
            current_position = start_position + idx - 1
            print(
                f"  Processing task {current_position}/{total_all_tasks}: {task_name}"
            )

            normalized = self.normalize_task(task)
            task_gid = normalized["task_id"]  # task_id is the Asana GID

            # Download description attachments and rewrite HTML
            description_attachment_map = download_description_attachments(
                self.client, task_gid, normalized.get("description_html_remote")
            )
            normalized["description_html"] = rewrite_html_with_local_attachments(
                normalized.get("description_html_remote"), description_attachment_map
            )
            # Derive a local-only plain-text description from HTML, preserving link labels
            if normalized.get("description_html"):
                normalized["description"] = html_to_local_text(
                    normalized["description_html"]
                )
            normalized_tasks.append(normalized)

            # Recursively fetch and normalize all subtasks at all levels
            try:
                # Pass the normalized task_id as parent_task_id for consistency
                all_subtasks = self.fetch_all_subtasks_recursive(
                    task_gid, parent_task_id=task_gid
                )
                if all_subtasks:
                    print(
                        f"    Found {len(all_subtasks)} subtask(s) for task {task_name}"
                    )

                for subtask_data, parent_id in all_subtasks:
                    subtask_normalized = self.normalize_task(
                        subtask_data, parent_task_id=parent_id
                    )
                    subtask_gid = subtask_normalized["task_id"]
                    subtask_gids.append(subtask_gid)

                    # Download description attachments and rewrite HTML for subtask
                    subtask_attachment_map = download_description_attachments(
                        self.client,
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
                    normalized_tasks.append(subtask_normalized)
            except Exception as e:
                print(f"Warning: Error fetching subtasks for task {task_gid}: {e}")
                continue

            # Save checkpoint and save tasks incrementally
            if idx % self.checkpoint_interval == 0:
                current_position = start_position + idx - 1
                self.save_checkpoint(task_gid, current_position, total_all_tasks)

                # Save tasks incrementally for safety (in case script crashes)
                print(
                    f"  Saving {len(normalized_tasks)} tasks to parquet incrementally..."
                )
                merged_df, inc_stats = self.merge_and_save_tasks(
                    normalized_tasks, log_individual_tasks=True
                )
                print(
                    f"  Incremental save: {inc_stats['tasks_new']} new, {inc_stats['tasks_updated']} updated, {inc_stats['tasks_skipped']} skipped, total in parquet: {len(merged_df)}"
                )

        # Save final checkpoint
        if raw_tasks:
            last_task_gid = raw_tasks[-1].get("gid")
            final_position = start_position + len(raw_tasks) - 1
            self.save_checkpoint(last_task_gid, final_position, total_all_tasks)

        print(
            f"Processed {len(normalized_tasks)} tasks (including {len(subtask_gids)} subtasks)"
        )

        # Final merge and save (handles any tasks not yet saved from last checkpoint interval)
        print("Performing final merge and save...")
        merged_df, final_stats = self.merge_and_save_tasks(normalized_tasks)
        tasks_new = final_stats["tasks_new"]
        tasks_updated = final_stats["tasks_updated"]
        tasks_skipped = final_stats["tasks_skipped"]

        # Import projects from task data
        projects_imported = 0
        try:
            projects_imported = self.import_projects_from_tasks(raw_tasks)
        except Exception as e:
            print(f"Warning: Error importing projects: {e}", file=sys.stderr)
            projects_imported = 0

        # Always import comments for all fetched tasks (including subtasks) into task_comments.parquet.
        # import_comments_for_tasks is idempotent and deduplicates by asana_story_gid.
        comment_task_gids = [t.get("gid") for t in raw_tasks if t.get("gid")]
        comment_task_gids.extend(subtask_gids)  # Include subtasks
        comments_imported = 0
        if comment_task_gids:
            try:
                comments_imported = import_comments_for_tasks(
                    self.client, "source", comment_task_gids
                )
            except Exception:
                comments_imported = 0

        # Import all attachments for all fetched tasks (including subtasks) into task_attachments.parquet.
        # import_attachments_for_tasks is idempotent and deduplicates by asana_attachment_gid.
        print("Importing attachments...")
        attachment_task_gids = [t.get("gid") for t in raw_tasks if t.get("gid")]
        attachment_task_gids.extend(subtask_gids)  # Include subtasks
        attachments_downloaded = 0
        if attachment_task_gids:
            try:
                attachments_downloaded = import_attachments_for_tasks(
                    self.client, "source", attachment_task_gids
                )
                print(f"Downloaded {attachments_downloaded} attachments")
            except Exception as e:
                print(f"Warning: Error importing attachments: {e}")
                attachments_downloaded = 0

        # Import custom fields for all fetched tasks (including subtasks)
        print("Importing custom fields...")
        custom_fields_imported = 0
        if attachment_task_gids:
            try:
                custom_fields_imported = import_custom_fields_for_tasks(
                    self.client, "source", attachment_task_gids
                )
                print(f"Imported {custom_fields_imported} custom field values")
            except Exception as e:
                print(f"Warning: Error importing custom fields: {e}")
                custom_fields_imported = 0

        # Import dependencies for all fetched tasks (including subtasks)
        print("Importing dependencies...")
        dependencies_imported = 0
        if attachment_task_gids:
            try:
                dependencies_imported = import_dependencies_for_tasks(
                    self.client, "source", attachment_task_gids
                )
                print(f"Imported {dependencies_imported} dependencies")
            except Exception as e:
                print(f"Warning: Error importing dependencies: {e}")
                dependencies_imported = 0

        # Import all stories (not just comments) for all fetched tasks (including subtasks)
        print("Importing stories...")
        stories_imported = 0
        if attachment_task_gids:
            try:
                stories_imported = import_stories_for_tasks(
                    self.client, "source", attachment_task_gids
                )
                print(f"Imported {stories_imported} stories")
            except Exception as e:
                print(f"Warning: Error importing stories: {e}")
                stories_imported = 0

        stats = {
            "tasks_fetched": len(normalized_tasks),
            "tasks_updated": tasks_updated,
            "tasks_new": tasks_new,
            "tasks_skipped": tasks_skipped,
            "tasks_total": len(merged_df),
            "projects_imported": projects_imported,
            "attachments_downloaded": attachments_downloaded,
            "comments_imported": comments_imported,
            "custom_fields_imported": custom_fields_imported,
            "dependencies_imported": dependencies_imported,
            "stories_imported": stories_imported,
        }

        # Log import
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "action": "import_asana_tasks_direct",
            "source": "asana_api",
            "stats": stats,
            "merge_strategy": "intelligent_field_level",
        }
        with open(IMPORT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")

        # Clear checkpoint and cache on successful completion
        self.clear_checkpoint()
        self.clear_tasks_cache()
        print("Import completed successfully, checkpoint and cache cleared")

        return stats


def main():
    parser = argparse.ArgumentParser(
        description="Import tasks directly from Asana API to parquet"
    )
    parser.add_argument(
        "--only-incomplete",
        action="store_true",
        help="Only import tasks that are not completed",
    )
    parser.add_argument(
        "--assignee-gid",
        type=str,
        help="Only import tasks assigned to this Asana user gid",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        help="Maximum number of tasks to import (after filters)",
    )
    parser.add_argument(
        "--recalculate",
        action="store_true",
        help="Recalculate domain for all Asana tasks",
    )
    parser.add_argument(
        "--download-attachments",
        action="store_true",
        help="Download attachments for imported tasks into data/attachments/asana_tasks/",
    )
    parser.add_argument(
        "--exclude-archived",
        action="store_true",
        help="Exclude tasks from archived projects (only import from non-archived projects)",
    )
    parser.add_argument(
        "--task-gid",
        type=str,
        help="Import only this specific task GID (and its subtasks)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last checkpoint (saves progress every 100 tasks)",
    )
    parser.add_argument(
        "--checkpoint-interval",
        type=int,
        default=100,
        help="Save checkpoint every N tasks (default: 100)",
    )

    args = parser.parse_args()

    try:
        config = AsanaConfig.from_env()
        importer = AsanaDirectImporter(
            config,
            only_incomplete=args.only_incomplete,
            assignee_gid=args.assignee_gid,
            max_tasks=args.max_tasks,
            recalculate=args.recalculate,
            download_attachments=args.download_attachments,
            include_archived=not args.exclude_archived,
            task_gid=args.task_gid,
            resume=args.resume,
            checkpoint_interval=args.checkpoint_interval,
        )

        stats = importer.import_tasks()

        print("\n=== Import Complete ===")
        print(f"Tasks fetched: {stats['tasks_fetched']}")
        print(f"Tasks updated: {stats['tasks_updated']}")
        print(f"Tasks added: {stats['tasks_new']}")
        print(f"Tasks skipped (unchanged): {stats['tasks_skipped']}")
        print(f"Total tasks in parquet: {stats['tasks_total']}")
        if "projects_imported" in stats:
            print(f"Projects imported/updated: {stats['projects_imported']}")
        if "attachments_downloaded" in stats:
            print(f"Attachments downloaded: {stats['attachments_downloaded']}")

    except Exception as e:
        print(f"Error during import: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
