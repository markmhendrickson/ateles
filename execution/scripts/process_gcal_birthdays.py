#!/usr/bin/env python3
"""
Process Google Calendar birthdays: match to existing contacts/people and create new records.

This script:
1. Extracts unique birthdays from Google Calendar events
2. Matches to existing contacts/people by name
3. Updates existing records with birthday information
4. Creates new contact records for unmatched birthdays
5. Displays all birthdays
"""

import json
import re
import sys
from datetime import date, datetime


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


def main():
    # Read Google Calendar events from stdin
    events_data = json.load(sys.stdin)

    if isinstance(events_data, dict) and "events" in events_data:
        events = events_data["events"]
    elif isinstance(events_data, list):
        events = events_data
    else:
        print(f"Error: Unexpected input format: {type(events_data)}")
        sys.exit(1)

    print(f"=== PROCESSING {len(events)} GOOGLE CALENDAR BIRTHDAY EVENTS ===\n")

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
            birthday_date = datetime.strptime(start_date, "%Y-%m-%d").date()
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

    print(f"Extracted {len(unique_birthdays)} unique birthdays\n")

    # Process each birthday
    matched_contacts = []
    matched_people = []
    unmatched = []

    for bday_info in unique_birthdays.values():
        name = bday_info["name"]
        birthday = bday_info["birthday"]

        # Try to find in contacts
        contacts = mcp_parquet_read_parquet(  # noqa: F821
            data_type="contacts",
            filters={"name": {"$contains": name.split()[0] if name.split() else name}},
            limit=10,
        )

        matched = False
        if contacts and contacts.get("data"):
            for contact in contacts["data"]:
                contact_name = contact.get("name", "")
                if normalize_name(contact_name) == normalize_name(name) or (
                    name.split()
                    and contact_name.split()
                    and normalize_name(name.split()[0])
                    == normalize_name(contact_name.split()[0])
                ):
                    # Update contact
                    mcp_parquet_update_records(  # noqa: F821
                        data_type="contacts",
                        filters={"contact_id": contact["contact_id"]},
                        updates={
                            "birthday": str(birthday),
                            "updated_date": str(date.today()),
                        },
                    )
                    matched_contacts.append(
                        {"name": contact_name, "birthday": birthday, "type": "contact"}
                    )
                    matched = True
                    break

        # Try to find in people if not matched
        if not matched:
            people = mcp_parquet_read_parquet(  # noqa: F821
                data_type="people",
                filters={
                    "name": {"$contains": name.split()[0] if name.split() else name}
                },
                limit=10,
            )

            if people and people.get("data"):
                for person in people["data"]:
                    person_name = person.get("name", "")
                    if person_name and (
                        normalize_name(person_name) == normalize_name(name)
                        or (
                            name.split()
                            and person_name.split()
                            and normalize_name(name.split()[0])
                            == normalize_name(person_name.split()[0])
                        )
                    ):
                        # Update person
                        mcp_parquet_update_records(  # noqa: F821
                            data_type="people",
                            filters={"person_id": person["person_id"]},
                            updates={"birthday": str(birthday)},
                        )
                        matched_people.append(
                            {
                                "name": person_name,
                                "birthday": birthday,
                                "type": "person",
                            }
                        )
                        matched = True
                        break

        # If not matched, create new contact
        if not matched:
            unmatched.append({"name": name, "birthday": birthday})

    print(f"Matched {len(matched_contacts)} contacts")
    print(f"Matched {len(matched_people)} people")
    print(f"Unmatched: {len(unmatched)} (will create new contacts)\n")

    # Create new contacts for unmatched birthdays
    created = 0
    for bday_info in unmatched:
        name = bday_info["name"]
        birthday = bday_info["birthday"]

        # Create new contact
        contact_id = str(uuid.uuid4())[:16]
        mcp_parquet_add_record(  # noqa: F821
            data_type="contacts",
            record={
                "contact_id": contact_id,
                "name": name,
                "contact_type": "personal",
                "category": "birthday",
                "platform": "Google Calendar",
                "birthday": str(birthday),
                "created_date": str(date.today()),
                "updated_date": str(date.today()),
                "notes": "Birthday imported from Google Calendar",
            },
        )
        created += 1

    print(f"Created {created} new contact records\n")

    # Display all birthdays
    print("=== ALL BIRTHDAYS ===\n")

    # Get all contacts with birthdays
    all_contacts = mcp_parquet_read_parquet(  # noqa: F821
        data_type="contacts",
        filters={"birthday": {"$ne": None}},
        sort_by=[{"column": "birthday", "ascending": True}],
    )

    # Get all people with birthdays
    all_people = mcp_parquet_read_parquet(  # noqa: F821
        data_type="people",
        filters={"birthday": {"$ne": None}},
        sort_by=[{"column": "birthday", "ascending": True}],
    )

    all_birthdays = []

    if all_contacts and all_contacts.get("data"):
        for contact in all_contacts["data"]:
            if contact.get("birthday"):
                all_birthdays.append(
                    {
                        "name": contact.get("name", "Unknown"),
                        "birthday": contact["birthday"],
                        "type": "Contact",
                    }
                )

    if all_people and all_people.get("data"):
        for person in all_people["data"]:
            if person.get("birthday"):
                all_birthdays.append(
                    {
                        "name": person.get("name", "Unknown"),
                        "birthday": person["birthday"],
                        "type": "Person",
                    }
                )

    # Sort by birthday (month and day, ignoring year)
    def birthday_key(b):
        bday = (
            datetime.strptime(b["birthday"], "%Y-%m-%d").date()
            if isinstance(b["birthday"], str)
            else b["birthday"]
        )
        return (bday.month, bday.day)

    all_birthdays.sort(key=birthday_key)

    # Group by month
    from collections import defaultdict

    by_month = defaultdict(list)
    month_names = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]

    for bday in all_birthdays:
        bday_date = (
            datetime.strptime(bday["birthday"], "%Y-%m-%d").date()
            if isinstance(bday["birthday"], str)
            else bday["birthday"]
        )
        by_month[bday_date.month].append(bday)

    for month_num in range(1, 13):
        if month_num in by_month:
            print(f"{month_names[month_num - 1]}:")
            for bday in by_month[month_num]:
                bday_date = (
                    datetime.strptime(bday["birthday"], "%Y-%m-%d").date()
                    if isinstance(bday["birthday"], str)
                    else bday["birthday"]
                )
                print(
                    f"  {bday_date.strftime('%B %d')}: {bday['name']} ({bday['type']})"
                )
            print()

    print(f"\nTotal: {len(all_birthdays)} birthdays")


if __name__ == "__main__":
    import uuid

    main()
