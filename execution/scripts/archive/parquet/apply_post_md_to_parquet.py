#!/usr/bin/env python3
"""
Apply a single post's markdown file to the parquet posts table and regenerate cache.

Usage:
  python3 execution/scripts/apply_post_md_to_parquet.py [slug]
  python3 execution/scripts/apply_post_md_to_parquet.py truth-layer-agent-memory

If the post exists in parquet, updates body and updated_date. If not, adds it using
metadata from posts.private.json (or posts.json) plus body from the markdown file.
Requires parquet MCP server (mcp/parquet). After updating parquet, runs generate_posts_cache.py.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")
WEBSITE_POSTS_DIR = (
    PROJECT_ROOT
    / "execution"
    / "website"
    / "markmhendrickson"
    / "react-app"
    / "src"
    / "content"
    / "posts"
)
DRAFTS_DIR = WEBSITE_POSTS_DIR / "drafts"
POSTS_JSON = WEBSITE_POSTS_DIR / "posts.json"
POSTS_PRIVATE_JSON = WEBSITE_POSTS_DIR / "posts.private.json"

sys.path.insert(0, str(SCRIPT_DIR))
from parquet_client import ParquetMCPClient


def find_parquet_server():
    """Resolve parquet MCP server path."""
    import os

    env_path = os.getenv("PARQUET_MCP_SERVER_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    for base in [PROJECT_ROOT, SCRIPT_DIR.parent]:
        candidate = base / "mcp" / "parquet" / "parquet_mcp_server.py"
        if candidate.exists():
            return str(candidate)
    return None


def read_markdown(slug: str) -> str | None:
    """Read post body from markdown file (published or drafts)."""
    for directory in (WEBSITE_POSTS_DIR, DRAFTS_DIR):
        md_file = directory / f"{slug}.md"
        if md_file.exists():
            return md_file.read_text(encoding="utf-8")
    return None


def read_summary_markdown(slug: str) -> str | None:
    """Read key takeaways from {slug}.summary.md (published or drafts)."""
    for directory in (WEBSITE_POSTS_DIR, DRAFTS_DIR):
        summary_file = directory / f"{slug}.summary.md"
        if summary_file.exists():
            return summary_file.read_text(encoding="utf-8").strip()
    return None


def load_metadata_for_slug(slug: str) -> dict | None:
    """Load post metadata from cache JSON (private first, then public)."""
    for path in (POSTS_PRIVATE_JSON, POSTS_JSON):
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            posts = json.load(f)
        for p in posts:
            if p.get("slug") == slug:
                return p
    return None


def meta_to_record(
    meta: dict, body: str, today: str, summary: str | None = None
) -> dict:
    """Build parquet record from cache metadata, body, and optional summary."""
    tags = meta.get("tags", [])
    if isinstance(tags, list):
        tags = json.dumps(tags)
    rec = {
        "slug": meta["slug"],
        "title": meta.get("title", ""),
        "excerpt": meta.get("excerpt", ""),
        "published": meta.get("published", False),
        "published_date": meta.get("publishedDate") or meta.get("published_date"),
        "category": meta.get("category", "essay"),
        "read_time": meta.get("readTime") or meta.get("read_time") or 1,
        "tags": tags,
        "hero_image": meta.get("heroImage") or meta.get("hero_image"),
        "hero_image_style": meta.get("heroImageStyle") or meta.get("hero_image_style"),
        "exclude_from_listing": meta.get("excludeFromListing")
        or meta.get("exclude_from_listing")
        or False,
        "show_metadata": (
            meta.get("showMetadata")
            if "showMetadata" in meta
            else meta.get("show_metadata", True)
        ),
        "body": body,
        "created_date": meta.get("createdDate") or meta.get("created_date") or today,
        "updated_date": today,
    }
    if summary is not None:
        rec["summary"] = summary
    elif meta.get("summary") is not None:
        rec["summary"] = meta["summary"]
    return rec


def main():
    parser = argparse.ArgumentParser(
        description="Apply post markdown to parquet and regenerate cache"
    )
    parser.add_argument(
        "slug",
        nargs="?",
        default="truth-layer-agent-memory",
        help="Post slug (default: truth-layer-agent-memory)",
    )
    args = parser.parse_args()
    slug = args.slug

    body = read_markdown(slug)
    if body is None:
        print(
            f"ERROR: No markdown file found for slug '{slug}' in {WEBSITE_POSTS_DIR} or {DRAFTS_DIR}"
        )
        sys.exit(1)

    server_path = find_parquet_server()
    if not server_path:
        print(
            "ERROR: Parquet MCP server not found. Set PARQUET_MCP_SERVER_PATH or run from repo with mcp/parquet."
        )
        sys.exit(1)

    summary = read_summary_markdown(slug)
    client = ParquetMCPClient(parquet_server_path=server_path)
    today = datetime.now().strftime("%Y-%m-%d")
    updates = {"body": body, "updated_date": today}
    if summary is not None:
        updates["summary"] = summary
    try:
        result = client.call_tool_sync(
            "update_records",
            {
                "data_type": "posts",
                "filters": {"slug": slug},
                "updates": updates,
            },
        )
        updated = result.get("rows_updated", 0)
        if updated > 0:
            msg = f"Updated post '{slug}' in parquet (body + updated_date)"
            if summary is not None:
                msg += " and key takeaways from .summary.md"
            print(msg + ".")
        else:
            meta = load_metadata_for_slug(slug)
            if meta is None:
                print(
                    f"ERROR: No post in parquet and no metadata for '{slug}' in {POSTS_JSON} / {POSTS_PRIVATE_JSON}. Add metadata or run migrate_posts_to_parquet.py."
                )
                sys.exit(1)
            record = meta_to_record(meta, body, today, summary=summary)
            client.call_tool_sync(
                "add_record", {"data_type": "posts", "record": record}
            )
            print(
                f"Added post '{slug}' to parquet (from cache metadata + markdown body)."
            )
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print("Regenerating posts cache...")
    subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "generate_posts_cache.py")],
        cwd=PROJECT_ROOT,
        check=True,
    )
    print("Done.")


if __name__ == "__main__":
    main()
