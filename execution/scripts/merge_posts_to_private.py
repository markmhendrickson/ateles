#!/usr/bin/env python3
"""
Merge posts.json into posts.private.json, avoiding duplicates.
"""

import json
import sys
from pathlib import Path

# Add project root to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

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
PUBLIC_POSTS = WEBSITE_POSTS_DIR / "posts.json"
PRIVATE_POSTS = WEBSITE_POSTS_DIR / "posts.private.json"


def main():
    # Load public posts
    with open(PUBLIC_POSTS, encoding="utf-8") as f:
        public_posts = json.load(f)

    # Load private posts if exists
    private_posts = []
    if PRIVATE_POSTS.exists():
        with open(PRIVATE_POSTS, encoding="utf-8") as f:
            private_posts = json.load(f)

    # Create slug set for deduplication
    existing_slugs = {post["slug"] for post in private_posts}

    # Merge: add public posts that don't exist in private
    merged = list(private_posts)  # Start with existing private posts
    added = 0

    for post in public_posts:
        if post["slug"] not in existing_slugs:
            merged.append(post)
            existing_slugs.add(post["slug"])
            added += 1

    # Sort by publishedDate (newest first)
    def sort_key(post):
        date = post.get("publishedDate") or "0000-01-01"
        return (date, post.get("title", ""))

    merged.sort(key=sort_key, reverse=True)

    # Write merged posts to private file
    with open(PRIVATE_POSTS, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    print(f"Merged {added} posts from posts.json into posts.private.json")
    print(f"Total posts in posts.private.json: {len(merged)}")
    print(f"  - Published: {sum(1 for p in merged if p.get('published'))}")
    print(f"  - Drafts: {sum(1 for p in merged if not p.get('published'))}")


if __name__ == "__main__":
    main()
