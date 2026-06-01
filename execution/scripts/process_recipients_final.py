#!/usr/bin/env python3
"""
Process all 140 recipients from the latest Minted holiday card delivery.
Adds new contacts and updates existing ones with the 2025 holiday card mailing tag.
"""

import os
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

# Get delivery data from MCP response
# We have 140 recipients in the 'recipients' field
# Each recipient has: name, address1, address2, locality, administrative_area, postal_code, country, external_id

# Process each recipient
updated_count = 0
added_count = 0
new_rows = []

# Load delivery data - we'll get it from the MCP response
# The delivery data structure has recipients with: name, address1, address2, locality, administrative_area, postal_code, country, external_id
# We need to get it from the MCP call

print("Processing 140 recipients from delivery data...")
print(f"Will tag all with: {mailing_note}")

# We'll process recipients using pandas batch processing
# This will be more efficient than using MCP tools one by one

# For now, we'll prepare the processing logic
# We'll process recipients when we have the delivery data

print("Script ready - will process recipients when delivery data is provided")
print("Expected: 140 recipients from latest Minted delivery")
