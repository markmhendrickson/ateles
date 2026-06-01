#!/usr/bin/env python3
"""
Process all 140 recipients from the latest Minted holiday card delivery.
Adds new contacts and updates existing ones with the 2025 holiday card mailing tag.
"""

import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent.parent

# Read current contacts
contacts_path = PROJECT_ROOT / "data/contacts/contacts.parquet"
df_contacts = pd.read_parquet(contacts_path)
print(f"Starting with {len(df_contacts)} contacts")

# Create snapshot
os.makedirs(PROJECT_ROOT / "data/snapshots", exist_ok=True)
timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
snapshot_path = PROJECT_ROOT / f"data/snapshots/contacts-{timestamp}.parquet"
df_contacts.to_parquet(snapshot_path, index=False)
print(f"Created snapshot: {snapshot_path}")


# Helper functions
def normalize_name(name):
    if pd.isna(name) or not name:
        return ""
    return str(name).lower().strip()


def normalize_address(addr1, addr2=""):
    parts = [str(addr1) if addr1 else "", str(addr2) if addr2 else ""]
    return " ".join(parts).strip().lower()


def find_existing_contact(recipient, df_contacts):
    """Find existing contact by name, address, or external_id"""
    rec_name = normalize_name(recipient.get("name", ""))
    rec_addr1 = recipient.get("address1", "")
    rec_addr2 = recipient.get("address2", "")
    rec_addr = normalize_address(rec_addr1, rec_addr2)
    rec_external_id = recipient.get("external_id")

    # Try to find by external_id first (most reliable)
    if rec_external_id and "external_id" in df_contacts.columns:
        matches = df_contacts[df_contacts["external_id"] == str(rec_external_id)]
        if len(matches) > 0:
            return matches.index[0]

    # Try to find by name and address
    for idx, row in df_contacts.iterrows():
        row_name = normalize_name(row.get("name", ""))
        row_addr = normalize_address(row.get("address", ""))

        # Check name match
        if rec_name and row_name and rec_name == row_name:
            # If address also matches, it's definitely the same contact
            if rec_addr and row_addr and rec_addr == row_addr:
                return idx
            # If name matches but no address in recipient, still likely match
            if not rec_addr:
                return idx

        # Check address match (if name doesn't match but address does)
        if rec_addr and row_addr and rec_addr == row_addr and rec_name:
            return idx

    return None


# Process recipients from delivery data
mailing_note = "Received 2025 holiday card mailing (Leaping Stag The Met Holiday Cards)"
today = date.today()

# Get delivery data from MCP - we'll call it
sys.path.insert(0, str(PROJECT_ROOT))
try:
    sys.path.insert(0, str(PROJECT_ROOT / "mcp-servers" / "minted"))
    from minted_mcp_server import get_minted_latest_delivery

    # Get delivery data
    print("Getting delivery data from MCP...")
    delivery_result = get_minted_latest_delivery()

    if not delivery_result or "recipients" not in delivery_result:
        print("Error: Could not get delivery data from MCP")
        sys.exit(1)

    recipients = delivery_result["recipients"]
    print(f"Found {len(recipients)} recipients")

    # Process each recipient
    updated_count = 0
    added_count = 0
    new_rows = []

    for recipient in recipients:
        # Build address string
        addr_parts = [recipient.get("address1", ""), recipient.get("address2", "")]
        addr_parts = [p for p in addr_parts if p]
        address = ", ".join(addr_parts) if addr_parts else ""

        if address and recipient.get("locality"):
            address += f", {recipient.get('locality')}"
        if address and recipient.get("administrative_area"):
            address += f", {recipient.get('administrative_area')}"
        if address and recipient.get("postal_code"):
            address += f" {recipient.get('postal_code')}"

        # Find existing contact
        existing_idx = find_existing_contact(recipient, df_contacts)

        if existing_idx is not None:
            # Update existing contact
            existing_notes = (
                df_contacts.at[existing_idx, "notes"]
                if pd.notna(df_contacts.at[existing_idx, "notes"])
                else ""
            )
            if mailing_note not in existing_notes:
                if existing_notes:
                    df_contacts.at[
                        existing_idx, "notes"
                    ] = f"{existing_notes}; {mailing_note}"
                else:
                    df_contacts.at[existing_idx, "notes"] = mailing_note
            df_contacts.at[existing_idx, "last_contact_date"] = today
            df_contacts.at[existing_idx, "updated_date"] = today
            updated_count += 1
        else:
            # Create new contact
            contact_id = recipient.get("id", str(uuid.uuid4())[:16])
            new_contact = {
                "contact_id": contact_id,
                "name": recipient.get("name", ""),
                "contact_type": "personal",
                "category": "holiday_card_recipient",
                "platform": "Minted",
                "email": None,
                "phone": None,
                "address": address,
                "country": recipient.get("country", ""),
                "website": None,
                "language": (
                    "English" if recipient.get("country") == "US" else "Spanish"
                ),
                "notes": mailing_note,
                "first_contact_date": today,
                "last_contact_date": today,
                "created_date": today,
                "updated_date": today,
                "external_id": (
                    str(recipient.get("external_id", ""))
                    if recipient.get("external_id")
                    else None
                ),
            }
            new_rows.append(new_contact)
            added_count += 1

    # Add new contacts
    if new_rows:
        df_new = pd.DataFrame(new_rows)
        df_contacts = pd.concat([df_contacts, df_new], ignore_index=True)

    # Save updated contacts
    df_contacts.to_parquet(contacts_path, index=False)
    print("\nProcessing complete!")
    print(f"Updated: {updated_count} existing contacts")
    print(f"Added: {added_count} new contacts")
    print(f"Total contacts now: {len(df_contacts)}")

except ImportError as e:
    print(f"Error importing MCP server: {e}")
    print("Will process recipients when delivery data is provided")
except Exception as e:
    print(f"Error processing recipients: {e}")
    import traceback

    traceback.print_exc()
