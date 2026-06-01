#!/usr/bin/env python3
"""
Import tweets from twitter_markymark_tweets.json as posts, but only for tweets
that don't already have an associated long-form post.

Tweet posts use:
- slug: inferred from tweet content (keyword extraction)
- body: tweet text (full content)
- excerpt: truncated tweet text
- category: "tweet"
- linked_tweet_url: tweet URL

Use --update-existing to update existing tweet posts with full text when the
JSON has longer content (e.g. after re-scraping with full tweet fetch).

Run generate_posts_cache.py after this to update the website cache.
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from dotenv import load_dotenv

env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    load_dotenv(env_file)

from parquet_client import ParquetMCPClient

TWEETS_JSON = PROJECT_ROOT / "data" / "tmp" / "twitter_markymark_tweets.json"

# Stopwords for slug inference (lowercase)
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "need",
        "to",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "i",
        "you",
        "your",
        "we",
        "our",
        "they",
        "their",
        "he",
        "him",
        "his",
        "she",
        "her",
        "my",
        "me",
        "so",
        "if",
        "when",
        "where",
        "how",
        "why",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "than",
        "too",
        "very",
        "just",
        "also",
        "already",
        "even",
        "now",
        "then",
        "there",
        "here",
    ]
)


def infer_slug_from_tweet(text: str, tweet_id: str, existing_slugs: set[str]) -> str:
    """Infer slug keywords from tweet content."""
    # Remove URLs
    text = re.sub(r"https?://\S+", "", text, flags=re.IGNORECASE)
    # Remove @mentions
    text = re.sub(r"@\w+", "", text)
    # Lowercase and extract words (alphanumeric + hyphen)
    words = re.findall(r"[a-z0-9]+", text.lower())
    # Filter stopwords and short words, take meaningful terms (no repetition)
    seen = set()
    meaningful = []
    for w in words:
        if w not in STOPWORDS and len(w) >= 2 and w not in seen:
            seen.add(w)
            meaningful.append(w)
    # Prefer longer/denser words (likely topic keywords)
    meaningful.sort(key=lambda w: (-len(w), w))
    # Take up to 5 keywords
    keywords = meaningful[:5]
    if not keywords:
        keywords = ["tweet"]
    slug_base = "-".join(keywords)
    # Ensure uniqueness
    slug = slug_base
    counter = 1
    while slug in existing_slugs:
        slug = f"{slug_base}-{counter}"
        counter += 1
    return slug


def main():
    parser = argparse.ArgumentParser(description="Import tweets as posts")
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Update existing tweet posts with full text when JSON has longer content",
    )
    args = parser.parse_args()

    if not TWEETS_JSON.exists():
        print(f"Tweets file not found: {TWEETS_JSON}")
        sys.exit(1)

    with open(TWEETS_JSON, encoding="utf-8") as f:
        data = json.load(f)

    tweets = data.get("tweets", [])
    if not tweets:
        print("No tweets in file.")
        sys.exit(0)

    client = ParquetMCPClient(
        parquet_server_path=str(
            PROJECT_ROOT / "mcp" / "parquet" / "parquet_mcp_server.py"
        )
    )

    # Build url -> tweet map for update logic
    url_to_tweet = {t.get("url", ""): t for t in tweets if t.get("url")}

    # Get existing posts with linked_tweet_url (long-form posts that link to tweets)
    result = client.call_tool_sync(
        "read_parquet",
        {
            "data_type": "posts",
            "columns": ["slug", "linked_tweet_url", "category", "body"],
        },
    )
    posts = result.get("data", [])
    linked_urls_with_longform = set()
    existing_slugs = set()
    for p in posts:
        url = p.get("linked_tweet_url")
        if url:
            linked_urls_with_longform.add(url.strip())
        slug = p.get("slug")
        if slug:
            existing_slugs.add(slug)

    # Update existing tweet posts when --update-existing:
    # 1) Set title="" and excerpt="" for all tweet posts
    # 2) When JSON has longer text, update body too
    if args.update_existing:
        tweet_posts = [p for p in posts if (p.get("category") or "").lower() == "tweet"]
        updated = 0
        for p in tweet_posts:
            url = (p.get("linked_tweet_url") or "").strip()
            if not url:
                continue
            updates = {"title": "", "excerpt": ""}
            if url in url_to_tweet:
                t = url_to_tweet[url]
                new_text = (t.get("text") or "").strip()
                if new_text and len(new_text) >= 10:
                    current_body = (p.get("body") or "").strip()
                    if len(new_text) > len(current_body):
                        updates["body"] = new_text
                # Update all tweet properties (hero_image, tweet_metadata)
                images = t.get("images") or []
                updates["hero_image"] = (
                    images[0] if images and isinstance(images[0], str) else ""
                )
                updates["tweet_metadata"] = json.dumps(
                    {
                        "likes": t.get("likes", 0),
                        "retweets": t.get("retweets", 0),
                        "replies": t.get("replies", 0),
                        "quote_count": t.get("quote_count", 0),
                        "bookmark_count": t.get("bookmark_count", 0),
                        "is_reply": t.get("is_reply", False),
                        "is_retweet": t.get("is_retweet", False),
                        "is_quote": t.get("is_quote", False),
                        "lang": t.get("lang", ""),
                        "author_name": t.get("author_name", ""),
                        "images": images,
                    }
                )
            # Always clear title/excerpt; optionally update body
            try:
                client.call_tool_sync(
                    "update_records",
                    {
                        "data_type": "posts",
                        "filters": {"linked_tweet_url": url},
                        "updates": updates,
                    },
                )
                updated += 1
                if "body" in updates:
                    print(
                        f"  Updated: {p.get('slug', url)} (body + cleared title/excerpt)"
                    )
                else:
                    print(f"  Cleared title/excerpt: {p.get('slug', url)}")
            except Exception as e:
                print(f"  ERROR updating {url}: {e}")
        if updated:
            print(f"\nUpdated {updated} tweet post(s).")

    # Filter: only markymark tweets with content, not already linked
    # URL must be from markymark (user's own tweets)
    to_import = []
    for t in tweets:
        url = t.get("url", "")
        text = (t.get("text") or "").strip()
        tweet_id = t.get("tweet_id", "")
        # Skip if no content
        if not text or len(text) < 10:
            continue
        # Skip if already has long-form post
        if url in linked_urls_with_longform:
            continue
        # Skip tweets from other accounts (retweets/quote tweets - url shows different user)
        if "/markymark/status/" not in url:
            continue
        to_import.append(t)

    if not to_import:
        print(
            "No tweets to import (all already have associated posts or filtered out)."
        )
        sys.exit(0)

    print(f"Importing {len(to_import)} tweet(s) as posts...")

    created = 0
    for t in to_import:
        url = t["url"]
        text = t["text"].strip()
        tweet_id = t.get("tweet_id", "")
        timestamp = t.get("timestamp", "")

        # Parse published date from timestamp (YYYY-MM-DD)
        published_date = timestamp[:10] if len(timestamp) >= 10 else None

        slug = infer_slug_from_tweet(text, tweet_id, existing_slugs)
        existing_slugs.add(slug)

        # First image as hero_image (posts schema has hero_image)
        images = t.get("images") or []
        hero_image = images[0] if images and isinstance(images[0], str) else None

        # Engagement and metadata as JSON for display/query
        tweet_metadata = {
            "likes": t.get("likes", 0),
            "retweets": t.get("retweets", 0),
            "replies": t.get("replies", 0),
            "quote_count": t.get("quote_count", 0),
            "bookmark_count": t.get("bookmark_count", 0),
            "is_reply": t.get("is_reply", False),
            "is_retweet": t.get("is_retweet", False),
            "is_quote": t.get("is_quote", False),
            "lang": t.get("lang", ""),
            "author_name": t.get("author_name", ""),
            "images": images,
        }

        record = {
            "slug": slug,
            "title": "",
            "excerpt": "",
            "body": text,
            "published": True,
            "published_date": published_date,
            "created_date": published_date,
            "updated_date": published_date,
            "category": "tweet",
            "linked_tweet_url": url,
            "read_time": 1,
            "tags": json.dumps(["tweet"]),
            "hero_image": hero_image or "",
            "tweet_metadata": json.dumps(tweet_metadata),
        }

        try:
            client.call_tool_sync(
                "add_record",
                {
                    "data_type": "posts",
                    "record": record,
                },
            )
            created += 1
            print(f"  Created: {slug} <- {url}")
        except Exception as e:
            print(f"  ERROR creating {slug}: {e}")

    print(
        f"\nCreated {created} tweet post(s). Run generate_posts_cache.py to update the website."
    )


if __name__ == "__main__":
    main()
