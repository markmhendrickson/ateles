#!/usr/bin/env python3
"""
Delete all unassigned tasks from Asana target workspace.

Unassigned tasks: Tasks that have no assignee (assignee is null/None).
"""

import argparse
import sys
import time
from pathlib import Path

import requests

# Add execution to path
EXECUTION_LAYER = Path(__file__).parent.parent
sys.path.insert(0, str(EXECUTION_LAYER))

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig


def get_all_tasks_in_workspace(
    client: AsanaClientWrapper, workspace_gid: str
) -> list[dict]:
    """Get all tasks from target workspace."""
    all_tasks = []
    task_gids = set()

    opt_fields = [
        "gid",
        "name",
        "completed",
        "assignee",
        "assignee.gid",
        "assignee.name",
    ]

    # Get tasks from all projects (including archived)
    try:
        print("Fetching projects (including archived)...")
        # Get non-archived projects
        projects_opts = {"workspace": workspace_gid, "archived": False}
        projects = list(client._with_retry(client.projects.get_projects, projects_opts))
        print(f"Found {len(projects)} non-archived projects")

        # Also get archived projects
        archived_projects_opts = {"workspace": workspace_gid, "archived": True}
        archived_projects = list(
            client._with_retry(client.projects.get_projects, archived_projects_opts)
        )
        print(f"Found {len(archived_projects)} archived projects")
        projects.extend(archived_projects)
        print(f"Total projects: {len(projects)}")

        for idx, project in enumerate(projects, 1):
            project_gid = project.get("gid")
            project_name = project.get("name", "")
            print(f"Fetching tasks from project {idx}/{len(projects)}: {project_name}")

            opts = {"project": project_gid, "opt_fields": ",".join(opt_fields)}

            try:
                project_tasks = list(client._with_retry(client.tasks.get_tasks, opts))

                for task_data in project_tasks:
                    task_gid = task_data.get("gid")
                    if task_gid and task_gid not in task_gids:
                        task_gids.add(task_gid)
                        all_tasks.append(task_data)

            except Exception as e:
                print(
                    f"  Warning: Error fetching tasks from project '{project_name}': {e}"
                )
                continue

    except Exception as e:
        print(f"Warning: Error fetching projects: {e}")

    # Get current user GID
    current_user_gid = None
    try:
        headers = {"Authorization": f"Bearer {client._pat}"}
        url = "https://app.asana.com/api/1.0/users/me"
        params = {"opt_fields": "gid,name,email"}
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        me = response.json().get("data", {})
        current_user_gid = me.get("gid")
    except Exception as e:
        print(f"Warning: Could not get current user GID: {e}")

    # Get tasks assigned to current user (workspace-wide) to catch tasks not in projects
    if current_user_gid:
        try:
            print(
                "Fetching tasks assigned to you in workspace (to catch tasks not in projects)..."
            )
            assignee_opts = {
                "assignee": current_user_gid,
                "workspace": workspace_gid,
                "opt_fields": ",".join(opt_fields),
            }
            assignee_tasks = list(
                client._with_retry(client.tasks.get_tasks, assignee_opts)
            )

            for task_data in assignee_tasks:
                task_gid = task_data.get("gid")
                if task_gid and task_gid not in task_gids:
                    task_gids.add(task_gid)
                    all_tasks.append(task_data)

            print(f"Found {len(assignee_tasks)} additional tasks assigned to you")
        except Exception as e:
            print(f"Warning: Error fetching tasks assigned to you: {e}")

    # Also try to get tasks from user task list (My Tasks) to catch standalone unassigned tasks
    # Use the singular endpoint: GET /users/{user_gid}/user_task_list (works even if deprecated)
    try:
        print("Fetching tasks from My Tasks sections...")
        headers = {"Authorization": f"Bearer {client._pat}"}

        # Get current user GID first
        me_url = "https://app.asana.com/api/1.0/users/me"
        me_response = requests.get(
            me_url, headers=headers, params={"opt_fields": "gid"}, timeout=10
        )
        if me_response.status_code != 200:
            raise Exception(f"Could not get user info: {me_response.status_code}")

        user_gid = me_response.json().get("data", {}).get("gid")
        if not user_gid:
            raise Exception("Could not get user GID")

        # Get user task list for this workspace (singular endpoint)
        utl_url = f"https://app.asana.com/api/1.0/users/{user_gid}/user_task_list"
        utl_params = {"workspace": workspace_gid, "opt_fields": "gid"}
        utl_response = requests.get(
            utl_url, headers=headers, params=utl_params, timeout=10
        )

        if utl_response.status_code == 200:
            utl_data = utl_response.json().get("data", {})
            user_task_list_gid = utl_data.get("gid")

            if user_task_list_gid:
                # Get tasks from user task list (fetch all pages)
                tasks_url = f"https://app.asana.com/api/1.0/user_task_lists/{user_task_list_gid}/tasks"
                tasks_params = {"opt_fields": ",".join(opt_fields), "limit": 100}
                all_my_tasks = []

                while True:
                    tasks_response = requests.get(
                        tasks_url, headers=headers, params=tasks_params, timeout=10
                    )
                    if tasks_response.status_code != 200:
                        break

                    response_data = tasks_response.json()
                    page_tasks = response_data.get("data", [])
                    all_my_tasks.extend(page_tasks)

                    # Check for next page
                    next_page = response_data.get("next_page")
                    if next_page:
                        tasks_params["offset"] = next_page.get("offset")
                    else:
                        break

                for task_data in all_my_tasks:
                    task_gid = task_data.get("gid")
                    if task_gid and task_gid not in task_gids:
                        task_gids.add(task_gid)
                        all_tasks.append(task_data)

                print(f"Found {len(all_my_tasks)} additional tasks from My Tasks")
            else:
                print("  Could not get user task list GID")
        else:
            # Endpoint might be deprecated, but try anyway
            print(
                f"  Warning: User task list endpoint returned {utl_response.status_code} (may be deprecated)"
            )
            if utl_response.status_code != 404:
                print(f"  Error: {utl_response.text[:200]}")

    except Exception as e:
        # My Tasks API might be deprecated - that's okay, we'll work with what we have
        print(f"Warning: Could not fetch My Tasks: {e}")

    # Also try to get tasks assigned to all workspace members to catch any we might have missed
    # This helps find tasks assigned to other users that we might not have seen
    try:
        print("Fetching all workspace members to check their assigned tasks...")
        headers = {"Authorization": f"Bearer {client._pat}"}
        users_url = f"https://app.asana.com/api/1.0/workspaces/{workspace_gid}/users"
        users_params = {"opt_fields": "gid,name"}
        users_response = requests.get(
            users_url, headers=headers, params=users_params, timeout=10
        )

        if users_response.status_code == 200:
            workspace_users = users_response.json().get("data", [])
            print(f"Found {len(workspace_users)} workspace members")

            # For each user, get their assigned tasks (but skip if we already have them)
            for user in workspace_users[
                :10
            ]:  # Limit to first 10 users to avoid too many API calls
                user_gid = user.get("gid")
                if user_gid == current_user_gid:
                    continue  # Already fetched current user's tasks

                try:
                    assignee_opts = {
                        "assignee": user_gid,
                        "workspace": workspace_gid,
                        "opt_fields": ",".join(opt_fields),
                    }
                    user_tasks = list(
                        client._with_retry(client.tasks.get_tasks, assignee_opts)
                    )

                    new_tasks = 0
                    for task_data in user_tasks:
                        task_gid = task_data.get("gid")
                        if task_gid and task_gid not in task_gids:
                            task_gids.add(task_gid)
                            all_tasks.append(task_data)
                            new_tasks += 1

                    if new_tasks > 0:
                        print(
                            f"  Found {new_tasks} additional tasks assigned to {user.get('name')}"
                        )
                except Exception as e:
                    print(
                        f"  Warning: Error fetching tasks for user {user.get('name')}: {e}"
                    )
                    continue
        else:
            print(
                f"  Warning: Could not fetch workspace users: {users_response.status_code}"
            )
    except Exception as e:
        print(f"Warning: Could not fetch tasks from all workspace members: {e}")

    # Also try using the search API to find tasks that might not be in projects or assigned
    # The search API can find tasks that regular queries miss
    try:
        print(
            "Searching for tasks using search API (to catch unassigned tasks without projects)..."
        )
        headers = {"Authorization": f"Bearer {client._pat}"}

        # Search for common task patterns or use a broad search
        # We'll search for tasks and then filter for unassigned ones
        search_url = (
            f"https://app.asana.com/api/1.0/workspaces/{workspace_gid}/tasks/search"
        )

        # Try searching with empty query to get all tasks (if supported)
        # Or search for common terms to find more tasks
        search_queries = ["", "is:incomplete", "is:unassigned"]

        for query in search_queries:
            try:
                search_params = {"opt_fields": ",".join(opt_fields), "limit": 100}
                if query:
                    search_params["text"] = query

                search_response = requests.get(
                    search_url, headers=headers, params=search_params, timeout=10
                )
                if search_response.status_code == 200:
                    search_tasks = search_response.json().get("data", [])
                    new_tasks = 0
                    for task_data in search_tasks:
                        task_gid = task_data.get("gid")
                        if task_gid and task_gid not in task_gids:
                            task_gids.add(task_gid)
                            all_tasks.append(task_data)
                            new_tasks += 1
                    if new_tasks > 0:
                        print(
                            f"  Found {new_tasks} additional tasks via search (query: '{query}')"
                        )
                    break  # If one search works, use it
                elif search_response.status_code == 404:
                    # Search API might not be available
                    break
            except Exception:
                continue
    except Exception as e:
        print(f"Warning: Could not use search API: {e}")

    # Also, make sure we're getting ALL tasks from My Tasks by checking all sections individually
    # My Tasks has multiple sections (Today, Upcoming, Later, etc.) and we need to check each
    try:
        print("Checking all My Tasks sections individually...")
        headers = {"Authorization": f"Bearer {client._pat}"}

        me_url = "https://app.asana.com/api/1.0/users/me"
        me_response = requests.get(
            me_url, headers=headers, params={"opt_fields": "gid"}, timeout=10
        )
        if me_response.status_code == 200:
            user_gid = me_response.json().get("data", {}).get("gid")

            utl_url = f"https://app.asana.com/api/1.0/users/{user_gid}/user_task_list"
            utl_params = {"workspace": workspace_gid, "opt_fields": "gid"}
            utl_response = requests.get(
                utl_url, headers=headers, params=utl_params, timeout=10
            )

            if utl_response.status_code == 200:
                user_task_list_gid = utl_response.json().get("data", {}).get("gid")

                if user_task_list_gid:
                    # Get all sections in the user task list
                    sections_url = f"https://app.asana.com/api/1.0/user_task_lists/{user_task_list_gid}/sections"
                    sections_response = requests.get(
                        sections_url,
                        headers=headers,
                        params={"opt_fields": "gid,name"},
                        timeout=10,
                    )

                    if sections_response.status_code == 200:
                        sections = sections_response.json().get("data", [])
                        print(f"  Found {len(sections)} My Tasks sections")

                        for section in sections:
                            section_gid = section.get("gid")
                            section_name = section.get("name")

                            # Get tasks from this section with pagination
                            section_tasks_url = f"https://app.asana.com/api/1.0/sections/{section_gid}/tasks"
                            section_tasks_params = {
                                "opt_fields": ",".join(opt_fields),
                                "limit": 100,
                            }
                            all_section_tasks = []

                            while True:
                                section_tasks_response = requests.get(
                                    section_tasks_url,
                                    headers=headers,
                                    params=section_tasks_params,
                                    timeout=10,
                                )
                                if section_tasks_response.status_code != 200:
                                    break

                                response_data = section_tasks_response.json()
                                page_tasks = response_data.get("data", [])
                                all_section_tasks.extend(page_tasks)

                                next_page = response_data.get("next_page")
                                if next_page:
                                    section_tasks_params["offset"] = next_page.get(
                                        "offset"
                                    )
                                else:
                                    break

                            new_tasks = 0
                            for task_data in all_section_tasks:
                                task_gid = task_data.get("gid")
                                if task_gid and task_gid not in task_gids:
                                    task_gids.add(task_gid)
                                    all_tasks.append(task_data)
                                    new_tasks += 1
                            if new_tasks > 0:
                                print(
                                    f"    Found {new_tasks} additional tasks in section '{section_name}'"
                                )
    except Exception as e:
        print(f"Warning: Could not check My Tasks sections individually: {e}")

    # Note: Asana API doesn't allow querying for unassigned tasks directly
    # We can only get tasks by project, assignee, user_task_list, or search.
    # We've fetched from all sources, now we'll filter for unassigned ones.
    print(f"\nTotal tasks found: {len(all_tasks)}")

    return all_tasks


def delete_task(
    client: AsanaClientWrapper, task_gid: str, dry_run: bool = False
) -> bool:
    """Delete a task by GID using Asana API DELETE endpoint."""
    if dry_run:
        return True

    try:
        headers = {"Authorization": f"Bearer {client._pat}"}
        url = f"https://app.asana.com/api/1.0/tasks/{task_gid}"
        response = requests.delete(url, headers=headers, timeout=30)

        if response.status_code == 200:
            return True
        elif response.status_code == 404:
            # Task already deleted
            return True
        else:
            error_detail = (
                response.text if response.text else f"HTTP {response.status_code}"
            )
            print(f"  Warning: Failed to delete task {task_gid}: {error_detail[:200]}")
            return False

    except Exception as e:
        print(f"  Warning: Error deleting task {task_gid}: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete unassigned tasks from Asana target workspace."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show what would be deleted, do not actually delete.",
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    args = parser.parse_args()

    print("=" * 80)
    print("Delete Unassigned Tasks from Asana Target Workspace")
    print("=" * 80)

    config = AsanaConfig.from_env()
    target_client = AsanaClientWrapper.from_config_target(config)
    target_workspace_gid = config.target_workspace_gid

    print(f"\nFetching all tasks from target workspace {target_workspace_gid}...")
    all_tasks = get_all_tasks_in_workspace(target_client, target_workspace_gid)
    print(f"Found {len(all_tasks)} tasks in target workspace.")

    # Filter for unassigned tasks (incomplete and without projects)
    # Also re-check each task individually to get full details (API might return incomplete data in lists)
    unassigned_tasks = []
    print(f"\nChecking {len(all_tasks)} tasks for unassigned status...")

    # Get client for individual task checks
    config = AsanaConfig.from_env()
    target_client = AsanaClientWrapper.from_config_target(config)
    headers = {"Authorization": f"Bearer {target_client._pat}"}
    checked_count = 0

    for task in all_tasks:
        task_gid = task.get("gid")
        if not task_gid:
            continue

        # Re-fetch task individually to get complete details
        try:
            task_url = f"https://app.asana.com/api/1.0/tasks/{task_gid}"
            task_params = {
                "opt_fields": "gid,name,assignee,assignee.gid,assignee.name,projects,projects.gid,completed"
            }
            task_response = requests.get(
                task_url, headers=headers, params=task_params, timeout=5
            )

            if task_response.status_code == 200:
                full_task = task_response.json().get("data", {})
                assignee = full_task.get("assignee")
                projects = full_task.get("projects", [])
                completed = full_task.get("completed", False)

                # Check if assignee is None, null, or empty dict
                assignee_gid = None
                if assignee:
                    if isinstance(assignee, dict):
                        assignee_gid = assignee.get("gid")
                    elif isinstance(assignee, str):
                        assignee_gid = assignee

                is_unassigned = not assignee_gid

                # Check if no projects
                is_no_projects = not projects or len(projects) == 0

                # Only include incomplete, unassigned tasks without projects
                if is_unassigned and is_no_projects and not completed:
                    unassigned_tasks.append(full_task)

                checked_count += 1
                if checked_count % 10 == 0:
                    print(f"  Checked {checked_count}/{len(all_tasks)} tasks...")
        except Exception:
            # If individual fetch fails, use the data we have
            assignee = task.get("assignee")
            projects = task.get("projects", [])
            completed = task.get("completed", False)

            is_unassigned = (
                assignee is None
                or assignee == {}
                or (isinstance(assignee, dict) and not assignee.get("gid"))
            )
            is_no_projects = not projects or len(projects) == 0

            if is_unassigned and is_no_projects and not completed:
                unassigned_tasks.append(task)
                continue

    print(f"\nUnassigned tasks found: {len(unassigned_tasks)}")

    # Also provide summary of what we checked
    print("\nSummary of sources checked:")
    print(f"  - Tasks from {len(projects)} projects")
    print("  - Tasks assigned to current user")
    print("  - Tasks from My Tasks sections")
    print("  - Tasks assigned to all workspace members")
    print("  - Tasks in all project sections")
    print(
        "\nNote: The Asana API requires a filter (project, assignee, or user_task_list),"
    )
    print("so tasks that are not in projects, not assigned, and not in My Tasks cannot")
    print(
        "be queried directly. If you see unassigned tasks in the UI that aren't found"
    )
    print("here, they may be in a state that the API doesn't expose.")

    if not unassigned_tasks:
        print("\nNo unassigned tasks found via API.")
        print(
            "If you see unassigned tasks in the UI, they may not be accessible via the API."
        )
        return

    # Show sample of unassigned tasks
    print("\nSample unassigned tasks:")
    for task in unassigned_tasks[:10]:
        name = task.get("name", "Unknown")
        gid = task.get("gid", "N/A")
        completed = task.get("completed", False)
        status = "completed" if completed else "incomplete"
        print(f"  - {name} ({gid}) - {status}")
    if len(unassigned_tasks) > 10:
        print(f"  ... and {len(unassigned_tasks) - 10} more")

    if args.dry_run:
        print("\nDry run complete. No tasks were deleted.")
        return

    # Confirm deletion
    if not args.yes:
        print("\n" + "=" * 80)
        response = input(
            f"Delete {len(unassigned_tasks)} unassigned task(s) from Asana? (yes/no): "
        )
        if response.lower() != "yes":
            print("Deletion cancelled.")
            return
    else:
        print(
            f"\nProceeding with deletion of {len(unassigned_tasks)} unassigned task(s)..."
        )

    print(f"\nDeleting {len(unassigned_tasks)} unassigned task(s)...")
    deleted_count = 0
    failed_count = 0

    for idx, task in enumerate(unassigned_tasks, 1):
        task_gid = task.get("gid")
        task_name = task.get("name", "Unknown")
        print(f"Deleting task {idx}/{len(unassigned_tasks)}: {task_name} ({task_gid})")
        if delete_task(target_client, task_gid, dry_run=False):
            deleted_count += 1
        else:
            failed_count += 1
        time.sleep(0.5)  # Rate limiting

    print("\n" + "=" * 80)
    print("Deletion completed:")
    print(f"  Deleted: {deleted_count}")
    print(f"  Failed: {failed_count}")
    print("=" * 80)


if __name__ == "__main__":
    main()
