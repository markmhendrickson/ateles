#!/usr/bin/env python3
"""
Cotinga Deep Prep — Research meeting attendees and create briefing
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Load env
_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com")
MADRID_TZ = ZoneInfo("Europe/Madrid")

def _neotoma_get(path: str) -> dict | list | None:
    """Make a GET request to the Neotoma API."""
    if not NEOTOMA_BEARER_TOKEN or not NEOTOMA_BASE_URL:
        return None
    try:
        url = f"{NEOTOMA_BASE_URL.rstrip('/')}{path}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"Neotoma GET {path} failed: {exc}", file=sys.stderr)
        return None

def _neotoma_post(path: str, payload: dict) -> dict | None:
    """Make a POST request to the Neotoma API."""
    if not NEOTOMA_BEARER_TOKEN or not NEOTOMA_BASE_URL:
        return None
    try:
        url = f"{NEOTOMA_BASE_URL.rstrip('/')}{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"Neotoma POST {path} failed: {exc}", file=sys.stderr)
        return None

def lookup_person_in_neotoma(email: str, name: str) -> dict | None:
    """
    Try to find a person entity in Neotoma by email (preferred) or name.
    Returns the snapshot dict if found, None otherwise.
    """
    # Search by email first
    data = _neotoma_get(
        f"/api/entities?entity_type=person&search={urllib.parse.quote(email)}&limit=5"
    )
    if data:
        entities = data.get("entities") or []
        for e in entities:
            snap = e.get("snapshot") or {}
            if email.lower() in (snap.get("email") or "").lower():
                return snap

    # Fall back to name search
    data = _neotoma_get(
        f"/api/entities?entity_type=person&search={urllib.parse.quote(name)}&limit=5"
    )
    if data:
        entities = data.get("entities") or []
        for e in entities:
            snap = e.get("snapshot") or {}
            return snap  # return first name match

    return None

def create_person_entity(name: str, email: str, notes: str) -> str | None:
    """Create a person entity in Neotoma."""
    payload = {
        "entities": [{
            "entity_type": "person",
            "name": name,
            "email": email,
            "notes": notes,
        }]
    }
    result = _neotoma_post("/api/entities", payload)
    if result and "entities" in result and len(result["entities"]) > 0:
        return result["entities"][0].get("entity_id")
    return None

def create_checkpoint_brief(body: str, meeting_event_id: str | None = None) -> str | None:
    """Create a checkpoint_brief entity in Neotoma."""
    payload = {
        "entities": [{
            "entity_type": "checkpoint_brief",
            "body": body,
            "schema_id": "b0bfcfab-1f07-4526-8fa5-d5ace343b004",
        }],
        "relationships": []
    }

    if meeting_event_id:
        # Add relationship REFERS_TO the meeting event
        payload["relationships"].append({
            "from_entity_id": None,  # Will be filled by server with new entity ID
            "to_entity_id": meeting_event_id,
            "relationship_type": "REFERS_TO",
        })

    result = _neotoma_post("/api/entities", payload)
    if result and "entities" in result and len(result["entities"]) > 0:
        return result["entities"][0].get("entity_id")
    return None

def main():
    # Meeting details
    meeting_title = "Neotoma"
    meeting_datetime = "2026-05-27 at 16:30"
    attendees = [{"name": "ivan", "email": "ivan@jme.vc"}]

    print("=== Cotinga Deep Prep: Neotoma ===\n")

    # Step 1: Research participants
    participant_info = []
    for attendee in attendees:
        name = attendee["name"]
        email = attendee["email"]
        print(f"Researching {name} ({email})...")

        person_snap = lookup_person_in_neotoma(email, name)
        if person_snap:
            print(f"  Found in Neotoma: {person_snap.get('name')}")
            participant_info.append({
                "name": person_snap.get("name", name),
                "email": email,
                "role": person_snap.get("role", "Unknown"),
                "company": person_snap.get("company", "Unknown"),
                "found": True,
                "notes": person_snap.get("notes", ""),
            })
        else:
            print(f"  Not found. Creating stub entity...")
            entity_id = create_person_entity(
                name=name,
                email=email,
                notes=f"Attendee at '{meeting_title}' on 2026-05-27"
            )
            if entity_id:
                print(f"  Created entity: {entity_id}")
            participant_info.append({
                "name": name,
                "email": email,
                "role": "Unknown",
                "company": "Unknown",
                "found": False,
                "notes": "First meeting",
            })

    # Step 2: Compose brief
    brief = f"""📅 Cotinga deep prep: {meeting_title} ({meeting_datetime})

👥 Participants
"""

    for p in participant_info:
        status = "met before" if p["found"] else "first meeting"
        brief += f"• {p['name']} ({p['email']}) — {p['role']} at {p['company']} — {status}\n"

    brief += """
🎯 Goals
• Understand Ivan's interest in Neotoma and his use case at JME.VC
• Demonstrate Neotoma's capabilities for venture capital workflows
• Identify potential integration opportunities or partnership paths

📋 Agenda
1. Introduction and context — Ivan's background at JME.VC
2. Neotoma overview — memory layer architecture and API
3. Demo relevant use cases for VC operations (portfolio tracking, deal flow, LP communications)
4. Discuss Ivan's specific needs and pain points
5. Explore technical integration possibilities
6. Next steps and follow-up actions

📝 Open questions
• What prompted Ivan's interest in Neotoma?
• What are JME.VC's current tools for portfolio management and relationship tracking?
• Is this exploratory or is there a specific project/need driving the conversation?
• What level of technical involvement would Ivan have in any integration?

✅ Pre-event tasks created
• None identified — this appears to be an exploratory conversation
"""

    print("\n=== Generated Brief ===")
    print(brief)

    # Step 3: Store checkpoint_brief
    print("\nStoring checkpoint_brief in Neotoma...")
    brief_id = create_checkpoint_brief(brief, meeting_event_id=None)
    if brief_id:
        print(f"Stored as entity: {brief_id}")
    else:
        print("Failed to store brief in Neotoma")

    # Step 4: Send to Telegram
    print("\nSending to Telegram...")
    print(brief)
    print("\n[Telegram send not implemented yet — brief printed above]")

if __name__ == "__main__":
    main()
