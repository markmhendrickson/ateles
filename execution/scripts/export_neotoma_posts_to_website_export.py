#!/usr/bin/env python3
"""
Convert Neotoma post entities to website export JSON.

Reads entities from a JSON file (from mcp_neotoma_retrieve_entities with entity_type=post),
extracts snapshots, merges raw_fragments (hero_image_square, og_image, etc.), and writes
to the Neotoma website export format. Preserves existing links and timeline from the
target export file.

Usage:
  python execution/scripts/export_neotoma_posts_to_website_export.py --input data/tmp/neotoma_posts_raw.json
  python execution/scripts/export_neotoma_posts_to_website_export.py --input data/tmp/neotoma_posts_raw.json --export data/tmp/neotoma_website_export.json

Input: JSON with {"entities": [{"entity_id", "entity_type", "snapshot", "raw_fragments", ...}, ...]}
Output: data/tmp/neotoma_website_export.json with {"posts": [...], "links": [...], "timeline": [...]}
"""

import json
import os
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", repo_root / "data"))
DEFAULT_INPUT = DATA_DIR / "tmp" / "neotoma_posts_raw.json"
DEFAULT_EXPORT = DATA_DIR / "tmp" / "neotoma_website_export.json"


def entity_to_post(entity: dict) -> dict | None:
    """Convert a Neotoma post entity to export format. Merges raw_fragments into snapshot."""
    snapshot = entity.get("snapshot") or {}
    if entity.get("entity_type") != "post":
        return None
    slug = snapshot.get("slug")
    if not slug:
        return None
    post = dict(snapshot)
    raw = entity.get("raw_fragments") or {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key not in post or post[key] is None:
                post[key] = value
    return post


def main():
    import argparse

    p = argparse.ArgumentParser(
        description="Convert Neotoma post entities to website export"
    )
    p.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="Input JSON (Neotoma entities)",
    )
    p.add_argument(
        "--export", type=Path, default=DEFAULT_EXPORT, help="Output export JSON path"
    )
    args = p.parse_args()

    if not args.input.exists():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        print(
            "Export post entities from Neotoma MCP (retrieve_entities entity_type=post) to this file first.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    entities = (
        data.get("entities", [])
        if isinstance(data, dict)
        else (data if isinstance(data, list) else [])
    )

    posts = []
    seen = {}
    for e in entities:
        post = entity_to_post(e)
        if not post:
            continue
        slug = post.get("slug")
        this_date = post.get("updated_date") or post.get("published_date") or ""
        existing = seen.get(slug)
        existing_date = (
            (existing.get("updated_date") or existing.get("published_date") or "")
            if existing
            else ""
        )
        if existing is None or this_date >= existing_date:
            seen[slug] = post
    posts = list(seen.values())

    # Preserve links and timeline from existing export if present
    export_data = {"posts": posts, "links": [], "timeline": []}
    if args.export.exists():
        with open(args.export, encoding="utf-8") as f:
            existing = json.load(f)
        export_data["links"] = existing.get("links") or []
        export_data["timeline"] = existing.get("timeline") or []

    args.export.parent.mkdir(parents=True, exist_ok=True)
    with open(args.export, "w", encoding="utf-8") as f:
        json.dump(export_data, f, indent=2, ensure_ascii=False)

    print(f"Exported {len(posts)} posts to {args.export}")
    print(
        f"  links: {len(export_data['links'])}, timeline: {len(export_data['timeline'])}"
    )


if __name__ == "__main__":
    main()
