#!/usr/bin/env python3
"""
Migrate markmhendrickson website content JSON into parquet via MCP.

Migrates:
- links.json -> links.parquet
- timeline.json -> timeline.parquet
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from parquet_client import ParquetMCPClient


def load_repo_env() -> Path:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent.parent
    env_file = repo_root / ".env"
    if env_file.exists():
        load_dotenv(env_file)
    return repo_root


def load_json(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def to_date_string(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip()


def split_date_location(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    if "·" in value:
        parts = [part.strip() for part in value.split("·", 1)]
        return parts[0] or None, parts[1] or None
    return value.strip(), None


def infer_entry_type(role: str | None, company: str | None) -> str:
    role_value = (role or "").lower()
    company_value = (company or "").lower()
    if (
        "college" in company_value
        or "high school" in role_value
        or "high school" in company_value
    ):
        return "education"
    if role_value.startswith("ba"):
        return "education"
    return "work"


def migrate_links(client: ParquetMCPClient, links: list[dict]) -> None:
    client.call_tool_sync("create_data_type", {"data_type": "links"})
    existing = client.call_tool_sync("read_parquet", {"data_type": "links"})
    if existing.get("data"):
        print("Links already exist in parquet. Skipping migration.")
        return

    today = date.today().isoformat()
    for index, link in enumerate(links, start=1):
        url = link.get("url") or ""
        category = "contact" if url.startswith("mailto:") else "social"
        record = {
            "link_id": uuid.uuid4().hex[:16],
            "name": link.get("name"),
            "url": link.get("url"),
            "icon": link.get("icon"),
            "description": link.get("description"),
            "category": category,
            "display_order": index,
            "active": True,
            "created_date": today,
            "updated_date": today,
            "import_date": today,
            "import_source_file": "website_links_json",
        }
        client.call_tool_sync("add_record", {"data_type": "links", "record": record})

    print(f"Migrated {len(links)} links into parquet.")


def migrate_timeline(client: ParquetMCPClient, timeline: list[dict]) -> None:
    client.call_tool_sync("create_data_type", {"data_type": "timeline"})
    existing = client.call_tool_sync("read_parquet", {"data_type": "timeline"})
    if existing.get("data"):
        print("Timeline already exists in parquet. Skipping migration.")
        return

    today = date.today().isoformat()
    for index, entry in enumerate(timeline, start=1):
        date_text = entry.get("date")
        date_value, location = split_date_location(date_text)
        description = entry.get("description") or []
        if isinstance(description, list):
            description_value = json.dumps(description, ensure_ascii=False)
        else:
            description_value = str(description)

        record = {
            "entry_id": uuid.uuid4().hex[:16],
            "role": entry.get("role"),
            "company": entry.get("company"),
            "date": to_date_string(date_text),
            "description": description_value,
            "location": location,
            "display_order": index,
            "entry_type": infer_entry_type(entry.get("role"), entry.get("company")),
            "start_date": None,
            "end_date": None,
            "created_date": today,
            "updated_date": today,
            "import_date": today,
            "import_source_file": "website_timeline_json",
        }
        client.call_tool_sync("add_record", {"data_type": "timeline", "record": record})

    print(f"Migrated {len(timeline)} timeline entries into parquet.")


def main() -> None:
    repo_root = load_repo_env()
    parquet_server_path = repo_root / "mcp" / "parquet" / "parquet_mcp_server.py"
    client = ParquetMCPClient(parquet_server_path=str(parquet_server_path))

    links_path = (
        repo_root
        / "execution"
        / "website"
        / "markmhendrickson"
        / "react-app"
        / "src"
        / "data"
        / "links.json"
    )
    timeline_path = (
        repo_root
        / "execution"
        / "website"
        / "markmhendrickson"
        / "react-app"
        / "src"
        / "data"
        / "timeline.json"
    )

    links = load_json(links_path)
    timeline = load_json(timeline_path)

    migrate_links(client, links)
    migrate_timeline(client, timeline)


if __name__ == "__main__":
    main()
