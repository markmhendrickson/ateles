#!/usr/bin/env python3
"""
Recreate therapy payment task based on next Google Calendar appointment.

This script:
1. Searches Google Calendar for the next therapy appointment
2. Calculates the day after that appointment
3. Creates a new task to pay for therapy on that date

Usage:
    python execution/scripts/recreate_therapy_payment_task.py
"""

import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.config import DATA_DIR

# Try to import Google Calendar API client
try:
    import pickle

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    GOOGLE_CALENDAR_AVAILABLE = True
except ImportError:
    GOOGLE_CALENDAR_AVAILABLE = False
    print(
        "Warning: Google Calendar API libraries not available. Install with: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client"
    )

# Data file paths
TASKS_FILE = DATA_DIR / "tasks" / "tasks.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def get_google_calendar_service():
    """Get authenticated Google Calendar service."""
    if not GOOGLE_CALENDAR_AVAILABLE:
        return None

    token_path = Path.home() / ".config" / "google-calendar-mcp" / "tokens.json"
    (Path.home() / ".config" / "google-calendar-mcp" / "credentials.json")

    # Try to load existing token
    if token_path.exists():
        try:
            with open(token_path) as token:
                # Tokens are stored as JSON, need to convert to Credentials object
                import json

                json.load(token)
                # This is a simplified approach - actual implementation may vary
                # For now, we'll try to use the MCP server approach instead
                pass
        except Exception as e:
            print(f"Error loading token: {e}")

    # If we can't get credentials, return None and use MCP approach
    return None


def search_therapy_appointments_mcp() -> date | None:
    """
    Search for next therapy appointment using MCP Google Calendar tools.
    This function should be called from an environment that has MCP access.
    Returns the date of the next therapy appointment, or None if not found.
    """
    # This function is a placeholder - actual implementation would use MCP tools
    # For now, we'll create a helper that can be called with MCP context
    print(
        "Note: This script should be run in an environment with MCP Google Calendar access"
    )
    print("or use the direct Google Calendar API with proper authentication.")
    return None


def find_next_therapy_appointment() -> date | None:
    """
    Find the next therapy appointment from Google Calendar.
    Returns the date of the appointment, or None if not found.
    """
    # Try MCP approach first (if available in context)
    # Otherwise, try direct API

    # For now, we'll use a simple approach: search events data
    # Check if events are imported
    events_file = DATA_DIR / "events" / "events.parquet"
    if events_file.exists():
        try:
            df = pd.read_parquet(events_file)
            today = date.today()

            # Search for therapy-related events in the future
            # Query therapy contact from contacts.parquet to get search keywords
            # For now, use generic keywords (can be enhanced to query contact name)
            therapy_keywords = ["therapy", "terapia"]
            future_events = df[
                (df["start_date"] > today)
                & (
                    df["name"]
                    .str.lower()
                    .str.contains("|".join(therapy_keywords), case=False, na=False)
                )
            ]

            if not future_events.empty:
                # Get the earliest future therapy event
                next_event = future_events.nsmallest(1, "start_date").iloc[0]
                appointment_date = next_event["start_date"]
                if isinstance(appointment_date, pd.Timestamp):
                    appointment_date = appointment_date.date()
                print(f"Found next therapy appointment: {appointment_date}")
                return appointment_date
        except Exception as e:
            print(f"Error searching events parquet: {e}")

    print("Could not find next therapy appointment. Please ensure:")
    print("1. Google Calendar events are imported to data/events/events.parquet")
    print("2. Or run this script in an environment with MCP Google Calendar access")
    return None


def create_therapy_payment_task(appointment_date: date) -> str:
    """
    Create a new therapy payment task for the day after the appointment.
    Returns the task_id of the created task.
    """
    # Calculate due date (day after appointment)
    due_date = appointment_date + timedelta(days=1)

    # Create snapshot
    if TASKS_FILE.exists():
        df = pd.read_parquet(TASKS_FILE)
        SNAPSHOTS_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        df.to_parquet(SNAPSHOTS_DIR / f"tasks-{timestamp}.parquet", index=False)
        print(f"Created snapshot: {SNAPSHOTS_DIR / f'tasks-{timestamp}.parquet'}")
    else:
        df = pd.DataFrame()

    # Generate task ID
    task_id = str(uuid.uuid4())[:16]

    # Create task record
    now = datetime.now()
    today = date.today()

    # Query therapy contact from contacts.parquet (contact_id: 578f6ce3-f9a4-4f)
    # For now, use generic description (can be enhanced to query contact name)
    therapy_contact_name = (
        "therapy provider"  # TODO: Query from contacts.parquet via MCP
    )

    new_task = {
        "task_id": task_id,
        "title": "Pay €60 for therapy session if occurred yesterday",
        "description": f"Check if therapy session occurred on {appointment_date.strftime('%Y-%m-%d')} and pay €60 to {therapy_contact_name} via Wise if it did.",
        "description_html": None,
        "description_html_remote": None,
        "domain": "finance",
        "status": "pending",
        "due_date": due_date,
        "start_date": None,
        "completed_date": None,
        "recurrence": None,
        "execution_plan_path": None,
        "notes": f"Auto-created based on therapy appointment on {appointment_date.strftime('%Y-%m-%d')}. Payment: €60 to therapy provider (contact_id: 578f6ce3-f9a4-4f, query contacts.parquet for IBAN) via Wise.",
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
        "created_at": now,
        "updated_at": now,
        "asana_workspace": None,
        "asana_source_gid": None,
        "asana_target_gid": None,
        "parent_task_id": None,
        "permalink_url": None,
        "followers_gids": None,
        "follower_names": None,
        "import_date": today,
        "import_source_file": "recreate_therapy_payment_task",
    }

    # Add new task to dataframe
    new_df = pd.DataFrame([new_task])
    df = pd.concat([df, new_df], ignore_index=True)

    # Save
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(TASKS_FILE, index=False)

    print("Created therapy payment task:")
    print(f"  Task ID: {task_id}")
    print(f"  Title: {new_task['title']}")
    print(f"  Due date: {due_date}")
    print(f"  Based on appointment: {appointment_date}")

    return task_id


def main():
    """Main function."""
    print(
        "Recreating therapy payment task based on next Google Calendar appointment..."
    )

    # Find next therapy appointment
    appointment_date = find_next_therapy_appointment()

    if not appointment_date:
        print("\nError: Could not find next therapy appointment.")
        print(
            "Please ensure Google Calendar events are imported or run with MCP access."
        )
        sys.exit(1)

    # Create task for day after appointment
    task_id = create_therapy_payment_task(appointment_date)

    print(f"\n✓ Successfully created task {task_id}")


if __name__ == "__main__":
    main()
