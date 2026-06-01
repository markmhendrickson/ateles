#!/usr/bin/env python3
"""
PithBot: Extract the pithiest statements from blog posts and compile them
into a structured wisdom collection.

Reads published post markdown files, sends them to OpenAI to extract
quotable/pithy statements, and outputs a JSON data file for the website.

Usage:
    python generate_pithbot.py
    python generate_pithbot.py --dry-run          # Preview without calling LLM
    python generate_pithbot.py --posts-dir <path>  # Custom posts directory
    python generate_pithbot.py --max-posts 5       # Limit for testing
"""

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution" / "scripts"))

from dotenv import load_dotenv

env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    load_dotenv(env_file)

REACT_APP_ROOT = (
    PROJECT_ROOT / "execution" / "website" / "markmhendrickson" / "react-app"
).resolve()
DEFAULT_POSTS_DIR = REACT_APP_ROOT / "src" / "content" / "posts"
CACHE_DIR = REACT_APP_ROOT / "cache"
WISDOM_JSON = CACHE_DIR / "wisdom.json"
PUBLIC_API_DIR = REACT_APP_ROOT / "public" / "api"
API_WISDOM_JSON = PUBLIC_API_DIR / "wisdom.json"
SITE_BASE = "https://markmhendrickson.com"

SKIP_SLUGS = {
    "content",
    "draft-example",
    "kanban",
    "relationship",
    "share-frequency",
    "README",
    "tutor-catala",
    "alps-snowboarding-recommendations",
    "barcelona-guest-floor",
    "foursquare-swarm-checkins-sync",
    "sync-server-dropbox-api-version-2-fix",
    "park-ranger-grunt-hoist-proxy",
    "touch-bar-autosuggest",
    "yc-w19-web-developer-contract",
    "the-outline",
    "the-flip-side",
}

SKIP_SUFFIXES = {"-1", "-old"}

SYSTEM_PROMPT = """You are PithBot, a literary curator with a sharp eye for the most quotable,
memorable, and wisdom-dense statements in essays about technology, startups, AI, and life.

Your task: given a blog post, extract the 3-8 pithiest statements. These should be:
- Standalone: make sense without surrounding context
- Memorable: the kind of line someone would highlight, quote, or share
- Sharp: concise, opinionated, or surprising
- Varied: capture different ideas from across the post, not just the conclusion

Each quote should be a direct extraction or very light paraphrase (preserving the author's voice).
Do NOT invent quotes. Only extract what is actually in the text.

Return ONLY a JSON array of objects, each with:
- "quote": the pithy statement (1-3 sentences max)
- "context": a 5-10 word label for the theme or section it comes from

Example output:
[
  {"quote": "The humanness isn't in typing every word. It's in deciding what's worth saying.", "context": "AI and creative authenticity"},
  {"quote": "Memory that you can correct and trace becomes infrastructure, not a convenience.", "context": "Agent memory as system state"}
]

Return ONLY the JSON array, no markdown fences, no explanation."""


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Extract YAML frontmatter and body from markdown content."""
    if not content.startswith("---\n"):
        return {}, content
    idx = content.find("\n---\n", 4)
    if idx == -1:
        return {}, content
    parsed = {}
    for line in content[4:idx].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (
                v.startswith("'") and v.endswith("'")
            ):
                v = v[1:-1]
            parsed[k.strip().lower()] = v
    return parsed, content[idx + 5 :].lstrip("\n")


def load_published_slugs() -> set[str]:
    """Load the set of published post slugs from the cache posts.json."""
    cache_path = CACHE_DIR / "posts.json"
    if not cache_path.exists():
        return set()
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {
                p["slug"]
                for p in data
                if isinstance(p, dict) and p.get("slug") and p.get("published")
            }
    except Exception:
        pass
    return set()


def load_cache_metadata() -> dict[str, dict]:
    """Load post metadata from cache posts.json keyed by slug."""
    cache_path = CACHE_DIR / "posts.json"
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {p["slug"]: p for p in data if isinstance(p, dict) and p.get("slug")}
    except Exception:
        pass
    return {}


def load_posts(posts_dir: Path, max_posts: int | None = None) -> list[dict]:
    """Load published post markdown files and return structured data."""
    published_slugs = load_published_slugs()
    cache_meta = load_cache_metadata()
    posts = []
    for md_file in sorted(posts_dir.glob("*.md")):
        if md_file.name.endswith(".summary.md"):
            continue
        if md_file.name.endswith(".tweet.md"):
            continue
        if md_file.name.endswith(".linkedin.md"):
            continue
        if md_file.name.endswith(".share.md"):
            continue
        if md_file.name.endswith(".postscript.md"):
            continue

        slug = md_file.stem
        if slug in SKIP_SLUGS:
            continue
        if any(slug.endswith(suffix) for suffix in SKIP_SUFFIXES):
            continue

        if published_slugs and slug not in published_slugs:
            continue

        try:
            raw = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        frontmatter, body = parse_frontmatter(raw)

        cached = cache_meta.get(slug, {})
        title = (
            frontmatter.get("title")
            or cached.get("title")
            or slug.replace("-", " ").title()
        )
        published_date = (
            frontmatter.get("published_date") or cached.get("publishedDate") or ""
        )

        word_count = len(body.split())
        if word_count < 100:
            continue

        posts.append(
            {
                "slug": slug,
                "title": title,
                "body": body,
                "published_date": published_date,
            }
        )

    posts.sort(key=lambda p: p.get("published_date", ""), reverse=True)

    if max_posts:
        posts = posts[:max_posts]

    return posts


def extract_quotes_openai(title: str, body: str) -> list[dict]:
    """Use OpenAI to extract pithy quotes from a post."""
    try:
        from openai import OpenAI
    except ImportError:
        print("ERROR: openai package not installed. Run: pip install openai")
        sys.exit(1)

    client = OpenAI()

    user_msg = f"# {title}\n\n{body}"

    if len(user_msg) > 30000:
        user_msg = user_msg[:30000] + "\n\n[truncated]"

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=2000,
    )

    raw = response.choices[0].message.content.strip()

    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        quotes = json.loads(raw)
        if isinstance(quotes, list):
            return [q for q in quotes if isinstance(q, dict) and q.get("quote")]
    except json.JSONDecodeError:
        print("  WARNING: Failed to parse LLM response as JSON")
        return []

    return []


def generate_wisdom(
    posts_dir: Path, max_posts: int | None = None, dry_run: bool = False
) -> dict:
    """Main pipeline: load posts, extract quotes, build wisdom collection."""
    print(f"Loading posts from {posts_dir}...")
    posts = load_posts(posts_dir, max_posts)
    print(f"Found {len(posts)} eligible posts")

    wisdom_entries = []

    for i, post in enumerate(posts):
        slug = post["slug"]
        title = post["title"]
        print(f"\n[{i + 1}/{len(posts)}] {title} ({slug})")

        if dry_run:
            print("  (dry run — skipping LLM extraction)")
            wisdom_entries.append(
                {
                    "slug": slug,
                    "title": title,
                    "publishedDate": post.get("published_date", ""),
                    "quotes": [{"quote": "[dry run placeholder]", "context": "test"}],
                }
            )
            continue

        quotes = extract_quotes_openai(title, post["body"])
        print(f"  Extracted {len(quotes)} quotes")
        for q in quotes:
            print(
                f"    — \"{q['quote'][:80]}...\""
                if len(q["quote"]) > 80
                else f"    — \"{q['quote']}\""
            )

        if quotes:
            wisdom_entries.append(
                {
                    "slug": slug,
                    "title": title,
                    "publishedDate": post.get("published_date", ""),
                    "quotes": quotes,
                }
            )

    total_quotes = sum(len(e["quotes"]) for e in wisdom_entries)
    collection = {
        "generatedAt": datetime.now(UTC).isoformat(),
        "totalQuotes": total_quotes,
        "totalPosts": len(wisdom_entries),
        "entries": wisdom_entries,
    }

    return collection


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(
        description="PithBot: Extract pithy wisdom from blog posts"
    )
    parser.add_argument(
        "--posts-dir",
        type=Path,
        default=DEFAULT_POSTS_DIR,
        help=f"Posts directory (default: {DEFAULT_POSTS_DIR})",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=None,
        help="Maximum number of posts to process (for testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview posts without calling the LLM",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"Output JSON path (default: {WISDOM_JSON})",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("PithBot — Extracting wisdom from the writing")
    print("=" * 60)

    collection = generate_wisdom(args.posts_dir, args.max_posts, args.dry_run)

    output_path = args.output or WISDOM_JSON
    write_json(output_path, collection)
    print(f"\nWrote {output_path}")
    print(f"  {collection['totalQuotes']} quotes from {collection['totalPosts']} posts")

    write_json(
        API_WISDOM_JSON,
        {
            "url": f"{SITE_BASE}/api/wisdom.json",
            "wisdom": collection,
        },
    )
    print(f"  API: {API_WISDOM_JSON}")

    print("\nPithBot complete.")


if __name__ == "__main__":
    main()
