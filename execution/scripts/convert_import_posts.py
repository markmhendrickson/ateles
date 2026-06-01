#!/usr/bin/env python3
"""
Convert imported posts from $DATA_DIR/imports/posts/ to website format.

Reads JSON files from imports directory and converts them to:
- Markdown files in execution/website/markmhendrickson/react-app/src/content/posts/
- Metadata entries in posts.json
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add project root to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.scripts.config import DATA_DIR

# Paths
IMPORTS_POSTS_DIR = DATA_DIR / "imports" / "posts"
WEBSITE_POSTS_DIR = (
    Path(__file__).parent.parent
    / "website"
    / "markmhendrickson"
    / "react-app"
    / "src"
    / "content"
    / "posts"
)
POSTS_JSON = WEBSITE_POSTS_DIR / "posts.json"

# Reading speed: average 200 words per minute
WORDS_PER_MINUTE = 200


def normalize_slug(post_id: str) -> str:
    """Convert post ID to kebab-case slug."""
    # Already in kebab-case, but ensure it's clean
    slug = post_id.lower().strip()
    # Remove any non-alphanumeric except hyphens
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    # Replace multiple hyphens with single hyphen
    slug = re.sub(r"-+", "-", slug)
    return slug


def parse_date(date_str: Optional[str]) -> Optional[str]:
    """Convert date string to YYYY-MM-DD format."""
    if not date_str:
        return None

    # Try various date formats
    formats = [
        "%Y-%m-%dT%H:%M:%S%z",  # ISO with timezone
        "%Y-%m-%dT%H:%M:%S",  # ISO without timezone
        "%Y-%m-%d",  # Simple date
        "%Y-%m-%d %H:%M:%S",  # Space-separated
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue

    # If all formats fail, try to extract just the date part
    match = re.search(r"(\d{4}-\d{2}-\d{2})", date_str)
    if match:
        return match.group(1)

    return None


def calculate_read_time(body: str) -> int:
    """Calculate reading time in minutes from word count."""
    if not body:
        return 1

    # Count words (split by whitespace)
    words = len(body.split())
    # Round up to nearest minute
    minutes = max(1, (words + WORDS_PER_MINUTE - 1) // WORDS_PER_MINUTE)
    return minutes


def generate_excerpt(body: str, max_length: int = 150) -> str:
    """Generate excerpt from body content."""
    if not body:
        return ""

    # Remove markdown formatting for excerpt
    text = re.sub(r"[#*_`\[\]()]", "", body)
    text = re.sub(r"\n+", " ", text)
    text = text.strip()

    if len(text) <= max_length:
        return text

    # Truncate at word boundary
    truncated = text[:max_length].rsplit(" ", 1)[0]
    return truncated + "..."


def determine_category(title: str, body: str) -> str:
    """Determine post category based on title and content."""
    title_lower = title.lower()
    body_lower = body.lower() if body else ""
    combined = f"{title_lower} {body_lower}"

    # Technical keywords
    if any(
        word in combined
        for word in [
            "code",
            "programming",
            "api",
            "software",
            "technical",
            "development",
            "design",
            "architecture",
        ]
    ):
        return "technical"

    # Article keywords
    if any(
        word in combined
        for word in ["analysis", "review", "report", "guide", "how-to", "tutorial"]
    ):
        return "article"

    # Default to essay
    return "essay"


def clean_body_content(body: str) -> str:
    """Clean up body content, removing template syntax and fixing formatting."""
    if not body:
        return ""

    # Remove Handlebars template syntax (e.g., {{#link-to 'post' $post-content.id}})
    body = re.sub(r"\{\{#link-to[^}]+\}\}", "", body)
    body = re.sub(r"\{\{/\#link-to\}\}", "", body)
    body = re.sub(r"\{\{\$[^}]+\}\}", "", body)
    body = re.sub(r"\{\{[^}]+\}\}", "", body)

    # Clean up extra whitespace
    body = re.sub(r"\n{3,}", "\n\n", body)

    return body.strip()


def get_body_content(post_data: dict, post_id: str) -> Optional[str]:
    """Get body content from JSON or separate .body.md file."""
    # First try body in JSON
    body = post_data.get("attributes", {}).get("body")
    if body:
        return clean_body_content(body)

    # Try separate .body.md file
    body_file = IMPORTS_POSTS_DIR / f"{post_id}.body.md"
    if body_file.exists():
        return body_file.read_text(encoding="utf-8").strip()

    return None


def convert_post(json_file: Path) -> Optional[dict]:
    """Convert a single post JSON file to website format."""
    try:
        with open(json_file, encoding="utf-8") as f:
            post_data = json.load(f)
    except Exception as e:
        print(f"Error reading {json_file.name}: {e}")
        return None

    post_id = post_data.get("id", json_file.stem)
    attributes = post_data.get("attributes", {})

    # Get body content
    body = get_body_content(post_data, post_id)
    if not body:
        print(f"Warning: No body content found for {post_id}, skipping")
        return None

    # Extract metadata
    slug = normalize_slug(post_id)
    title = attributes.get("title")
    if not title:
        # Generate title from slug (convert kebab-case to Title Case)
        title = slug.replace("-", " ").title()
        # Or use first line of body if available
        if body:
            first_line = body.split("\n")[0].strip()
            # Remove markdown formatting
            first_line = re.sub(r"[#*_`\[\]()]", "", first_line)
            if len(first_line) > 10 and len(first_line) < 100:
                title = first_line
    published_at = parse_date(attributes.get("published-at"))
    excerpt = attributes.get("excerpt") or generate_excerpt(body)
    read_time = calculate_read_time(body)
    category = determine_category(title, body)

    # Determine if published
    published = published_at is not None

    return {
        "slug": slug,
        "title": title,
        "excerpt": excerpt,
        "published": published,
        "publishedDate": published_at,
        "category": category,
        "readTime": read_time,
        "tags": [],
        "body": body,
    }


def main():
    """Convert all posts from imports directory."""
    print(f"Reading posts from: {IMPORTS_POSTS_DIR}")
    print(f"Writing posts to: {WEBSITE_POSTS_DIR}")

    # Ensure output directory exists
    WEBSITE_POSTS_DIR.mkdir(parents=True, exist_ok=True)

    # Find all JSON files
    json_files = sorted(IMPORTS_POSTS_DIR.glob("*.json"))
    print(f"\nFound {len(json_files)} JSON files")

    # Load existing posts.json if it exists
    existing_posts = []
    if POSTS_JSON.exists():
        try:
            with open(POSTS_JSON, encoding="utf-8") as f:
                existing_posts = json.load(f)
                print(f"Loaded {len(existing_posts)} existing posts from posts.json")
        except Exception as e:
            print(f"Warning: Could not read existing posts.json: {e}")
            existing_posts = []

    # Track existing slugs to avoid duplicates
    existing_slugs = {post.get("slug") for post in existing_posts if post.get("slug")}

    # Convert all posts
    converted_posts = []
    skipped = 0

    for json_file in json_files:
        print(f"\nProcessing: {json_file.name}")
        post = convert_post(json_file)

        if not post:
            skipped += 1
            continue

        # Check for duplicate slug
        slug = post["slug"]
        if slug in existing_slugs:
            print(f"  Warning: Slug '{slug}' already exists, appending number")
            counter = 1
            while f"{slug}-{counter}" in existing_slugs:
                counter += 1
            slug = f"{slug}-{counter}"
            post["slug"] = slug

        existing_slugs.add(slug)

        # Write markdown file
        md_file = WEBSITE_POSTS_DIR / f"{slug}.md"
        md_file.write_text(post["body"], encoding="utf-8")
        print(f"  Created: {md_file.name}")

        # Remove body from metadata (not stored in JSON)
        post_metadata = {k: v for k, v in post.items() if k != "body"}
        converted_posts.append(post_metadata)

    # Merge with existing posts
    all_posts = existing_posts + converted_posts

    # Sort by publishedDate (newest first), then by title
    def sort_key(post):
        date = post.get("publishedDate") or "0000-01-01"
        return (date, post.get("title", ""))

    all_posts.sort(key=sort_key, reverse=True)

    # Write updated posts.json
    with open(POSTS_JSON, "w", encoding="utf-8") as f:
        json.dump(all_posts, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print("Conversion complete!")
    print(f"  Converted: {len(converted_posts)} posts")
    print(f"  Skipped: {skipped} posts")
    print(f"  Total posts in posts.json: {len(all_posts)}")
    print(f"  Updated: {POSTS_JSON}")


if __name__ == "__main__":
    main()
