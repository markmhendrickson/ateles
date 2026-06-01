#!/usr/bin/env python3
"""
Extract birthday dates from Google Calendar and tasks, then update contacts and people records.

This script:
1. Queries Google Calendar for birthday events
2. Extracts birthday dates from task titles and due dates
3. Matches names to contacts and people records
4. Updates records with birthday information
"""

import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Read existing data
# Script is in execution/scripts/, so go up two levels to get to project root
script_dir = os.path.dirname(os.path.abspath(__file__))
base_dir = os.path.dirname(os.path.dirname(script_dir))
sys.path.insert(0, base_dir)
from scripts.config import get_data_dir

DATA_DIR = Path(get_data_dir())
contacts_path = DATA_DIR / "contacts/contacts.parquet"
people_path = DATA_DIR / "people/people.parquet"
tasks_path = DATA_DIR / "tasks/tasks.parquet"
snapshots_dir = DATA_DIR / "snapshots"

# Ensure snapshots directory exists
os.makedirs(snapshots_dir, exist_ok=True)


def create_snapshot(file_path: str) -> str:
    """Create timestamped snapshot of parquet file."""
    if not os.path.exists(file_path):
        return None

    filename = os.path.basename(file_path).replace(".parquet", "")
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    snapshot_path = os.path.join(snapshots_dir, f"{filename}-{timestamp}.parquet")

    df = pd.read_parquet(file_path)
    df.to_parquet(snapshot_path, index=False)
    print(f"Created snapshot: {snapshot_path}")
    return snapshot_path


def extract_birthday_from_task_title(
    title: str, due_date: date | None = None
) -> tuple[str, date] | None:
    """
    Extract name and birthday date from task title.
    Returns (name, birthday_date) or None.
    """
    title_lower = title.lower()

    # Skip tasks that are about organizing/planning but not specific birthdays
    skip_patterns = [
        r"add.*birthday.*calendar",
        r"organize.*birthday",
        r"plan.*birthday",
        r"create.*birthday",
        r"buy.*birthday.*card",
        r"buy.*birthday.*gift",
        r"send.*birthday.*message",
        r"wish.*happy.*birthday",  # Generic wishes without specific dates
    ]

    for pattern in skip_patterns:
        if re.search(pattern, title_lower):
            # Only skip if it doesn't have a specific date
            if not re.search(
                r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d",
                title_lower,
            ):
                return None

    # Extract name patterns
    name_patterns = [
        r"(?:Call|Wish|Plan for|Prepare for|Buy.*for|Send.*to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\'?s?\s+birthday",
        r"birthday.*for\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
    ]

    name = None
    for pattern in name_patterns:
        match = re.search(pattern, title)
        if match:
            name = match.group(1).strip()
            # Clean up common prefixes
            if name.startswith("Send ") or name.startswith("Buy "):
                continue
            break

    # Extract date patterns
    date_patterns = [
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?",
        r"(\d{1,2})/(\d{1,2})",  # MM/DD format
        r"(\d{1,2})-(\d{1,2})",  # MM-DD format
    ]

    birthday_date = None

    # Try to extract date from title
    month_map = {
        "January": 1,
        "February": 2,
        "March": 3,
        "April": 4,
        "May": 5,
        "June": 6,
        "July": 7,
        "August": 8,
        "September": 9,
        "October": 10,
        "November": 11,
        "December": 12,
    }

    for pattern in date_patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            if "January" in pattern or "February" in pattern:
                month_name = match.group(1)
                day = int(match.group(2))
                if month_name in month_map:
                    # Use current year or next year if date has passed
                    today = date.today()
                    year = (
                        today.year
                        if (month_map[month_name], day) >= (today.month, today.day)
                        else today.year + 1
                    )
                    birthday_date = date(year, month_map[month_name], day)
            elif len(match.groups()) == 2:
                # MM/DD or MM-DD format
                month = int(match.group(1))
                day = int(match.group(2))
                today = date.today()
                year = (
                    today.year
                    if (month, day) >= (today.month, today.day)
                    else today.year + 1
                )
                birthday_date = date(year, month, day)
            break

    # If no date in title and this is a call/wish task, use due_date
    if not birthday_date and due_date:
        if "call" in title_lower or "wish" in title_lower:
            if isinstance(due_date, date):
                birthday_date = due_date
            elif isinstance(due_date, str):
                try:
                    birthday_date = pd.to_datetime(due_date).date()
                except Exception:
                    pass

    if name and birthday_date:
        return (name, birthday_date)
    elif birthday_date and not name:
        # Try to extract name from context
        # Look for common patterns like "Call [Name] for birthday"
        name_match = re.search(r"(?:Call|Wish)\s+([A-Z][a-z]+)", title)
        if name_match:
            return (name_match.group(1), birthday_date)

    return None


def normalize_name(name: str) -> str:
    """Normalize name for matching (lowercase, remove extra spaces)."""
    return " ".join(name.lower().split())


def match_name_to_contact(name: str, contacts_df: pd.DataFrame) -> pd.Series | None:
    """Find matching contact by name."""
    if contacts_df.empty:
        return None

    normalized_search = normalize_name(name)

    # Try exact match first
    for idx, contact in contacts_df.iterrows():
        contact_name = str(contact.get("name", "")).strip()
        if not contact_name:
            continue

        normalized_contact = normalize_name(contact_name)

        # Exact match
        if normalized_contact == normalized_search:
            return contact

        # Partial match (first name or last name)
        search_parts = normalized_search.split()
        contact_parts = normalized_contact.split()

        if len(search_parts) > 0 and len(contact_parts) > 0:
            # First name match
            if search_parts[0] == contact_parts[0]:
                return contact
            # Last name match (if both have last names)
            if len(search_parts) > 1 and len(contact_parts) > 1:
                if search_parts[-1] == contact_parts[-1]:
                    return contact

    return None


def match_name_to_person(name: str, people_df: pd.DataFrame) -> pd.Series | None:
    """Find matching person by name."""
    if people_df.empty or "people.parquet" not in str(people_df):
        return None

    normalized_search = normalize_name(name)

    # Try exact match first
    for idx, person in people_df.iterrows():
        person_name = str(person.get("name", "")).strip()
        if not person_name:
            continue

        normalized_person = normalize_name(person_name)

        # Exact match
        if normalized_person == normalized_search:
            return person

        # Partial match (first name or last name)
        search_parts = normalized_search.split()
        person_parts = normalized_person.split()

        if len(search_parts) > 0 and len(person_parts) > 0:
            # First name match
            if search_parts[0] == person_parts[0]:
                return person
            # Last name match (if both have last names)
            if len(search_parts) > 1 and len(person_parts) > 1:
                if search_parts[-1] == person_parts[-1]:
                    return person

    return None


def main():
    print("=== BIRTHDAY EXTRACTION AND UPDATE ===\n")

    # Load existing data
    print("Loading existing data...")
    contacts_df = (
        pd.read_parquet(contacts_path)
        if os.path.exists(contacts_path)
        else pd.DataFrame()
    )
    people_df = (
        pd.read_parquet(people_path) if os.path.exists(people_path) else pd.DataFrame()
    )
    tasks_df = (
        pd.read_parquet(tasks_path) if os.path.exists(tasks_path) else pd.DataFrame()
    )

    print(f"  Contacts: {len(contacts_df)} records")
    print(f"  People: {len(people_df)} records")
    print(f"  Tasks: {len(tasks_df)} records\n")

    # Add birthday column if it doesn't exist
    if "birthday" not in contacts_df.columns:
        contacts_df["birthday"] = None
    if "birthday" not in people_df.columns:
        people_df["birthday"] = None

    # Extract birthdays from tasks
    print("Extracting birthdays from tasks...")
    if tasks_df.empty or "title" not in tasks_df.columns:
        print("  No tasks data available or missing 'title' column")
        birthday_tasks = pd.DataFrame()
    else:
        birthday_tasks = tasks_df[
            tasks_df["title"].str.contains(
                "birthday|Birthday|BIRTHDAY", case=False, na=False
            )
        ]
    print(f"  Found {len(birthday_tasks)} birthday-related tasks")

    birthdays_from_tasks = []
    for idx, task in birthday_tasks.iterrows():
        title = str(task["title"])
        due_date = task.get("due_date")

        if pd.notna(due_date):
            try:
                due_date = (
                    pd.to_datetime(due_date).date()
                    if isinstance(due_date, str)
                    else due_date
                )
            except Exception:
                due_date = None

        result = extract_birthday_from_task_title(title, due_date)
        if result:
            name, birthday_date = result
            birthdays_from_tasks.append(
                {
                    "name": name,
                    "birthday": birthday_date,
                    "source": "task",
                    "task_title": title,
                }
            )

    print(f"  Extracted {len(birthdays_from_tasks)} birthdays from tasks\n")

    # Display extracted birthdays
    if birthdays_from_tasks:
        print("Birthdays extracted from tasks:")
        for bday in birthdays_from_tasks:
            print(
                f"  - {bday['name']}: {bday['birthday']} (from: {bday['task_title'][:50]}...)"
            )
        print()

    # Create snapshots before updating
    print("Creating snapshots...")
    create_snapshot(contacts_path)
    if os.path.exists(people_path):
        create_snapshot(people_path)
    print()

    # Update contacts with birthdays
    print("Updating contacts with birthdays...")
    contacts_updated = 0
    for bday_info in birthdays_from_tasks:
        name = bday_info["name"]
        birthday = bday_info["birthday"]

        # Try to match to contact
        contact = match_name_to_contact(name, contacts_df)
        if contact is not None:
            contact_id = contact["contact_id"]
            # Check if birthday is already set
            existing_birthday = (
                contacts_df.loc[
                    contacts_df["contact_id"] == contact_id, "birthday"
                ].iloc[0]
                if len(contacts_df.loc[contacts_df["contact_id"] == contact_id]) > 0
                else None
            )

            if pd.isna(existing_birthday) or existing_birthday is None:
                contacts_df.loc[
                    contacts_df["contact_id"] == contact_id, "birthday"
                ] = birthday
                contacts_df.loc[
                    contacts_df["contact_id"] == contact_id, "updated_date"
                ] = date.today()
                contacts_updated += 1
                print(f"  ✓ Updated contact: {contact['name']} -> {birthday}")
            else:
                print(
                    f"  - Skipped {contact['name']} (birthday already set: {existing_birthday})"
                )
        else:
            print(f"  ? No contact match found for: {name}")

    # Update people with birthdays
    print("\nUpdating people with birthdays...")
    people_updated = 0
    for bday_info in birthdays_from_tasks:
        name = bday_info["name"]
        birthday = bday_info["birthday"]

        # Try to match to person
        person = match_name_to_person(name, people_df)
        if person is not None:
            person_id = person["person_id"]
            # Check if birthday is already set
            existing_birthday = (
                people_df.loc[people_df["person_id"] == person_id, "birthday"].iloc[0]
                if len(people_df.loc[people_df["person_id"] == person_id]) > 0
                else None
            )

            if pd.isna(existing_birthday) or existing_birthday is None:
                people_df.loc[
                    people_df["person_id"] == person_id, "birthday"
                ] = birthday
                people_updated += 1
                print(f"  ✓ Updated person: {person['name']} -> {birthday}")
            else:
                print(
                    f"  - Skipped {person['name']} (birthday already set: {existing_birthday})"
                )
        else:
            # Already printed for contacts, skip duplicate message
            pass

    # Save updated data
    print("\nSaving updated data...")
    contacts_df.to_parquet(contacts_path, index=False)
    print(
        f"  ✓ Saved {len(contacts_df)} contacts ({contacts_updated} updated with birthdays)"
    )

    if os.path.exists(people_path):
        people_df.to_parquet(people_path, index=False)
        print(
            f"  ✓ Saved {len(people_df)} people ({people_updated} updated with birthdays)"
        )

    print("\n=== COMPLETE ===")
    print(f"Total birthdays extracted: {len(birthdays_from_tasks)}")
    print(f"Contacts updated: {contacts_updated}")
    print(f"People updated: {people_updated}")
    print(
        "\nNote: Google Calendar birthdays should be extracted separately using MCP tools."
    )
    print("Run this script after querying Google Calendar for birthday events.")


if __name__ == "__main__":
    main()
