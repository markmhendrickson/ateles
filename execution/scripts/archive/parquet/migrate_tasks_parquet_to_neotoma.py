#!/usr/bin/env python3
"""
Migrate tasks from legacy Parquet to Neotoma with canonical field mapping.

Reads all task rows from Parquet file, maps them to the canonical Neotoma task
model (classification=urgent/nonurgent/scheduled, explicit category/domain),
and stores via neotoma CLI in batches.

Initial full migration completed 2026-03-31: 16,180 records, 0 errors.

Usage:
  python migrate_tasks_parquet_to_neotoma.py [--dry-run] [--limit N] [--offset N] [--batch-size N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from shutil import which

PAGE_SIZE = 200
DEFAULT_BATCH_SIZE = 25

PRIORITY_TO_CLASSIFICATION = {
    "critical": "urgent",
    "high": "urgent",
    "medium": "nonurgent",
    "low": "nonurgent",
    "normal": "nonurgent",
}

DOMAIN_TO_CATEGORY = {
    "finance": "finances",
    "social": "social",
    "admin": "admin",
    "work": "work",
    "health": "health",
    "other": None,
}

PROJECT_TO_CATEGORY = {
    "social (private)": "social",
    "finances (private)": "finances",
    "finances": "finances",
    "finances (analysis)": "finances",
    "home (private)": "home",
    "home": "home",
    "stylebee": "work",
    "blockstack": "work",
    "health & sports (private)": "health",
    "organization (private)": "organization",
    "organization": "organization",
    "travel (private)": "travel",
    "apartment": "home",
    "apartment supplies & purchases": "home",
    "humans": "social",
    "buy": "purchases",
    "purchases & maintenance": "purchases",
    "neotoma": "work",
    "health": "health",
    "rachel gillum": "social",
    "social": "social",
    "clothes & gear (private)": "purchases",
    "website": "work",
    "food & cooking (private)": "food",
    "live ultimate": "work",
    "legal (private)": "legal",
    "hiking": "recreation",
    "tontitos": "social",
    "career": "work",
    "knowledge": "learning",
}


def classify_task(priority: str | None, due_date: str | None) -> str:
    if due_date and due_date.strip():
        return "scheduled"
    if not priority:
        return "nonurgent"
    return PRIORITY_TO_CLASSIFICATION.get(priority.strip().lower(), "nonurgent")


def derive_category(
    project_names: str | None, domain: str | None, section_names: str | None
) -> str | None:
    if project_names:
        key = project_names.strip().lower()
        cat = PROJECT_TO_CATEGORY.get(key)
        if cat:
            return cat
        return project_names.strip()

    if domain:
        key = domain.strip().lower()
        cat = DOMAIN_TO_CATEGORY.get(key)
        if cat:
            return cat
        if key != "other":
            return domain.strip()

    if section_names:
        return section_names.strip()

    return None


def map_parquet_to_neotoma(row: dict) -> dict:
    priority = (row.get("priority") or "").strip().lower() or None
    due_date = row.get("due_date")
    if due_date is not None:
        due_date = str(due_date).strip() or None

    classification = classify_task(priority, due_date)

    category = derive_category(
        row.get("project_names"),
        row.get("domain"),
        row.get("section_names"),
    )

    entity = {
        "entity_type": "task",
        "title": (row.get("title") or "").strip() or None,
        "status": (row.get("status") or "").strip().lower() or None,
        "classification": classification,
        "due_date": due_date,
        "start_date": row.get("start_date") or None,
        "completed_date": row.get("completed_date") or None,
        "notes": (row.get("notes") or "").strip() or None,
        "description": (row.get("description") or "").strip() or None,
        "category": category,
        "domain": (row.get("domain") or "").strip() or None,
        "project_names": (row.get("project_names") or "").strip() or None,
        "project_ids": (row.get("project_ids") or "").strip() or None,
        "section_names": (row.get("section_names") or "").strip() or None,
        "outcome_names": (row.get("outcome_names") or "").strip() or None,
        "outcome_ids": (row.get("outcome_ids") or "").strip() or None,
        "assignee_name": (row.get("assignee_name") or "").strip() or None,
        "asana_source_gid": (row.get("asana_source_gid") or "").strip() or None,
        "permalink_url": (row.get("permalink_url") or "").strip() or None,
        "priority": priority,
        "import_date": str(row.get("import_date") or ""),
        "import_source_file": row.get("import_source_file") or "parquet_migration",
        "source": "parquet_migration",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }

    entity = {k: v for k, v in entity.items() if v is not None and v != ""}
    return entity


def idempotency_key_for(row: dict) -> str:
    gid = row.get("asana_source_gid") or row.get("task_id") or ""
    if gid:
        return f"migrate-task-parquet-{gid}"
    title = (row.get("title") or "").strip()
    h = hashlib.sha256(title.encode()).hexdigest()[:16]
    return f"migrate-task-parquet-hash-{h}"


def neotoma_store_batch(
    entities: list[dict], idempotency_key: str, timeout: int = 120
) -> tuple[bool, str]:
    if not which("neotoma"):
        return False, "neotoma CLI not found on PATH"

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", delete=False
    ) as f:
        json.dump(entities, f, ensure_ascii=False, default=str)
        tmp_path = f.name

    try:
        cmd = [
            "neotoma",
            "store",
            "--file",
            tmp_path,
            "--idempotency-key",
            idempotency_key,
            "--api-only",
        ]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return False, (p.stderr or p.stdout or "neotoma store failed").strip()
        return True, p.stdout.strip()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Migrate Parquet tasks to Neotoma")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print mapped entities without storing"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Max total rows to process (0=all)"
    )
    parser.add_argument("--offset", type=int, default=0, help="Start offset in Parquet")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Entities per store call",
    )
    parser.add_argument(
        "--status-filter",
        type=str,
        default=None,
        help="Only migrate tasks with this status",
    )
    args = parser.parse_args()

    print(
        f"Migration parameters: dry_run={args.dry_run}, limit={args.limit}, offset={args.offset}, batch_size={args.batch_size}"
    )

    total_processed = 0
    total_stored = 0
    total_skipped = 0
    total_errors = 0
    offset = args.offset
    batch_buffer: list[tuple[dict, str]] = []

    while True:
        page_limit = (
            min(PAGE_SIZE, args.limit - total_processed)
            if args.limit > 0
            else PAGE_SIZE
        )
        if page_limit <= 0:
            break

        print(f"\nReading Parquet rows offset={offset}, limit={page_limit}...")
        cmd = [
            "neotoma",
            "parquet",
            "read",
            "--data-type",
            "tasks",
            "--offset",
            str(offset),
            "--limit",
            str(page_limit),
        ]

        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            print(
                "ERROR: neotoma CLI not found. Using Parquet MCP fallback not implemented in script."
            )
            sys.exit(1)

        if p.returncode != 0:
            print(f"ERROR reading parquet: {p.stderr or p.stdout}")
            break

        try:
            page_data = json.loads(p.stdout)
        except json.JSONDecodeError:
            print("ERROR parsing parquet response, trying to read from output...")
            break

        rows = page_data.get("data", [])
        if not rows:
            print("No more rows.")
            break

        for row in rows:
            if (
                args.status_filter
                and (row.get("status") or "").strip().lower() != args.status_filter
            ):
                total_skipped += 1
                continue

            entity = map_parquet_to_neotoma(row)
            idem_key = idempotency_key_for(row)

            if args.dry_run:
                title = entity.get("title", "?")[:60]
                cls = entity.get("classification", "?")
                cat = entity.get("category", "?")
                print(f"  [DRY] {title} | {cls} | {cat} | {idem_key}")
                total_stored += 1
            else:
                batch_buffer.append((entity, idem_key))

                if len(batch_buffer) >= args.batch_size:
                    ok = flush_batch(batch_buffer)
                    if ok:
                        total_stored += len(batch_buffer)
                    else:
                        total_errors += len(batch_buffer)
                    batch_buffer = []

            total_processed += 1

        offset += len(rows)
        has_more = page_data.get("has_more", False)
        if not has_more:
            break

    if batch_buffer and not args.dry_run:
        ok = flush_batch(batch_buffer)
        if ok:
            total_stored += len(batch_buffer)
        else:
            total_errors += len(batch_buffer)

    print("\n=== Migration Summary ===")
    print(f"Total processed: {total_processed}")
    print(f"Total stored: {total_stored}")
    print(f"Total skipped: {total_skipped}")
    print(f"Total errors: {total_errors}")


def flush_batch(buffer: list[tuple[dict, str]]) -> bool:
    entities = [e for e, _ in buffer]
    keys = [k for _, k in buffer]
    batch_key = keys[0] if len(keys) == 1 else f"{keys[0]}__to__{keys[-1]}"

    ok, msg = neotoma_store_batch(entities, batch_key)
    if ok:
        print(f"  Stored batch of {len(entities)} tasks")
    else:
        print(f"  ERROR storing batch: {msg}")

    time.sleep(0.2)
    return ok


if __name__ == "__main__":
    main()
