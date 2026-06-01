#!/usr/bin/env python3
"""
Update contacts and people records with birthdays from Google Calendar events.

This script processes Google Calendar birthday events and updates contacts/people records.
"""

import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# Read existing data
# Get data directory from environment variable
data_dir = os.getenv("DATA_DIR")
if not data_dir:
    raise RuntimeError(
        "DATA_DIR environment variable is not set. "
        "Please set DATA_DIR to your data directory path, e.g.: "
        'export DATA_DIR="/absolute/path/to/data"'
    )
contacts_path = os.path.join(data_dir, "contacts/contacts.parquet")
people_path = os.path.join(data_dir, "people/people.parquet")
snapshots_dir = os.path.join(data_dir, "snapshots")

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


def extract_name_from_summary(summary: str) -> str:
    """Extract person's name from birthday event summary."""
    # Remove common suffixes
    summary = summary.replace("'s birthday", "")
    summary = summary.replace("'s Birthday", "")
    summary = summary.replace(" birthday", "")
    summary = summary.replace(" Birthday", "")
    summary = summary.replace(" cumpleaños", "")
    summary = summary.replace(" Aniversari de", "")
    summary = summary.replace("Aniversari de ", "")

    # Remove year in parentheses
    summary = re.sub(r"\s*\(\d{4}\)", "", summary)

    # Remove extra punctuation and whitespace
    summary = summary.strip().rstrip("!").strip()

    return summary


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
    if people_df.empty:
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
    print("=== UPDATE BIRTHDAYS FROM GOOGLE CALENDAR ===\n")

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

    print(f"  Contacts: {len(contacts_df)} records")
    print(f"  People: {len(people_df)} records\n")

    # Add birthday column if it doesn't exist
    if "birthday" not in contacts_df.columns:
        contacts_df["birthday"] = None
    if "birthday" not in people_df.columns:
        people_df["birthday"] = None

    # Read Google Calendar events from stdin (JSON)
    print("Reading Google Calendar events from stdin...")
    events_data = json.load(sys.stdin)

    if isinstance(events_data, dict) and "events" in events_data:
        events = events_data["events"]
    elif isinstance(events_data, list):
        events = events_data
    else:
        print(f"Error: Unexpected input format: {type(events_data)}")
        sys.exit(1)

    print(f"  Found {len(events)} birthday events\n")

    # Extract unique birthdays (by recurringEventId to avoid duplicates)
    unique_birthdays = {}
    for event in events:
        summary = event.get("summary", "")
        start = event.get("start", {})
        start_date = start.get("date") if "date" in start else None

        if not start_date:
            continue

        # Extract name from summary
        name = extract_name_from_summary(summary)
        if not name:
            continue

        # Parse date
        try:
            birthday_date = pd.to_datetime(start_date).date()
        except Exception:
            continue

        # Use recurringEventId as key to deduplicate
        recurring_id = event.get("recurringEventId")
        if recurring_id:
            if recurring_id not in unique_birthdays:
                unique_birthdays[recurring_id] = {
                    "name": name,
                    "birthday": birthday_date,
                    "summary": summary,
                }
        else:
            # No recurring ID, use name+date as key
            key = f"{name}_{birthday_date}"
            if key not in unique_birthdays:
                unique_birthdays[key] = {
                    "name": name,
                    "birthday": birthday_date,
                    "summary": summary,
                }

    print(f"Extracted {len(unique_birthdays)} unique birthdays:\n")
    for bday_info in unique_birthdays.values():
        print(f"  - {bday_info['name']}: {bday_info['birthday']}")
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
    contacts_not_found = []

    for bday_info in unique_birthdays.values():
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
            contacts_not_found.append(name)
            print(f"  ? No contact match found for: {name}")

    # Update people with birthdays
    print("\nUpdating people with birthdays...")
    people_updated = 0

    for bday_info in unique_birthdays.values():
        name = bday_info["name"]
        birthday = bday_info["birthday"]

        # Skip if already found in contacts
        if name in contacts_not_found:
            # Try to match to person
            person = match_name_to_person(name, people_df)
            if person is not None:
                person_id = person["person_id"]
                # Check if birthday is already set
                existing_birthday = (
                    people_df.loc[people_df["person_id"] == person_id, "birthday"].iloc[
                        0
                    ]
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
    print(f"Total unique birthdays: {len(unique_birthdays)}")
    print(f"Contacts updated: {contacts_updated}")
    print(f"People updated: {people_updated}")


if __name__ == "__main__":
    main()
