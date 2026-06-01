#!/usr/bin/env python3
"""
Merge links and timeline into a Neotoma website export JSON.

The export file (e.g. data/tmp/neotoma_website_export.json) is used by
generate_posts_cache.py --from-neotoma-json. It must contain "posts"; this script
adds or overwrites "links" and "timeline" from optional Neotoma entity JSON files.

Entity file format: JSON array. Each item can be:
- Flat: { "name", "url", "icon", "description", "display_order" } for links;
  { "role", "company", "date", "description", "display_order" } for timeline.
- Wrapped: { "snapshot": { ... } } (snapshot is used for the fields above).

Usage:
  python execution/scripts/build_neotoma_website_export.py
  python execution/scripts/build_neotoma_website_export.py --links data/tmp/neotoma_links.json --timeline data/tmp/neotoma_timeline.json
  python execution/scripts/build_neotoma_website_export.py --export data/tmp/neotoma_website_export.json
"""

import json
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent.parent
DATA_DIR = repo_root / "data"
DEFAULT_EXPORT = DATA_DIR / "tmp" / "neotoma_website_export.json"


def _unwrap(e: dict) -> dict:
    if "snapshot" in e and isinstance(e["snapshot"], dict):
        return e["snapshot"]
    return e


def _parse_description(desc):
    if desc is None or (isinstance(desc, str) and not desc.strip()):
        return []
    if isinstance(desc, list):
        return desc
    if isinstance(desc, str):
        try:
            out = json.loads(desc)
            return out if isinstance(out, list) else [str(out)]
        except (json.JSONDecodeError, TypeError):
            return [desc]
    return [str(desc)]


def load_links(path: Path) -> list:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raw = raw.get("entities", raw.get("links", []))
    out = []
    for e in raw:
        e = _unwrap(e)
        out.append(
            {
                "name": e.get("name"),
                "url": e.get("url"),
                "icon": e.get("icon"),
                "description": e.get("description"),
                "display_order": e.get("display_order", 0),
            }
        )
    return out


def load_timeline(path: Path) -> list:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raw = raw.get("entities", raw.get("timeline", []))
    out = []
    for e in raw:
        e = _unwrap(e)
        out.append(
            {
                "role": e.get("role"),
                "company": e.get("company"),
                "date": e.get("date"),
                "description": _parse_description(e.get("description")),
                "display_order": e.get("display_order", 0),
            }
        )
    return out


def main():
    import argparse

    p = argparse.ArgumentParser(
        description="Merge links and timeline into Neotoma website export."
    )
    p.add_argument(
        "--export", type=Path, default=DEFAULT_EXPORT, help="Export JSON path"
    )
    p.add_argument(
        "--links", type=Path, default=None, help="Links entity JSON (from Neotoma)"
    )
    p.add_argument(
        "--timeline",
        type=Path,
        default=None,
        help="Timeline entity JSON (from Neotoma)",
    )
    args = p.parse_args()

    if not args.export.exists():
        print(f"Export file not found: {args.export}", file=sys.stderr)
        sys.exit(1)

    with open(args.export, encoding="utf-8") as f:
        data = json.load(f)

    if args.links and args.links.exists():
        data["links"] = load_links(args.links)
        print(f"Merged {len(data['links'])} links from {args.links}")
    elif "links" not in data:
        data["links"] = []

    if args.timeline and args.timeline.exists():
        data["timeline"] = load_timeline(args.timeline)
        print(f"Merged {len(data['timeline'])} timeline entries from {args.timeline}")
    elif "timeline" not in data:
        data["timeline"] = []

    with open(args.export, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(
        f"Updated {args.export} (posts: {len(data.get('posts', []))}, links: {len(data.get('links', []))}, timeline: {len(data.get('timeline', []))})"
    )


if __name__ == "__main__":
    main()
