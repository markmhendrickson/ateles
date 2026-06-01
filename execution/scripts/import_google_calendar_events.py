#!/usr/bin/env python3
"""
Import Google Calendar events into events data type and link to contacts.

This script processes Google Calendar events and:
1. Transforms them to match the events schema
2. Extracts contact information from attendees and organizers
3. Creates/updates contacts in contacts.parquet
4. Imports events with contact references in notes field
"""

import argparse
import json
import logging
import sys
import uuid
from datetime import date, datetime

# Add parent directory to path to import from scripts
sys.path.insert(0, str(__file__).rsplit("/", 1)[0])

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# Parse arguments
parser = argparse.ArgumentParser(description="Import Google Calendar events")
parser.add_argument(
    "--file", "-f", type=str, help="JSON file with events (default: read from stdin)"
)
parser.add_argument(
    "--limit", "-l", type=int, help="Limit number of events to process (default: all)"
)
args = parser.parse_args()

# Read the events from file or stdin
logger.info("Reading events from input...")
if args.file:
    logger.info(f"Reading from file: {args.file}")
    with open(args.file) as f:
        events_data = json.load(f)
else:
    logger.info("Reading from stdin...")
    events_data = json.load(sys.stdin)

# Handle both list and dict formats
if isinstance(events_data, list):
    events = events_data
elif isinstance(events_data, dict):
    events = events_data.get("events", [])
else:
    logger.error(f"Unexpected input format: {type(events_data)}")
    sys.exit(1)

# Limit events if specified
if args.limit:
    logger.info(f"Limiting to {args.limit} events (received {len(events)} events)")
    events = events[: args.limit]
else:
    logger.info(f"Processing {len(events)} events")

# Read existing events and contacts
import os

import pandas as pd

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
events_file = os.path.join(base_dir, "data/events/events.parquet")
contacts_file = os.path.join(base_dir, "data/contacts/contacts.parquet")
snapshots_dir = os.path.join(base_dir, "data/snapshots")

# Ensure snapshots directory exists
os.makedirs(snapshots_dir, exist_ok=True)

# Read existing data
logger.info(f"Reading existing events from {events_file}")
existing_events = (
    pd.read_parquet(events_file) if os.path.exists(events_file) else pd.DataFrame()
)
logger.info(f"Found {len(existing_events)} existing events")

logger.info(f"Reading existing contacts from {contacts_file}")
existing_contacts = (
    pd.read_parquet(contacts_file) if os.path.exists(contacts_file) else pd.DataFrame()
)
logger.info(f"Found {len(existing_contacts)} existing contacts")

# Create snapshots
timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
if not existing_events.empty:
    snapshot_path = os.path.join(snapshots_dir, f"events-{timestamp}.parquet")
    logger.info(f"Creating snapshot: {snapshot_path}")
    existing_events.to_parquet(snapshot_path, index=False)
if not existing_contacts.empty:
    snapshot_path = os.path.join(snapshots_dir, f"contacts-{timestamp}.parquet")
    logger.info(f"Creating snapshot: {snapshot_path}")
    existing_contacts.to_parquet(snapshot_path, index=False)


def normalize_email(email: str) -> str:
    """Normalize email for comparison."""
    return email.lower().strip() if email else ""


def find_contact_by_email(email: str, contacts_df: pd.DataFrame) -> pd.Series | None:
    """Find existing contact by email."""
    if email and not contacts_df.empty:
        normalized_email = normalize_email(email)
        matches = contacts_df[
            contacts_df["email"].str.lower().str.strip() == normalized_email
        ]
        if not matches.empty:
            return matches.iloc[0]
    return None


def create_or_update_contact(
    email: str, name: str | None, contacts_df: pd.DataFrame, event_summary: str = ""
) -> tuple[str, pd.DataFrame]:
    """Create or update contact and return (contact_id, updated_contacts_df)."""
    if not email or email == "markmhendrickson@gmail.com":
        logger.debug(f"Skipping self email: {email}")
        return ("", contacts_df)  # Skip self

    existing = find_contact_by_email(email, contacts_df)

    if existing is not None:
        contact_id = existing["contact_id"]
        logger.info(f"Updating existing contact: {email} (ID: {contact_id})")
        # Update last_contact_date
        contacts_df = contacts_df.copy()
        contacts_df.loc[
            contacts_df["contact_id"] == contact_id, "last_contact_date"
        ] = date.today()
        contacts_df.loc[
            contacts_df["contact_id"] == contact_id, "updated_date"
        ] = date.today()
        return (contact_id, contacts_df)
    else:
        # Create new contact
        contact_id = str(uuid.uuid4())[:16]
        contact_name = name or email.split("@")[0]
        logger.info(
            f"Creating new contact: {contact_name} ({email}) (ID: {contact_id})"
        )
        new_contact = {
            "contact_id": contact_id,
            "name": contact_name,
            "contact_type": "personal",
            "category": "calendar",
            "platform": "Google Calendar",
            "email": email,
            "phone": None,
            "address": None,
            "country": None,
            "website": None,
            "language": None,
            "notes": f"Imported from Google Calendar event: {event_summary}",
            "first_contact_date": date.today(),
            "last_contact_date": date.today(),
            "created_date": date.today(),
            "updated_date": date.today(),
        }
        contacts_df = pd.concat(
            [contacts_df, pd.DataFrame([new_contact])], ignore_index=True
        )
        return (contact_id, contacts_df)


def parse_date(date_obj: dict) -> date | None:
    """Parse Google Calendar date object to Python date."""
    if "dateTime" in date_obj:
        dt_str = date_obj["dateTime"]
        # Parse ISO format datetime
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.date()
    elif "date" in date_obj:
        return datetime.fromisoformat(date_obj["date"]).date()
    return None


def parse_datetime_string(date_obj: dict) -> str | None:
    """Parse Google Calendar date object to ISO datetime string."""
    if not date_obj:
        return None
    if "dateTime" in date_obj:
        return date_obj["dateTime"]
    elif "date" in date_obj:
        # Convert date-only to datetime string (start of day)
        return f"{date_obj['date']}T00:00:00"
    return None


def process_event(
    gcal_event: dict, contacts_df: pd.DataFrame
) -> tuple[dict | None, pd.DataFrame]:
    """Process a Google Calendar event into events schema format.
    Returns (event_record, updated_contacts_df)."""
    event_id = gcal_event.get("id", "")
    if not event_id:
        logger.warning("Event missing ID, skipping")
        return (None, contacts_df)

    # Generate short event_id (first 16 chars of Google Calendar ID)
    short_event_id = event_id[:16] if len(event_id) >= 16 else event_id
    summary = gcal_event.get("summary", "No title")

    # Check if event already exists
    if not existing_events.empty:
        if "event_id" in existing_events.columns:
            if short_event_id in existing_events["event_id"].values:
                logger.debug(
                    f"Skipping existing event: {summary} (ID: {short_event_id})"
                )
                return (None, contacts_df)  # Skip existing events

    logger.info(f"Processing event: {summary} (ID: {short_event_id})")

    start_obj = gcal_event.get("start", {})
    end_obj = gcal_event.get("end", {})
    location = gcal_event.get("location", "")
    description = gcal_event.get("description", "")
    attendees = gcal_event.get("attendees", [])
    organizer = gcal_event.get("organizer", {})

    start_date = parse_date(start_obj)
    end_date = parse_date(end_obj)

    if not start_date:
        logger.warning(f"Event {summary} missing start date, skipping")
        return (None, contacts_df)

    logger.debug(f"  Start: {start_date}, End: {end_date}, Location: {location}")
    logger.debug(
        f"  Attendees: {len(attendees)}, Organizer: {organizer.get('email', 'None')}"
    )

    # Process contacts
    contact_emails = []
    contact_names = []

    # Add organizer
    if organizer and organizer.get("email"):
        org_email = organizer["email"]
        org_name = organizer.get("displayName", "")
        if org_email != "markmhendrickson@gmail.com":
            contact_id, contacts_df = create_or_update_contact(
                org_email, org_name, contacts_df, summary
            )
            if contact_id:
                contact_emails.append(org_email)
                contact_names.append(org_name or org_email.split("@")[0])

    # Add attendees
    for attendee in attendees:
        attendee_email = attendee.get("email", "")
        attendee_name = attendee.get("displayName", "")
        if attendee_email and attendee_email != "markmhendrickson@gmail.com":
            contact_id, contacts_df = create_or_update_contact(
                attendee_email, attendee_name, contacts_df, summary
            )
            if contact_id:
                contact_emails.append(attendee_email)
                contact_names.append(attendee_name or attendee_email.split("@")[0])

    # Build notes with contact info
    notes_parts = []
    if description:
        notes_parts.append(description)
    if contact_names:
        notes_parts.append(f"Attendees: {', '.join(contact_names)}")
    if contact_emails:
        notes_parts.append(f"Contact emails: {', '.join(contact_emails)}")
    if gcal_event.get("htmlLink"):
        notes_parts.append(f"Google Calendar link: {gcal_event['htmlLink']}")

    notes = "\n\n".join(notes_parts) if notes_parts else ""

    # Determine calendar category from calendarId
    calendar_id = gcal_event.get("calendarId", "")
    category = ""
    if "Work" in calendar_id or "work" in calendar_id.lower():
        category = "Work"
    elif "Travel" in calendar_id or "travel" in calendar_id.lower():
        category = "Travel"
    elif "Tontitos" in calendar_id or "tontitos" in calendar_id.lower():
        category = "Family"

    # Extract all Google Calendar metadata
    creator = gcal_event.get("creator", {})
    organizer = gcal_event.get("organizer", {})

    # Build event record with all Google Calendar fields
    event_record = {
        "event_id": short_event_id,
        "name": summary,
        "start_date": start_date,
        "end_date": end_date if end_date else start_date,
        "locations": location,
        "categories": category,
        "type": "",
        "dates_status": "",
        "destination_status": "",
        "jet_lag": "",
        "notes": notes,
        "import_date": date.today(),
        "import_source_file": f"google_calendar:{calendar_id}",
        # Google Calendar metadata fields
        "gcal_event_id": event_id,
        "gcal_status": gcal_event.get("status", ""),
        "gcal_created": gcal_event.get("created", ""),
        "gcal_updated": gcal_event.get("updated", ""),
        "gcal_creator_email": creator.get("email", ""),
        "gcal_organizer_email": organizer.get("email", ""),
        "gcal_organizer_display_name": organizer.get("displayName", ""),
        "gcal_iCalUID": gcal_event.get("iCalUID", ""),
        "gcal_sequence": gcal_event.get("sequence", 0),
        "gcal_event_type": gcal_event.get("eventType", ""),
        "gcal_account_id": gcal_event.get("accountId", ""),
        "gcal_html_link": gcal_event.get("htmlLink", ""),
        "gcal_start_datetime": parse_datetime_string(start_obj),
        "gcal_end_datetime": parse_datetime_string(end_obj),
        "gcal_start_timezone": start_obj.get("timeZone", ""),
        "gcal_end_timezone": end_obj.get("timeZone", ""),
        "gcal_description": description,
    }

    return (event_record, contacts_df)


# Process all events
logger.info("Starting to process events...")
new_events = []
updated_contacts = (
    existing_contacts.copy() if not existing_contacts.empty else pd.DataFrame()
)
skipped_count = 0

for idx, gcal_event in enumerate(events, 1):
    logger.info(f"Processing event {idx}/{len(events)}")
    processed, updated_contacts = process_event(gcal_event, updated_contacts)
    if processed:
        new_events.append(processed)
        logger.info(f"  ✓ Added event: {processed.get('name', 'Unknown')}")
    else:
        skipped_count += 1
        logger.debug("  ✗ Skipped event")

logger.info(
    f"Processed {len(events)} events: {len(new_events)} new, {skipped_count} skipped"
)

# Add new events
if new_events:
    logger.info(f"Writing {len(new_events)} new events to {events_file}")
    new_events_df = pd.DataFrame(new_events)
    if existing_events.empty:
        updated_events = new_events_df
    else:
        # Ensure all columns exist in both DataFrames before concatenating
        # This handles the case where existing events don't have the new gcal_* fields
        all_columns = set(existing_events.columns) | set(new_events_df.columns)
        for col in all_columns:
            if col not in existing_events.columns:
                existing_events[col] = None
            if col not in new_events_df.columns:
                new_events_df[col] = None
        updated_events = pd.concat([existing_events, new_events_df], ignore_index=True)
    updated_events.to_parquet(events_file, index=False)
    logger.info(f"✓ Successfully added {len(new_events)} new events")
    print(f"Added {len(new_events)} new events", file=sys.stderr)
else:
    logger.info("No new events to add")
    print("No new events to add", file=sys.stderr)

# Save updated contacts
if not updated_contacts.empty:
    contacts_added = len(updated_contacts) - (
        len(existing_contacts) if not existing_contacts.empty else 0
    )
    logger.info(
        f"Writing contacts to {contacts_file} (added {contacts_added} new contacts)"
    )
    updated_contacts.to_parquet(contacts_file, index=False)
    logger.info("✓ Successfully updated contacts")
    print(f"Updated contacts (added {contacts_added} new contacts)", file=sys.stderr)
else:
    logger.info("No contacts to update")
    print("No contacts to update", file=sys.stderr)

print(
    json.dumps(
        {
            "events_added": len(new_events),
            "contacts_total": (
                len(updated_contacts) if not updated_contacts.empty else 0
            ),
            "contacts_added": len(updated_contacts)
            - (len(existing_contacts) if not existing_contacts.empty else 0),
        }
    )
)
