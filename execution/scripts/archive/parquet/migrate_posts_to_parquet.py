#!/usr/bin/env python3
"""
Migrate website posts from markdown/JSON to parquet MCP storage.

Creates posts data type if it doesn't exist, then migrates all existing posts
from execution/website/markmhendrickson/react-app/src/content/posts/ to parquet.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution" / "scripts"))

from parquet_client import ParquetMCPClient

# Paths
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
POSTS_JSON = WEBSITE_POSTS_DIR / "posts.json"
POSTS_PRIVATE_JSON = WEBSITE_POSTS_DIR / "posts.private.json"
DRAFTS_DIR = WEBSITE_POSTS_DIR / "drafts"

# Load environment variables from .env file
from dotenv import load_dotenv

env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    load_dotenv(env_file)

# Get DATA_DIR from environment
DATA_DIR = Path(os.environ.get("DATA_DIR", ""))
if not DATA_DIR or not DATA_DIR.exists():
    print("ERROR: DATA_DIR not set or does not exist")
    print(f"Please set DATA_DIR environment variable in .env file: {env_file}")
    sys.exit(1)

print(f"Using DATA_DIR: {DATA_DIR}")

SCHEMAS_DIR = DATA_DIR / "schemas"
POSTS_DIR = DATA_DIR / "posts"
POSTS_SCHEMA_FILE = SCHEMAS_DIR / "posts_schema.json"


def create_posts_schema():
    """Create posts schema file if it doesn't exist."""
    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)

    schema = {
        "schema": {
            "slug": "string",
            "title": "string",
            "excerpt": "string",
            "summary": "string",
            "published": "boolean",
            "published_date": "date_string",
            "category": "string",
            "read_time": "integer",
            "tags": "string",
            "hero_image": "string",
            "hero_image_style": "string",
            "exclude_from_listing": "boolean",
            "show_metadata": "boolean",
            "body": "string",
            "created_date": "date_string",
            "updated_date": "date_string",
            "linked_tweet_url": "string",
            "x_timeline_url": "string",
        },
        "description": "Blog posts and essays for markmhendrickson.com website. Stores full markdown content and metadata. Tags are stored as JSON string array.",
        "version": "1.0.0",
        "notes": "body field contains full markdown content. summary field is optional Executive Synthesis (markdown, rendered above body). tags field is JSON-encoded array. dates are ISO strings (YYYY-MM-DD) or null.",
    }

    if POSTS_SCHEMA_FILE.exists():
        with open(POSTS_SCHEMA_FILE, encoding="utf-8") as f:
            existing = json.load(f)
        existing_schema = existing.get("schema", {})
        updated = False
        if "summary" not in existing_schema:
            existing_schema["summary"] = "string"
            updated = True
        if "linked_tweet_url" not in existing_schema:
            existing_schema["linked_tweet_url"] = "string"
            updated = True
        if "x_timeline_url" not in existing_schema:
            existing_schema["x_timeline_url"] = "string"
            updated = True
        if updated:
            existing["schema"] = existing_schema
            with open(POSTS_SCHEMA_FILE, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            print(f"Updated schema file: {POSTS_SCHEMA_FILE}")
        else:
            print(f"Schema file already exists: {POSTS_SCHEMA_FILE}")
        return

    with open(POSTS_SCHEMA_FILE, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)

    print(f"Created schema file: {POSTS_SCHEMA_FILE}")


def initialize_empty_parquet():
    """Initialize empty parquet file if it doesn't exist."""
    import pandas as pd

    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    parquet_file = POSTS_DIR / "posts.parquet"

    if parquet_file.exists():
        print(f"Parquet file already exists: {parquet_file}")
        return

    # Create empty DataFrame with correct schema
    df = pd.DataFrame(
        columns=[
            "slug",
            "title",
            "excerpt",
            "summary",
            "published",
            "published_date",
            "category",
            "read_time",
            "tags",
            "hero_image",
            "hero_image_style",
            "exclude_from_listing",
            "show_metadata",
            "body",
            "created_date",
            "updated_date",
        ]
    )

    # Set correct dtypes
    df = df.astype(
        {
            "slug": str,
            "title": str,
            "excerpt": str,
            "summary": str,
            "published": bool,
            "published_date": str,
            "category": str,
            "read_time": "Int64",
            "tags": str,
            "hero_image": str,
            "hero_image_style": str,
            "exclude_from_listing": bool,
            "show_metadata": bool,
            "body": str,
            "created_date": str,
            "updated_date": str,
        }
    )

    df.to_parquet(parquet_file, index=False)
    print(f"Created empty parquet file: {parquet_file}")


def load_metadata():
    """Load post metadata from JSON files."""
    # Start with public posts
    with open(POSTS_JSON, encoding="utf-8") as f:
        posts = json.load(f)

    # Merge with private posts if exists
    if POSTS_PRIVATE_JSON.exists():
        with open(POSTS_PRIVATE_JSON, encoding="utf-8") as f:
            private_posts = json.load(f)

        # Create slug map
        slug_map = {post["slug"]: post for post in posts}

        # Override with private posts
        for post in private_posts:
            slug_map[post["slug"]] = post

        posts = list(slug_map.values())

    return {post["slug"]: post for post in posts}


def read_markdown_file(slug, published=True):
    """Read markdown content from file."""
    # Check published directory
    md_file = WEBSITE_POSTS_DIR / f"{slug}.md"
    if md_file.exists():
        with open(md_file, encoding="utf-8") as f:
            return f.read()

    # Check drafts directory if not published
    if not published and DRAFTS_DIR.exists():
        md_file = DRAFTS_DIR / f"{slug}.md"
        if md_file.exists():
            with open(md_file, encoding="utf-8") as f:
                return f.read()

    return None


def migrate_posts(client: ParquetMCPClient, dry_run=False):
    """Migrate posts from markdown/JSON to parquet."""
    print("\nLoading post metadata...")
    metadata = load_metadata()
    print(f"Found {len(metadata)} posts in metadata files")

    # Check existing posts in parquet
    print("\nChecking existing posts in parquet...")
    try:
        existing_posts = client.call_tool_sync("read_parquet", {"data_type": "posts"})
        existing_slugs = {post["slug"] for post in existing_posts.get("data", [])}
        print(f"Found {len(existing_slugs)} existing posts in parquet")
    except Exception as e:
        print(f"No existing posts in parquet (or error reading): {e}")
        existing_slugs = set()

    # Migrate each post
    migrated = 0
    skipped = 0
    errors = []

    for slug, meta in metadata.items():
        if slug in existing_slugs:
            print(f"  Skipping {slug} (already exists)")
            skipped += 1
            continue

        # Read markdown content
        body = read_markdown_file(slug, published=meta.get("published", True))
        if body is None:
            print(f"  WARNING: No markdown file found for {slug}")
            body = ""

        # Prepare record
        record = {
            "slug": slug,
            "title": meta.get("title", ""),
            "excerpt": meta.get("excerpt", ""),
            "published": meta.get("published", False),
            "published_date": meta.get("publishedDate") or meta.get("published_date"),
            "category": meta.get("category", "essay"),
            "read_time": meta.get("readTime") or meta.get("read_time") or 1,
            "tags": json.dumps(meta.get("tags", [])),
            "hero_image": meta.get("heroImage") or meta.get("hero_image"),
            "hero_image_style": meta.get("heroImageStyle")
            or meta.get("hero_image_style"),
            "exclude_from_listing": meta.get("excludeFromListing")
            or meta.get("exclude_from_listing")
            or False,
            "show_metadata": (
                meta.get("showMetadata")
                if "showMetadata" in meta
                else meta.get("show_metadata", True)
            ),
            "body": body,
            "created_date": datetime.now().strftime("%Y-%m-%d"),
            "updated_date": datetime.now().strftime("%Y-%m-%d"),
        }

        if dry_run:
            print(f"  [DRY RUN] Would migrate: {slug}")
            migrated += 1
            continue

        # Add to parquet via MCP
        try:
            client.call_tool_sync(
                "add_record", {"data_type": "posts", "record": record}
            )
            print(f"  Migrated: {slug}")
            migrated += 1
        except Exception as e:
            print(f"  ERROR migrating {slug}: {e}")
            errors.append((slug, str(e)))

    print(f"\n{'='*60}")
    print(f"Migration {'simulation' if dry_run else 'complete'}!")
    print(f"  Migrated: {migrated} posts")
    print(f"  Skipped: {skipped} posts (already exist)")
    if errors:
        print(f"  Errors: {len(errors)} posts")
        for slug, error in errors:
            print(f"    - {slug}: {error}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Migrate posts to parquet MCP storage")
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate migration without writing"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Migrating Posts to Parquet MCP Storage")
    print("=" * 60)

    print("\n1. Creating posts schema...")
    create_posts_schema()

    print("\n2. Initializing empty parquet file...")
    initialize_empty_parquet()

    print("\n3. Setting up MCP client...")
    client = ParquetMCPClient()

    print("\n4. Migrating posts...")
    migrate_posts(client, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
