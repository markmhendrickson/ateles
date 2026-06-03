"""
Test task data generators for comprehensive testing.

Provides fixtures and utilities for generating test tasks with various
property combinations, edge cases, and boundary conditions.
"""

import uuid
from datetime import date, datetime, timedelta
from typing import Any


def generate_task_id() -> str:
    """Generate a unique task ID."""
    return str(uuid.uuid4())[:16]


def generate_basic_task(
    title: str = "Test Task", status: str = "pending", **overrides
) -> dict[str, Any]:
    """Generate a basic test task with minimal properties."""
    task = {
        "task_id": generate_task_id(),
        "title": title,
        "description": None,
        "description_html": None,
        "status": status,
        "due_date": None,
        "start_date": None,
        "completed_date": None,
        "updated_at": datetime.now(),
        "domain": "work",
        "project_names": None,
        "section_names": None,
        "tags": None,
        "assignee_gid": None,
        "followers_gids": None,
        "asana_source_gid": None,
        "asana_target_gid": None,
        "sync_log": None,
        "sync_datetime": None,
        "import_date": date.today(),
        "import_source_file": "test_fixture",
    }
    task.update(overrides)
    return task


def generate_full_property_task() -> dict[str, Any]:
    """Generate a task with all supported properties populated."""
    return generate_basic_task(
        title="Full Property Test Task",
        description="This is a test task with all properties populated.\n\nMultiple paragraphs of text.",
        description_html="<body>This is a test task with all properties populated.<br><br>Multiple paragraphs of text.</body>",
        status="in_progress",
        due_date=date.today() + timedelta(days=7),
        start_date=date.today(),
        updated_at=datetime.now(),
        domain="finance",
        project_names="Test Project|Another Project",
        section_names="Section 1|Section 2",
        tags="tag1|tag2|tag3",
        assignee_gid="1234567890",
        followers_gids="1234567890|0987654321",
        asana_source_gid="source_gid_123",
        sync_log="exported",
        sync_datetime=datetime.now(),
    )


def generate_edge_case_tasks() -> list[dict[str, Any]]:
    """Generate tasks with edge cases."""
    return [
        # Empty title (should fail validation)
        generate_basic_task(title=""),
        # Very long title
        generate_basic_task(title="A" * 1024),
        # Special characters in title
        generate_basic_task(title="Test 日本語 émojis 🎉 & symbols: <>&\"'"),
        # Unicode in description
        generate_basic_task(
            description="Unicode test: 日本語 中文 한국어 العربية עברית Ελληνικά",
        ),
        # Very long description
        generate_basic_task(
            description="Long description: " + ("Lorem ipsum dolor sit amet. " * 200),
        ),
        # Past due date
        generate_basic_task(due_date=date.today() - timedelta(days=30)),
        # Far future due date
        generate_basic_task(due_date=date.today() + timedelta(days=365 * 5)),
        # Start date after due date (invalid)
        generate_basic_task(
            start_date=date.today() + timedelta(days=10),
            due_date=date.today() + timedelta(days=5),
        ),
        # Completed task with no completed_date
        generate_basic_task(status="completed", completed_date=None),
        # Multiple projects
        generate_basic_task(
            project_names="|".join([f"Project {i}" for i in range(10)]),
        ),
        # Multiple sections
        generate_basic_task(
            section_names="|".join([f"Section {i}" for i in range(10)]),
        ),
        # Many tags
        generate_basic_task(
            tags="|".join([f"tag{i}" for i in range(50)]),
        ),
        # Multiple followers
        generate_basic_task(
            followers_gids="|".join([str(i) * 10 for i in range(10)]),
        ),
    ]


def generate_status_variants() -> list[dict[str, Any]]:
    """Generate tasks with different status values."""
    statuses = ["pending", "in_progress", "completed", "blocked", "cancelled"]
    return [
        generate_basic_task(
            title=f"Task with status: {status}",
            status=status,
            completed_date=date.today() if status == "completed" else None,
        )
        for status in statuses
    ]


def generate_domain_variants() -> list[dict[str, Any]]:
    """Generate tasks with different domain values."""
    domains = ["finance", "admin", "health", "work", "social"]
    return [
        generate_basic_task(
            title=f"Task in domain: {domain}",
            domain=domain,
        )
        for domain in domains
    ]


def generate_date_combinations() -> list[dict[str, Any]]:
    """Generate tasks with various date combinations."""
    today = date.today()
    return [
        # No dates
        generate_basic_task(due_date=None, start_date=None),
        # Only due date
        generate_basic_task(due_date=today + timedelta(days=7), start_date=None),
        # Only start date
        generate_basic_task(due_date=None, start_date=today),
        # Both dates
        generate_basic_task(
            due_date=today + timedelta(days=7),
            start_date=today,
        ),
        # Same start and due date
        generate_basic_task(
            due_date=today,
            start_date=today,
        ),
        # Past dates
        generate_basic_task(
            due_date=today - timedelta(days=7),
            start_date=today - timedelta(days=14),
        ),
        # Future dates
        generate_basic_task(
            due_date=today + timedelta(days=365),
            start_date=today + timedelta(days=358),
        ),
    ]


def generate_project_section_combinations() -> list[dict[str, Any]]:
    """Generate tasks with various project/section combinations."""
    return [
        # No project, no section
        generate_basic_task(project_names=None, section_names=None),
        # One project, no section
        generate_basic_task(project_names="Project 1", section_names=None),
        # One project, one section
        generate_basic_task(project_names="Project 1", section_names="Section 1"),
        # Multiple projects, no sections
        generate_basic_task(
            project_names="Project 1|Project 2",
            section_names=None,
        ),
        # Multiple projects, multiple sections
        generate_basic_task(
            project_names="Project 1|Project 2",
            section_names="Section 1|Section 2",
        ),
        # No project, but has section (edge case)
        generate_basic_task(project_names=None, section_names="Section 1"),
    ]


def generate_assignment_combinations() -> list[dict[str, Any]]:
    """Generate tasks with various assignment/follower combinations."""
    return [
        # No assignee, no followers
        generate_basic_task(assignee_gid=None, followers_gids=None),
        # Assignee, no followers
        generate_basic_task(assignee_gid="1234567890", followers_gids=None),
        # No assignee, one follower
        generate_basic_task(assignee_gid=None, followers_gids="1234567890"),
        # Assignee and followers (assignee is also follower)
        generate_basic_task(
            assignee_gid="1234567890",
            followers_gids="1234567890|0987654321",
        ),
        # Assignee and many followers
        generate_basic_task(
            assignee_gid="1234567890",
            followers_gids="|".join([str(i) * 10 for i in range(1, 11)]),
        ),
    ]


def generate_sync_state_combinations() -> list[dict[str, Any]]:
    """Generate tasks with various sync states."""
    return [
        # Never synced
        generate_basic_task(
            asana_source_gid=None,
            asana_target_gid=None,
            sync_log=None,
            sync_datetime=None,
        ),
        # Synced to source only
        generate_basic_task(
            asana_source_gid="source_gid_123",
            asana_target_gid=None,
            sync_log="exported",
            sync_datetime=datetime.now(),
        ),
        # Synced to target only
        generate_basic_task(
            asana_source_gid=None,
            asana_target_gid="target_gid_123",
            sync_log="exported",
            sync_datetime=datetime.now(),
        ),
        # Synced to both
        generate_basic_task(
            asana_source_gid="source_gid_123",
            asana_target_gid="target_gid_123",
            sync_log="exported",
            sync_datetime=datetime.now(),
        ),
        # Sync failed
        generate_basic_task(
            asana_source_gid=None,
            asana_target_gid=None,
            sync_log="failed:APIError",
            sync_datetime=datetime.now(),
        ),
        # Pending export
        generate_basic_task(
            sync_log="pending_export",
            sync_datetime=None,
        ),
    ]


def generate_all_permutations() -> list[dict[str, Any]]:
    """Generate comprehensive set of task permutations."""
    tasks = []

    # Basic variants
    tasks.append(generate_basic_task())
    tasks.append(generate_full_property_task())

    # Edge cases
    tasks.extend(generate_edge_case_tasks())

    # Status variants
    tasks.extend(generate_status_variants())

    # Domain variants
    tasks.extend(generate_domain_variants())

    # Date combinations
    tasks.extend(generate_date_combinations())

    # Project/section combinations
    tasks.extend(generate_project_section_combinations())

    # Assignment combinations
    tasks.extend(generate_assignment_combinations())

    # Sync state combinations
    tasks.extend(generate_sync_state_combinations())

    return tasks
