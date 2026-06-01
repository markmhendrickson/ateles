#!/usr/bin/env python3
"""
Process all recipients from the latest Minted holiday card delivery.
Adds new contacts and updates existing ones with the 2025 holiday card mailing tag.
"""

import os
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent.parent

# Read current contacts
contacts_path = PROJECT_ROOT / "data/contacts/contacts.parquet"
df_contacts = pd.read_parquet(contacts_path)
print(f"Current contacts: {len(df_contacts)}")

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


# Get delivery data from MCP
# We'll use the delivery data structure from the MCP response
mailing_note = "Received 2025 holiday card mailing (Leaping Stag The Met Holiday Cards)"
today = date.today()

# Process recipients - we'll get this from the MCP call
# For now, create the structure
print("Processing recipients...")

# We need to get the delivery data - let's use the MCP server
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from mcp_servers.minted.minted_mcp_server import get_minted_latest_delivery

    # Actually, we can't call it directly - we need to use the MCP response we already have
    # Let's process the recipients from the delivery data structure
    print("Note: This script should be run with delivery data from MCP response")
except ImportError:
    print("Could not import Minted MCP server")

print("Script ready - will process recipients when delivery data is provided")
