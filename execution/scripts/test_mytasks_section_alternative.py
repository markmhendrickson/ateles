#!/usr/bin/env python3
"""
Test alternative approaches to adding tasks to My Tasks sections.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.client import AsanaClientWrapper
from scripts.config import AsanaConfig


def test_alternative_approaches():
    """Test alternative ways to add tasks to My Tasks sections."""

    config = AsanaConfig.from_env()
    AsanaClientWrapper.from_config_source(config)
    target_client = AsanaClientWrapper.from_config_target(config)

    print("=== Testing Alternative My Tasks Section Approaches ===\n")

    # Approach 1: Try to get sections from a task that's already in My Tasks
    print("1. Testing: Get sections from existing My Tasks task...")
    try:
        # Get a task assigned to me in target workspace
        opts = {
            "assignee": "me",
            "workspace": config.target_workspace_gid,
            "opt_fields": "gid,name,assignee_section,assignee_section.gid,assignee_section.name",
            "limit": 1,
        }
        tasks = list(target_client._with_retry(target_client.tasks.get_tasks, opts))
        if tasks:
            task = tasks[0]
            assignee_section = task.get("assignee_section", {})
            if assignee_section:
                section_gid = assignee_section.get("gid")
                section_name = assignee_section.get("name")
                print(
                    f"   Found task in My Tasks section: {section_name} ({section_gid})"
                )

                # Try to get sections for this section's project (which should be the user_task_list)
                if section_gid:
                    # Try to get the section details
                    section_opts = {
                        "opt_fields": "gid,name,project,project.gid,project.name"
                    }
                    try:
                        section_data = target_client._with_retry(
                            target_client.sections.get_section,
                            section_gid,
                            section_opts,
                        )
                        project = section_data.get("project", {})
                        project_gid = (
                            project.get("gid") if isinstance(project, dict) else project
                        )
                        print(f"   Section's project GID: {project_gid}")
                        print("   This project GID might be the user_task_list!")

                        # Try to get all sections from this project
                        sections = list(
                            target_client._with_retry(
                                target_client.sections.get_sections_for_project,
                                project_gid,
                                {},
                            )
                        )
                        print(f"   Found {len(sections)} sections in this project:")
                        for s in sections:
                            print(f"     - {s.get('name')} ({s.get('gid')})")
                    except Exception as e:
                        print(f"   Error getting section details: {e}")
        else:
            print("   No tasks found assigned to me in target workspace")
    except Exception as e:
        print(f"   Error: {e}")

    print()

    # Approach 2: Try to create a section directly using a known pattern
    print("2. Testing: Try to find user_task_list by searching projects...")
    try:
        # Get all projects in workspace
        opts = {"workspace": config.target_workspace_gid, "archived": False}
        projects = list(
            target_client._with_retry(target_client.projects.get_projects, opts)
        )
        print(f"   Found {len(projects)} projects")

        # Look for a project that might be the user_task_list (usually has a specific name or pattern)
        for project in projects[:10]:  # Check first 10
            proj_gid = project.get("gid")
            proj_name = project.get("name", "")
            print(f"   Checking project: {proj_name} ({proj_gid})")

            # Try to get sections from this project
            try:
                sections = list(
                    target_client._with_retry(
                        target_client.sections.get_sections_for_project, proj_gid, {}
                    )
                )
                section_names = [s.get("name") for s in sections]
                if any(
                    name
                    in ["Today", "This week", "Later", "Recently assigned", "Scheduled"]
                    for name in section_names
                ):
                    print(
                        f"     ^ This looks like a user_task_list! Sections: {section_names}"
                    )
            except Exception:
                pass
    except Exception as e:
        print(f"   Error: {e}")


if __name__ == "__main__":
    test_alternative_approaches()
