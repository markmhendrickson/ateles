#!/usr/bin/env python3
"""
Generate website cache JSON files from Neotoma export only.

Source of truth for website data (posts, links, timeline) is Neotoma. Export
posts/links/timeline from Neotoma MCP to a JSON file, then run with
--from-neotoma-json <path>. Default path: data/tmp/neotoma_website_export.json.

Generates:
- src/content/posts/posts.json, src/data/links.json, timeline.json (for app bundle)
- public/api/posts.json, public/api/links.json, public/api/timeline.json (for HTTP;
  each includes absolute url and data: e.g. {"url": "https://markmhendrickson.com/api/posts.json", "posts": [...]})

Run this script before building the website to ensure cache is up-to-date.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution" / "scripts"))

# Load environment variables from .env file
from dotenv import load_dotenv

env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    load_dotenv(env_file)

# Default Neotoma export path (used when --from-neotoma-json is omitted)
DEFAULT_NEOTOMA_EXPORT = PROJECT_ROOT / "data" / "tmp" / "neotoma_website_export.json"

# Paths - use absolute paths resolved from script location
REACT_APP_ROOT = (
    PROJECT_ROOT / "execution" / "website" / "markmhendrickson" / "react-app"
).resolve()
CACHE_DIR = REACT_APP_ROOT / "cache"
CACHE_API_DIR = CACHE_DIR / "api"

WEBSITE_POSTS_DIR = (REACT_APP_ROOT / "src" / "content" / "posts").resolve()
POSTS_JSON = CACHE_DIR / "posts.json"
POSTS_PRIVATE_JSON = CACHE_DIR / "posts.private.json"
LINKS_JSON = CACHE_DIR / "links.json"
TIMELINE_JSON = CACHE_DIR / "timeline.json"
API_POSTS_JSON = CACHE_API_DIR / "posts.json"
API_LINKS_JSON = CACHE_API_DIR / "links.json"
API_TIMELINE_JSON = CACHE_API_DIR / "timeline.json"
SUPPORTED_LOCALES = [
    "en",
    "es",
    "ca",
    "zh",
    "hi",
    "ar",
    "fr",
    "pt",
    "ru",
    "bn",
    "ur",
    "id",
    "de",
]

PUBLIC_POSTS_IMAGES = (REACT_APP_ROOT / "public" / "images" / "posts").resolve()
PUBLIC_OG_IMAGES_DIR = (REACT_APP_ROOT / "public" / "images" / "og").resolve()

# Absolute base URL for API JSON (all public JSON under /api/)
SITE_BASE = "https://markmhendrickson.com"


def convert_post_to_metadata(post, include_body=False, include_share_tweet=True):
    """Convert post record (from Neotoma export) to cache metadata format.
    Set include_share_tweet=False for public/prod cache (posts.json) so shareTweet is not exposed.
    """
    # Parse tags from JSON string
    tags = []
    if post.get("tags"):
        try:
            tags = json.loads(post["tags"])
        except (json.JSONDecodeError, TypeError):
            tags = []

    # Build metadata. Tweet posts are always unpublished (excluded from public listing).
    is_tweet = (post.get("category") or "").lower() == "tweet"
    metadata = {
        "slug": post.get("slug") or "",
        "title": post.get("title") or "",
        "excerpt": post.get("excerpt") or "",
        "published": False if is_tweet else post.get("published", False),
        "publishedDate": post.get("published_date") if not is_tweet else None,
        "category": post.get("category") or "",
        "readTime": post.get("read_time"),
        "tags": tags,
    }

    # Include body if requested (for dev mode)
    if include_body and post.get("body"):
        metadata["body"] = post["body"]

    # Add optional fields if present
    if post.get("hero_image"):
        metadata["heroImage"] = post["hero_image"]
    if post.get("hero_image_style"):
        metadata["heroImageStyle"] = post["hero_image_style"]
    # Square version for thumbnails (posts list, home, prev/next): use export or derive from hero
    if post.get("hero_image_square"):
        metadata["heroImageSquare"] = post["hero_image_square"]
    elif post.get("hero_image"):
        h = post["hero_image"]
        base, ext = h.rsplit(".", 1) if "." in h else (h, "")
        square_name = f"{base}-square.{ext}" if ext else f"{base}-square"
        if (PUBLIC_POSTS_IMAGES / square_name).exists():
            metadata["heroImageSquare"] = square_name
    if post.get("exclude_from_listing"):
        metadata["excludeFromListing"] = post["exclude_from_listing"]
    if "show_metadata" in post:
        metadata["showMetadata"] = post["show_metadata"]
    if post.get("created_date"):
        metadata["createdDate"] = post["created_date"]
    if post.get("updated_date"):
        metadata["updatedDate"] = post["updated_date"]
    if post.get("summary"):
        metadata["summary"] = post["summary"]
    if include_share_tweet and post.get("share_tweet"):
        metadata["shareTweet"] = post["share_tweet"]
    if post.get("og_image"):
        metadata["ogImage"] = post["og_image"]
    elif (PUBLIC_OG_IMAGES_DIR / f"{post['slug']}-1200x630.jpg").exists():
        metadata["ogImage"] = f"og/{post['slug']}-1200x630.jpg"
    if post.get("linked_tweet_url"):
        metadata["linkedTweetUrl"] = post["linked_tweet_url"]
    if post.get("x_timeline_url"):
        metadata["xTimelineUrl"] = post["x_timeline_url"]
    if post.get("tweet_metadata"):
        raw = post["tweet_metadata"]
        if isinstance(raw, str):
            try:
                metadata["tweetMetadata"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(raw, dict):
            metadata["tweetMetadata"] = raw

    # Alternative slugs (e.g. short / share-friendly URLs). Must be stored in Neotoma (export) for each post.
    alternative_slugs = []
    if post.get("alternative_slugs"):
        raw = post["alternative_slugs"]
        if isinstance(raw, list):
            alternative_slugs = raw
        elif isinstance(raw, str):
            try:
                alternative_slugs = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    if alternative_slugs:
        metadata["alternativeSlugs"] = alternative_slugs

    # Series fields (optional; may be absent in export but present in markdown frontmatter)
    if post.get("series"):
        metadata["series"] = post["series"]
    if post.get("series_slug") or post.get("seriesSlug"):
        metadata["seriesSlug"] = post.get("series_slug") or post.get("seriesSlug")
    if post.get("series_part") is not None or post.get("seriesPart") is not None:
        part = post.get("series_part")
        if part is None:
            part = post.get("seriesPart")
        coerced = _coerce_int(part)
        if coerced is not None:
            metadata["seriesPart"] = coerced
    if post.get("series_total") is not None or post.get("seriesTotal") is not None:
        total = post.get("series_total")
        if total is None:
            total = post.get("seriesTotal")
        coerced = _coerce_int(total)
        if coerced is not None:
            metadata["seriesTotal"] = coerced

    return metadata


def write_json(path: Path, payload: list | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def locale_posts_json(locale: str) -> Path:
    return CACHE_DIR / f"posts.{locale}.json"


def locale_api_posts_json(locale: str) -> Path:
    return CACHE_API_DIR / f"posts.{locale}.json"


GLOSSARY_PATH = WEBSITE_POSTS_DIR / "translation_glossary.json"


def _load_glossary() -> dict:
    """Load the translation glossary for forbidden-sense validation."""
    if not GLOSSARY_PATH.exists():
        return {}
    try:
        return json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: Failed to load glossary {GLOSSARY_PATH}: {exc}")
        return {}


def validate_glossary_senses(localized_posts: dict[str, list[dict]]) -> list[str]:
    """Check locale post caches for forbidden-sense translations in headings.

    Returns a list of warning strings. When I18N_GLOSSARY_STRICT=1,
    raises RuntimeError on any violation.
    """
    glossary = _load_glossary()
    forbidden = glossary.get("forbidden_senses", {})
    if not forbidden:
        return []

    warnings: list[str] = []
    for locale, posts in localized_posts.items():
        for post in posts:
            slug = post.get("canonicalSlug") or post.get("slug") or ""
            for field in ("title", "body"):
                text = post.get(field) or ""
                if not text:
                    continue
                heading_lines = [
                    ln for ln in text.split("\n") if ln.lstrip().startswith("#")
                ]
                for hl in heading_lines:
                    for en_term, locale_map in forbidden.items():
                        if not isinstance(locale_map, dict) or locale not in locale_map:
                            continue
                        bad_list = sorted(locale_map[locale], key=len, reverse=True)
                        for bad in bad_list:
                            pattern = re.compile(
                                r"\b" + re.escape(bad) + r"\b", re.IGNORECASE
                            )
                            if pattern.search(hl):
                                warnings.append(
                                    f"[{locale}] '{en_term}' wrong-sense '{bad}' "
                                    f"in heading of {slug}/{field}: {hl.strip()[:80]}"
                                )

    if warnings:
        print(f"\nGlossary sense validation ({len(warnings)} issue(s)):")
        for w in warnings:
            print(f"  WARNING: {w}")
        if os.getenv("I18N_GLOSSARY_STRICT") == "1":
            raise RuntimeError(
                "Glossary sense validation failed. Fix translations or update glossary. "
                + "; ".join(warnings[:5])
            )
    return warnings


def fix_translation_markdown(text: str) -> str:
    """Repair machine-translation artifacts that break Markdown links, e.g. '] (' -> ']('."""
    if not text or not isinstance(text, str):
        return text
    return re.sub(r"]\s+\(", "](", text)


def apply_glossary_heading_corrections(text: str, locale: str) -> str:
    """Replace wrong-sense phrases inside markdown headings only (lines starting with #).

    Body paragraphs are unchanged so seasonal phrases like \"La tardor passada\" stay correct.
    """
    if locale == "en" or not text or not isinstance(text, str):
        return text
    glossary = _load_glossary()
    forbidden = glossary.get("forbidden_senses", {})
    overrides = glossary.get("heading_overrides", {})
    if not forbidden or not overrides:
        return text

    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            out.append(line)
            continue
        fixed = line
        for en_term, locale_map in forbidden.items():
            if not isinstance(locale_map, dict) or locale not in locale_map:
                continue
            repl_map = overrides.get(en_term)
            if not isinstance(repl_map, dict) or locale not in repl_map:
                continue
            replacement = repl_map[locale]
            for bad in sorted(locale_map[locale], key=len, reverse=True):
                pattern = re.compile(r"\b" + re.escape(bad) + r"\b", re.IGNORECASE)
                fixed = pattern.sub(replacement, fixed)
        out.append(fixed)
    return "\n".join(out)


def load_locale_overrides(locale: str) -> dict[str, dict]:
    """Load per-locale post field overrides from src/content/posts/translations.<locale>.json.
    Shape: { "<slug>": {"title": "...", "excerpt": "...", "summary": "...", "body": "...", "postscript": "...", "slug": "..."} }
    """
    path = WEBSITE_POSTS_DIR / f"translations.{locale}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"WARNING: Failed to parse {path}: {e}")
        return {}


def build_locale_posts(base_posts: list[dict], locale: str) -> list[dict]:
    """Create locale-specific posts cache by overlaying optional translation overrides."""
    overrides = load_locale_overrides(locale)
    localized: list[dict] = []
    for post in base_posts:
        slug = post.get("slug", "")
        override = overrides.get(slug, {})
        entry = dict(post)
        entry["postId"] = slug
        entry["locale"] = locale
        entry["canonicalSlug"] = slug
        # Allow locale-specific slug overrides while preserving canonical identity.
        if isinstance(override, dict):
            if override.get("slug"):
                entry["slug"] = override.get("slug")
            if isinstance(override.get("alternativeSlugs"), list):
                entry["alternativeSlugs"] = override.get("alternativeSlugs")
            for field in (
                "title",
                "excerpt",
                "summary",
                "body",
                "postscript",
                "shareDescription",
                "series",
                "seriesDescription",
            ):
                if override.get(field):
                    entry[field] = fix_translation_markdown(str(override.get(field)))
        # Guarantee required locale fields for validation and SEO metadata.
        if not (entry.get("title") or "").strip():
            entry["title"] = slug.replace("-", " ").title()
        if not (entry.get("excerpt") or "").strip():
            entry["excerpt"] = str(entry.get("summary") or "").strip()
        if not (entry.get("summary") or "").strip():
            fallback_summary = str(entry.get("excerpt") or "").strip()
            if not fallback_summary:
                fallback_summary = str(entry.get("body") or "").strip()[:280]
            entry["summary"] = fallback_summary
        if not (entry.get("body") or "").strip():
            entry["body"] = str(entry.get("summary") or "").strip()
        if locale != "en":
            for field in ("title", "body"):
                raw = entry.get(field)
                if isinstance(raw, str) and raw.strip():
                    entry[field] = fix_translation_markdown(
                        apply_glossary_heading_corrections(raw, locale)
                    )
        localized.append(entry)
    return localized


def validate_initial_translation_batch(
    published_metadata: list[dict], locales: list[str]
) -> None:
    """Ensure current-year published posts are present in locale override maps.
    Set I18N_BATCH_STRICT=1 to fail build when missing.
    """
    current_year = str(datetime.utcnow().year)
    current_year_slugs = {
        p.get("slug")
        for p in published_metadata
        if (p.get("publishedDate") or "").startswith(current_year)
    }
    current_year_slugs.discard(None)
    missing: dict[str, list[str]] = {}
    for locale in locales:
        overrides = load_locale_overrides(locale)
        locale_missing = [
            slug for slug in sorted(current_year_slugs) if slug not in overrides
        ]
        if locale_missing:
            missing[locale] = locale_missing
    if not missing:
        return
    print(
        "WARNING: Missing translation override entries for current-year published posts:"
    )
    for locale, slugs in missing.items():
        print(f"  {locale}: {', '.join(slugs)}")
    import os

    if os.getenv("I18N_BATCH_STRICT") == "1":
        raise RuntimeError(
            "Initial translation batch incomplete. Add entries to translations.<locale>.json files."
        )


def report_translation_coverage(
    published_metadata: list[dict], locales: list[str]
) -> None:
    """Print translation coverage for published posts by locale.
    Reports missing translation entries and missing key fields per locale.
    Set I18N_TRANSLATION_COVERAGE_STRICT=1 to fail on missing locale entries.
    """
    published_slugs = sorted(
        {p.get("slug") for p in published_metadata if p.get("slug")}
    )
    if not published_slugs:
        return

    strict = os.getenv("I18N_TRANSLATION_COVERAGE_STRICT") == "1"
    strict_failures: list[str] = []

    print("\nTranslation coverage (published posts):")
    for locale in locales:
        overrides = load_locale_overrides(locale)
        missing_entries = [slug for slug in published_slugs if slug not in overrides]
        missing_title = []
        missing_excerpt = []
        missing_summary = []
        for slug in published_slugs:
            entry = overrides.get(slug, {})
            if not isinstance(entry, dict):
                missing_title.append(slug)
                missing_excerpt.append(slug)
                missing_summary.append(slug)
                continue
            if not (entry.get("title") or "").strip():
                missing_title.append(slug)
            if not (entry.get("excerpt") or "").strip():
                missing_excerpt.append(slug)
            if not (entry.get("summary") or "").strip():
                missing_summary.append(slug)

        translated_count = len(published_slugs) - len(missing_entries)
        print(
            f"  {locale}: {translated_count}/{len(published_slugs)} entries, "
            f"missing entries={len(missing_entries)}, missing title={len(missing_title)}, "
            f"missing excerpt={len(missing_excerpt)}, missing summary={len(missing_summary)}"
        )
        if missing_entries:
            preview = ", ".join(missing_entries[:10])
            print(
                f"    missing slugs: {preview}{' ...' if len(missing_entries) > 10 else ''}"
            )
        if strict and missing_entries:
            strict_failures.append(
                f"{locale} missing translation entries: {', '.join(missing_entries[:20])}"
            )

    if strict_failures:
        raise RuntimeError(
            "Translation coverage strict mode failed. " + " | ".join(strict_failures)
        )


def list_markdown_files():
    """Collect markdown files from posts and drafts directories."""
    markdown_files = []
    if WEBSITE_POSTS_DIR.exists():
        markdown_files.extend(
            path
            for path in WEBSITE_POSTS_DIR.glob("*.md")
            if path.name != "README.md"
            and not path.name.endswith(".summary.md")
            and not path.name.endswith(".tweet.md")
            and not path.name.endswith(".postscript.md")
        )
    drafts_dir = WEBSITE_POSTS_DIR / "drafts"
    if drafts_dir.exists():
        markdown_files.extend(
            p
            for p in drafts_dir.glob("*.md")
            if not p.name.endswith(".summary.md")
            and not p.name.endswith(".tweet.md")
            and not p.name.endswith(".postscript.md")
        )
    return markdown_files


def draft_slugs_from_markdown() -> set[str]:
    """Return set of slugs that have a draft body .md file (drafts/*.md)."""
    drafts_dir = WEBSITE_POSTS_DIR / "drafts"
    if not drafts_dir.exists():
        return set()
    return {
        p.stem
        for p in drafts_dir.glob("*.md")
        if not p.name.endswith(".summary.md")
        and not p.name.endswith(".tweet.md")
        and not p.name.endswith(".postscript.md")
    }


def _parse_draft_frontmatter(content: str) -> tuple[dict, str]:
    """If content starts with YAML frontmatter (---\\n...\\n---\\n), return (parsed dict, body).
    Otherwise return ({}, content). Parses simple key: value lines (values may be quoted).
    """
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


def _parse_frontmatter_tags(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except (json.JSONDecodeError, TypeError):
                # Fall through: simple frontmatter parsers may leave tags as a bracketed string.
                inner = s[1:-1].strip()
                if not inner:
                    return []
                parts = [p.strip().strip('"').strip("'") for p in inner.split(",")]
                return [p for p in parts if p]
        return [s]
    return []


def _coerce_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def overlay_series_fields_from_frontmatter(frontmatter: dict, meta: dict) -> None:
    """Merge series-related fields from markdown frontmatter into cache metadata."""
    series = (frontmatter.get("series") or "").strip()
    if series:
        meta["series"] = series

    series_slug = (
        frontmatter.get("series_slug") or frontmatter.get("seriesslug") or ""
    ).strip() or (frontmatter.get("seriesSlug") or "").strip()
    if series_slug:
        meta["seriesSlug"] = series_slug

    part = _coerce_int(
        frontmatter.get("series_part")
        or frontmatter.get("seriespart")
        or frontmatter.get("seriesPart")
    )
    if part is not None:
        meta["seriesPart"] = part

    total = _coerce_int(
        frontmatter.get("series_total")
        or frontmatter.get("seriestotal")
        or frontmatter.get("seriesTotal")
    )
    if total is not None:
        meta["seriesTotal"] = total


CONTENT_MANIFEST_JSON = WEBSITE_POSTS_DIR / "posts.json"


def metadata_for_content_only_drafts(export_slugs: set[str]) -> list[dict]:
    """Drafts that live in content/posts (not drafts/) and are in content posts.json but not in the export."""
    if not CONTENT_MANIFEST_JSON.exists():
        return []
    try:
        raw = CONTENT_MANIFEST_JSON.read_text(encoding="utf-8")
        manifest = json.loads(raw)
    except Exception:
        return []
    if not isinstance(manifest, list):
        return []
    out = []
    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not slug or slug in export_slugs:
            continue
        if entry.get("published", False):
            continue
        body_path = WEBSITE_POSTS_DIR / f"{slug}.md"
        if not body_path.exists():
            continue
        try:
            raw_body = body_path.read_text(encoding="utf-8")
        except Exception:
            continue
        frontmatter, body = _parse_draft_frontmatter(raw_body)
        title = (frontmatter.get("title") or entry.get("title") or "").strip()
        excerpt = (frontmatter.get("excerpt") or entry.get("excerpt") or "").strip()
        if not title:
            for line in body.splitlines():
                s = line.strip()
                if not s:
                    continue
                if s.startswith("## "):
                    title = s[3:].strip()
                else:
                    title = s[:80] if len(s) > 80 else s
                break
        if not title:
            title = slug.replace("-", " ").title()
        summary_path = WEBSITE_POSTS_DIR / f"{slug}.summary.md"
        summary = (
            summary_path.read_text(encoding="utf-8").strip()
            if summary_path.exists()
            else ""
        )
        tweet_path = WEBSITE_POSTS_DIR / "drafts" / f"{slug}.tweet.md"
        share_tweet = (
            tweet_path.read_text(encoding="utf-8").strip()
            if tweet_path.exists()
            else ""
        )
        try:
            mtime = body_path.stat().st_mtime
            updated = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        except Exception:
            updated = entry.get("updatedDate") or entry.get("createdDate") or ""
        meta = {
            "slug": slug,
            "title": title,
            "excerpt": excerpt,
            "body": body,
            "published": False,
            "publishedDate": None,
            "category": entry.get("category") or "essay",
            "readTime": entry.get("readTime"),
            "tags": entry.get("tags") or [],
            "createdDate": entry.get("createdDate") or updated,
            "updatedDate": entry.get("updatedDate") or updated,
            "summary": summary,
            "shareTweet": share_tweet,
        }
        hero_path = PUBLIC_POSTS_IMAGES / f"{slug}-hero.png"
        if hero_path.exists():
            meta["heroImage"] = f"{slug}-hero.png"
            style_path = PUBLIC_POSTS_IMAGES / f"{slug}-hero-style.txt"
            meta["heroImageStyle"] = (
                (style_path.read_text(encoding="utf-8").strip() or "keep-proportions")
                if style_path.exists()
                else "keep-proportions"
            )
            square_path = PUBLIC_POSTS_IMAGES / f"{slug}-hero-square.png"
            if square_path.exists():
                meta["heroImageSquare"] = f"{slug}-hero-square.png"
        if entry.get("alternativeSlugs"):
            meta["alternativeSlugs"] = entry["alternativeSlugs"]
        out.append(meta)
    return out


def metadata_for_content_only_published(existing_public_slugs: set[str]) -> list[dict]:
    """Published posts from content/posts/posts.json that are not already in the public listing.
    This allows content manifest entries to surface posts whose export record may still be draft.
    """
    if not CONTENT_MANIFEST_JSON.exists():
        return []
    try:
        raw = CONTENT_MANIFEST_JSON.read_text(encoding="utf-8")
        manifest = json.loads(raw)
    except Exception:
        return []
    if not isinstance(manifest, list):
        return []
    out = []
    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not slug or slug in existing_public_slugs:
            continue
        if not entry.get("published", False):
            continue
        body_path = WEBSITE_POSTS_DIR / f"{slug}.md"
        if not body_path.exists():
            body_path = WEBSITE_POSTS_DIR / "drafts" / f"{slug}.md"
        if not body_path.exists():
            continue
        try:
            raw_body = body_path.read_text(encoding="utf-8")
        except Exception:
            continue
        frontmatter, body = _parse_draft_frontmatter(raw_body)
        title = (frontmatter.get("title") or entry.get("title") or "").strip()
        excerpt = (frontmatter.get("excerpt") or entry.get("excerpt") or "").strip()
        if not title:
            for line in body.splitlines():
                s = line.strip()
                if not s:
                    continue
                if s.startswith("## "):
                    title = s[3:].strip()
                else:
                    title = s[:80] if len(s) > 80 else s
                break
        if not title:
            title = slug.replace("-", " ").title()
        summary_path = WEBSITE_POSTS_DIR / f"{slug}.summary.md"
        if not summary_path.exists():
            summary_path = WEBSITE_POSTS_DIR / "drafts" / f"{slug}.summary.md"
        summary = (
            summary_path.read_text(encoding="utf-8").strip()
            if summary_path.exists()
            else ""
        )
        tweet_path = WEBSITE_POSTS_DIR / f"{slug}.tweet.md"
        if not tweet_path.exists():
            tweet_path = WEBSITE_POSTS_DIR / "drafts" / f"{slug}.tweet.md"
        share_tweet = (
            tweet_path.read_text(encoding="utf-8").strip()
            if tweet_path.exists()
            else ""
        )
        try:
            mtime = body_path.stat().st_mtime
            updated = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        except Exception:
            updated = entry.get("updatedDate") or entry.get("createdDate") or ""
        meta = {
            "slug": slug,
            "title": title,
            "excerpt": excerpt,
            "body": body,
            "published": True,
            "publishedDate": entry.get("publishedDate")
            or entry.get("published_date")
            or updated,
            "category": entry.get("category") or "essay",
            "readTime": entry.get("readTime"),
            "tags": entry.get("tags") or [],
            "createdDate": entry.get("createdDate") or updated,
            "updatedDate": entry.get("updatedDate") or updated,
            "summary": summary,
            "shareTweet": share_tweet,
        }
        hero_path = PUBLIC_POSTS_IMAGES / f"{slug}-hero.png"
        if hero_path.exists():
            meta["heroImage"] = f"{slug}-hero.png"
            style_path = PUBLIC_POSTS_IMAGES / f"{slug}-hero-style.txt"
            meta["heroImageStyle"] = (
                (style_path.read_text(encoding="utf-8").strip() or "keep-proportions")
                if style_path.exists()
                else "keep-proportions"
            )
        square_path = PUBLIC_POSTS_IMAGES / f"{slug}-hero-square.png"
        if square_path.exists():
            meta["heroImageSquare"] = f"{slug}-hero-square.png"
        if entry.get("heroImage"):
            meta["heroImage"] = entry["heroImage"]
        if entry.get("heroImageStyle"):
            meta["heroImageStyle"] = entry["heroImageStyle"]
        if entry.get("ogImage"):
            meta["ogImage"] = entry["ogImage"]
        elif (PUBLIC_OG_IMAGES_DIR / f"{slug}-1200x630.jpg").exists():
            meta["ogImage"] = f"og/{slug}-1200x630.jpg"
        out.append(meta)
    return out


def metadata_for_draft_only_slugs(
    export_slugs: set[str],
) -> list[dict]:
    """Build cache metadata for drafts that exist only as markdown (not in Neotoma export).
    So draft posts are viewable in dev without re-exporting from Neotoma.
    Supports optional YAML frontmatter (title, excerpt) at top of draft .md; body is rest of file.
    """
    draft_slugs = draft_slugs_from_markdown() - export_slugs
    if not draft_slugs:
        return []
    drafts_dir = WEBSITE_POSTS_DIR / "drafts"
    out = []
    for slug in sorted(draft_slugs):
        body_path = drafts_dir / f"{slug}.md"
        if not body_path.exists():
            continue
        try:
            raw = body_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"WARNING: Failed to read draft body {body_path}: {e}")
            continue
        frontmatter, body = _parse_draft_frontmatter(raw)
        title = (frontmatter.get("title") or "").strip()
        excerpt = (frontmatter.get("excerpt") or "").strip()
        # Fallback: derive title from first ## heading or first non-empty line
        if not title:
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("## "):
                    title = line[3:].strip()
                    break
                title = line[:80] if len(line) > 80 else line
                break
        if not title:
            title = slug.replace("-", " ").title()
        summary_path = drafts_dir / f"{slug}.summary.md"
        summary = ""
        if summary_path.exists():
            try:
                summary = summary_path.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        tweet_path = drafts_dir / f"{slug}.tweet.md"
        share_tweet = ""
        if tweet_path.exists():
            try:
                share_tweet = tweet_path.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        # Use file mtime for updated_date so drafts sort sensibly
        try:
            mtime = body_path.stat().st_mtime
            updated = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        except Exception:
            updated = ""
        # Allow published/published_date from frontmatter for draft-only posts
        pub = frontmatter.get("published")
        published = (
            str(pub).lower() in ("true", "1", "yes") if pub is not None else False
        )
        pub_date = (
            frontmatter.get("published_date") or frontmatter.get("publisheddate") or ""
        ).strip()
        meta = {
            "slug": slug,
            "title": title,
            "excerpt": excerpt,
            "body": body,
            "published": published,
            "publishedDate": pub_date or None,
            "category": (frontmatter.get("category") or "essay").strip() or "essay",
            "readTime": _coerce_int(
                frontmatter.get("read_time")
                or frontmatter.get("readtime")
                or frontmatter.get("readTime")
            ),
            "tags": _parse_frontmatter_tags(frontmatter.get("tags")),
            "createdDate": updated or None,
            "updatedDate": updated or None,
            "summary": summary,
            "shareTweet": share_tweet,
        }
        overlay_series_fields_from_frontmatter(frontmatter, meta)
        # Add hero image for drafts when files exist under public/images/posts
        hero_path = PUBLIC_POSTS_IMAGES / f"{slug}-hero.png"
        if hero_path.exists():
            meta["heroImage"] = f"{slug}-hero.png"
            style_path = PUBLIC_POSTS_IMAGES / f"{slug}-hero-style.txt"
            if style_path.exists():
                try:
                    meta["heroImageStyle"] = (
                        style_path.read_text(encoding="utf-8").strip()
                        or "keep-proportions"
                    )
                except Exception:
                    meta["heroImageStyle"] = "keep-proportions"
            square_path = PUBLIC_POSTS_IMAGES / f"{slug}-hero-square.png"
            if square_path.exists():
                meta["heroImageSquare"] = f"{slug}-hero-square.png"
        out.append(meta)
    return out


def body_md_path(slug: str, published: bool) -> Path:
    """Path to main post markdown (editable .md)."""
    if published:
        return WEBSITE_POSTS_DIR / f"{slug}.md"
    return WEBSITE_POSTS_DIR / "drafts" / f"{slug}.md"


def summary_md_path(slug: str, published: bool) -> Path:
    """Path to key takeaways file for a post (editable .summary.md)."""
    if published:
        return WEBSITE_POSTS_DIR / f"{slug}.summary.md"
    return WEBSITE_POSTS_DIR / "drafts" / f"{slug}.summary.md"


def tweet_md_path(slug: str, published: bool) -> Path:
    """Path to share tweet draft for a post (editable .tweet.md)."""
    if published:
        return WEBSITE_POSTS_DIR / f"{slug}.tweet.md"
    return WEBSITE_POSTS_DIR / "drafts" / f"{slug}.tweet.md"


def pull_summaries_to_markdown(posts: list) -> int:
    """Write summary from export to {slug}.summary.md when file is missing (pull into md for editing)."""
    drafts_dir = WEBSITE_POSTS_DIR / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    for post in posts:
        slug = post.get("slug")
        summary = post.get("summary") or ""
        if not slug or not summary.strip():
            continue
        published = post.get("published", False)
        path = summary_md_path(slug, published)
        if path.exists():
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(summary.strip() + "\n", encoding="utf-8")
            created += 1
        except Exception as e:
            print(f"WARNING: Failed to write {path}: {e}")
    if created:
        print(
            f"Pulled {created} key takeaways file(s) from export into .summary.md for editing."
        )
    return created


LISTING_OVERRIDES_JSON = WEBSITE_POSTS_DIR / "listing_overrides.json"
ALTERNATIVE_SLUGS_JSON = WEBSITE_POSTS_DIR / "alternative_slugs.json"


def load_listing_overrides() -> list[str]:
    """Load slug list for exclude_from_listing from listing_overrides.json if present."""
    if not LISTING_OVERRIDES_JSON.exists():
        return []
    try:
        with open(LISTING_OVERRIDES_JSON, encoding="utf-8") as f:
            data = json.load(f)
        return list(data.get("exclude_from_listing") or [])
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: Failed to read {LISTING_OVERRIDES_JSON}: {e}")
        return []


def overlay_listing_excludes(metadata_list: list) -> None:
    """Set excludeFromListing on posts whose slugs are in listing_overrides.json."""
    exclude_slugs = set(load_listing_overrides())
    if not exclude_slugs:
        return
    for meta in metadata_list:
        if meta.get("slug") in exclude_slugs:
            meta["excludeFromListing"] = True


def load_alternative_slugs() -> dict[str, list[str]]:
    """Load slug -> list of alternative slugs from alternative_slugs.json if present."""
    if not ALTERNATIVE_SLUGS_JSON.exists():
        return {}
    try:
        with open(ALTERNATIVE_SLUGS_JSON, encoding="utf-8") as f:
            data = json.load(f)
        return {k: list(v) if isinstance(v, list) else [] for k, v in data.items()}
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: Failed to read {ALTERNATIVE_SLUGS_JSON}: {e}")
        return {}


def overlay_alternative_slugs(metadata_list: list) -> None:
    """Merge alternative slugs from alternative_slugs.json into posts (for redirects / old URLs)."""
    overrides = load_alternative_slugs()
    if not overrides:
        return
    for meta in metadata_list:
        slug = meta.get("slug")
        if not slug or slug not in overrides:
            continue
        existing = list(meta.get("alternativeSlugs") or [])
        meta["alternativeSlugs"] = existing + [
            a for a in overrides[slug] if a not in existing
        ]


def load_content_manifest_by_slug():
    """Load content/posts/posts.json as slug -> entry for manifest overlays."""
    if not CONTENT_MANIFEST_JSON.exists():
        return {}
    try:
        raw = CONTENT_MANIFEST_JSON.read_text(encoding="utf-8")
        manifest = json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"WARNING: Failed to read manifest for overlay {CONTENT_MANIFEST_JSON}: {e}"
        )
        return {}
    if not isinstance(manifest, list):
        return {}
    out = {}
    for entry in manifest:
        if isinstance(entry, dict) and entry.get("slug"):
            out[entry["slug"]] = entry
    return out


def overlay_content_manifest_fields(metadata_list: list) -> None:
    """Fill listing fields from content/posts/posts.json when Neotoma export left them empty."""
    by_slug = load_content_manifest_by_slug()
    if not by_slug:
        return
    for meta in metadata_list:
        slug = meta.get("slug")
        if not slug or slug not in by_slug:
            continue
        entry = by_slug[slug]
        is_draft = not meta.get("published")
        # Drafts: manifest is the editorial source for listing fields (Neotoma export can
        # carry a stale or wrong title while body is edited in drafts/*.md).
        if entry.get("title"):
            if is_draft or not str(meta.get("title") or "").strip():
                meta["title"] = entry["title"]
        ex = str(meta.get("excerpt") or "").strip()
        if entry.get("excerpt"):
            if is_draft or (not ex or ex in (">-", ">", "|")):
                meta["excerpt"] = entry["excerpt"]
        if entry.get("category") and not str(meta.get("category") or "").strip():
            meta["category"] = entry["category"]
        if entry.get("readTime") is not None and meta.get("readTime") in (None, ""):
            meta["readTime"] = entry["readTime"]
        if entry.get("tags") and not (meta.get("tags") or []):
            meta["tags"] = entry["tags"]
        if entry.get("publishedDate") and not meta.get("publishedDate"):
            meta["publishedDate"] = entry["publishedDate"]
        if entry.get("createdDate") and not meta.get("createdDate"):
            meta["createdDate"] = entry["createdDate"]
        if entry.get("updatedDate") and not meta.get("updatedDate"):
            meta["updatedDate"] = entry["updatedDate"]
        if entry.get("heroImage") and not meta.get("heroImage"):
            meta["heroImage"] = entry["heroImage"]
        if entry.get("heroImageStyle") and not meta.get("heroImageStyle"):
            meta["heroImageStyle"] = entry["heroImageStyle"]
        if entry.get("heroImageSquare") and not meta.get("heroImageSquare"):
            meta["heroImageSquare"] = entry["heroImageSquare"]
        if entry.get("ogImage") and not meta.get("ogImage"):
            meta["ogImage"] = entry["ogImage"]


def overlay_missing_hero_from_public_files(metadata_list: list) -> None:
    """When Neotoma export dropped hero_* but repo still has hero assets, restore listing fields.

    Without this, cache regeneration clears heroImage / heroImageSquare and the Posts list
    hides thumbnails (Posts.tsx only renders the image block when those keys are set).
    """
    for meta in metadata_list:
        slug = meta.get("slug")
        if not slug or meta.get("heroImage"):
            continue
        hero_path = PUBLIC_POSTS_IMAGES / f"{slug}-hero.png"
        if not hero_path.exists():
            continue
        meta["heroImage"] = f"{slug}-hero.png"
        style_path = PUBLIC_POSTS_IMAGES / f"{slug}-hero-style.txt"
        meta["heroImageStyle"] = (
            (style_path.read_text(encoding="utf-8").strip() or "keep-proportions")
            if style_path.exists()
            else "keep-proportions"
        )
        square_path = PUBLIC_POSTS_IMAGES / f"{slug}-hero-square.png"
        if square_path.exists():
            meta["heroImageSquare"] = f"{slug}-hero-square.png"
        if (
            not meta.get("ogImage")
            and (PUBLIC_OG_IMAGES_DIR / f"{slug}-1200x630.jpg").exists()
        ):
            meta["ogImage"] = f"og/{slug}-1200x630.jpg"


def overlay_summaries_from_markdown(metadata_list: list) -> None:
    """Override each post's summary with content from {slug}.summary.md when the file exists.
    Ensures the cache uses the editable markdown (e.g. all 5 bullets) instead of a truncated export value.
    Tries draft path when published path is missing (so published posts edited in drafts/ are updated).
    """
    for meta in metadata_list:
        slug = meta.get("slug")
        if not slug:
            continue
        published = meta.get("published", False)
        path = summary_md_path(slug, published)
        if not path.exists() and published:
            path = summary_md_path(slug, False)
        if not path.exists() and not published:
            path = summary_md_path(slug, True)
        if path.exists():
            try:
                meta["summary"] = path.read_text(encoding="utf-8").strip()
            except Exception as e:
                print(f"WARNING: Failed to read {path}: {e}")


def _body_without_frontmatter(content: str) -> str:
    """If content starts with YAML frontmatter (---\\n...\\n---\\n), return only the body after it."""
    _, body = _parse_draft_frontmatter(content)
    return body


def overlay_body_from_markdown(metadata_list: list) -> None:
    """Set body from {slug}.md when the file exists and post has no body (or to override export).
    Enables full-text search on the Posts page using cache body.
    Strips optional YAML frontmatter so it is not rendered as post content.
    Tries draft path when published path is missing (so published posts edited in drafts/ are updated).
    When loading from markdown, also overlays title and excerpt from frontmatter so cache stays in sync.
    """
    for meta in metadata_list:
        slug = meta.get("slug")
        if not slug:
            continue
        published = meta.get("published", True)
        path = body_md_path(slug, published)
        if not path.exists() and published:
            path = body_md_path(slug, False)
        if not path.exists() and not published:
            path = body_md_path(slug, True)
        if path.exists():
            try:
                raw = path.read_text(encoding="utf-8")
                frontmatter, body = _parse_draft_frontmatter(raw)
                meta["body"] = body
                if frontmatter.get("title"):
                    meta["title"] = frontmatter["title"].strip()
                if frontmatter.get("excerpt"):
                    meta["excerpt"] = frontmatter["excerpt"].strip()
                overlay_series_fields_from_frontmatter(frontmatter, meta)
                fm_pub_date = (
                    frontmatter.get("publishedDate")
                    or frontmatter.get("published_date")
                    or ""
                )
                if isinstance(fm_pub_date, str):
                    fm_pub_date = fm_pub_date.strip()
                existing_pub = meta.get("publishedDate")
                if fm_pub_date and (
                    not existing_pub
                    or str(existing_pub).strip().lower() in ("", "null", "none")
                ):
                    meta["publishedDate"] = fm_pub_date
                read_time = _coerce_int(
                    frontmatter.get("read_time")
                    or frontmatter.get("readtime")
                    or frontmatter.get("readTime")
                )
                if read_time is not None and meta.get("readTime") in (None, ""):
                    meta["readTime"] = read_time
                tags = _parse_frontmatter_tags(frontmatter.get("tags"))
                if tags and not (meta.get("tags") or []):
                    meta["tags"] = tags
                cat = (frontmatter.get("category") or "").strip()
                if cat and not str(meta.get("category") or "").strip():
                    meta["category"] = cat

                # Hero assets: slug may differ from on-disk filenames (e.g. title-based slug with
                # legacy part-N hero PNGs). Prefer explicit frontmatter paths when export omitted them.
                def _fm_image_str(*keys: str) -> str:
                    # _parse_draft_frontmatter lowercases YAML keys.
                    for k in keys:
                        for variant in (k, k.lower(), k.replace("_", "")):
                            v = frontmatter.get(variant)
                            if isinstance(v, str) and v.strip():
                                return v.strip()
                    return ""

                hi = _fm_image_str("heroImage", "hero_image")
                if hi and not meta.get("heroImage"):
                    meta["heroImage"] = hi
                hs = _fm_image_str("heroImageSquare", "hero_image_square")
                if hs and not meta.get("heroImageSquare"):
                    meta["heroImageSquare"] = hs
                og = _fm_image_str("ogImage", "og_image")
                if og and not meta.get("ogImage"):
                    meta["ogImage"] = og
                hstyle = _fm_image_str("heroImageStyle", "hero_image_style")
                if hstyle and not meta.get("heroImageStyle"):
                    meta["heroImageStyle"] = hstyle
            except Exception as e:
                print(f"WARNING: Failed to read body {path}: {e}")


def pull_tweets_to_markdown(posts: list) -> int:
    """Write share_tweet from export to {slug}.tweet.md when file is missing."""
    drafts_dir = WEBSITE_POSTS_DIR / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    for post in posts:
        slug = post.get("slug")
        tweet = post.get("share_tweet") or ""
        if not slug or not tweet.strip():
            continue
        published = post.get("published", False)
        path = tweet_md_path(slug, published)
        if path.exists():
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tweet.strip() + "\n", encoding="utf-8")
            created += 1
        except Exception as e:
            print(f"WARNING: Failed to write {path}: {e}")
    if created:
        print(
            f"Pulled {created} share tweet file(s) from export into .tweet.md for editing."
        )
    return created


def load_from_neotoma_json(path: Path) -> tuple[list, list, list]:
    """Load posts, links, timeline from a Neotoma export JSON file.
    File shape: {"posts": [...], "links": [...], "timeline": [...]} with export records.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    posts = data.get("posts") or []
    links = data.get("links") or []
    timeline = data.get("timeline") or []
    return posts, links, timeline


def generate_cache_files(from_neotoma_json_path: Path):
    """Generate cache JSON files from Neotoma export. Path must exist."""
    print("Loading website data from Neotoma export file...")
    posts, _links, _timeline = load_from_neotoma_json(from_neotoma_json_path)
    print(f"Found {len(posts)} posts from Neotoma export")

    if not posts:
        print(
            "No posts in export; building cache from drafts/*.md and content posts.json only."
        )
        export_slugs = set()
        draft_only_metadata = metadata_for_draft_only_slugs(export_slugs)
        content_only = metadata_for_content_only_drafts(export_slugs)
        draft_slugs = {m.get("slug") for m in draft_only_metadata if m.get("slug")}
        for m in content_only:
            if m.get("slug") and m.get("slug") not in draft_slugs:
                draft_only_metadata.append(m)
        published_metadata = [m for m in draft_only_metadata if m.get("published")]
        # Include published posts from content manifest (e.g. professional-mission for home page)
        existing_public_slugs = {
            m.get("slug") for m in published_metadata if m.get("slug")
        }
        content_only_published = metadata_for_content_only_published(
            existing_public_slugs
        )
        published_metadata.extend(content_only_published)
        published_metadata.sort(key=lambda m: (m.get("slug") or ""))
        published_metadata.sort(
            key=lambda m: (m.get("publishedDate") or "0000-01-01"),
            reverse=True,
        )
        overlay_summaries_from_markdown(published_metadata)
        overlay_body_from_markdown(published_metadata)
        overlay_listing_excludes(published_metadata)
        overlay_alternative_slugs(published_metadata)
        overlay_content_manifest_fields(published_metadata)
        overlay_missing_hero_from_public_files(published_metadata)
        write_json(POSTS_JSON, published_metadata)
        write_json(
            API_POSTS_JSON,
            {"url": f"{SITE_BASE}/api/posts.json", "posts": published_metadata},
        )
        localized_public = {
            locale: build_locale_posts(published_metadata, locale)
            for locale in SUPPORTED_LOCALES
        }
        non_default_locales = [locale for locale in SUPPORTED_LOCALES if locale != "en"]
        validate_initial_translation_batch(published_metadata, non_default_locales)
        report_translation_coverage(published_metadata, non_default_locales)
        validate_glossary_senses(
            {loc: posts for loc, posts in localized_public.items() if loc != "en"}
        )
        for locale in SUPPORTED_LOCALES:
            write_json(locale_posts_json(locale), localized_public[locale])
            write_json(
                locale_api_posts_json(locale),
                {
                    "url": f"{SITE_BASE}/api/posts.{locale}.json",
                    "posts": localized_public[locale],
                },
            )
        all_metadata = list(draft_only_metadata)
        for m in content_only_published:
            if m.get("slug") and not any(
                x.get("slug") == m.get("slug") for x in all_metadata
            ):
                all_metadata.append(m)
        all_metadata.sort(
            key=lambda m: (
                0 if m.get("published") else 1,
                m.get("publishedDate")
                or m.get("updatedDate")
                or m.get("createdDate")
                or "0000-01-01",
                m.get("title", ""),
            ),
            reverse=True,
        )
        overlay_summaries_from_markdown(all_metadata)
        overlay_body_from_markdown(all_metadata)
        overlay_listing_excludes(all_metadata)
        overlay_alternative_slugs(all_metadata)
        overlay_content_manifest_fields(all_metadata)
        overlay_missing_hero_from_public_files(all_metadata)
        write_json(POSTS_PRIVATE_JSON, all_metadata)
        draft_count = sum(1 for m in all_metadata if not m.get("published"))
        print(
            f"  Private cache: {POSTS_PRIVATE_JSON} ({len(all_metadata)} posts, {draft_count} drafts)"
        )
        print(
            "\nCache generation complete (drafts only). Run with Neotoma export for full site."
        )
        return

    # Deduplicate by slug (keep one per slug, prefer latest updated_date)
    seen_by_slug = {}
    for post in posts:
        slug = post.get("slug")
        if not slug:
            continue
        existing = seen_by_slug.get(slug)
        this_date = post.get("updated_date") or post.get("published_date") or ""
        existing_date = (
            (existing.get("updated_date") or existing.get("published_date") or "")
            if existing
            else ""
        )
        if existing is None or this_date >= existing_date:
            seen_by_slug[slug] = post
    posts = list(seen_by_slug.values())
    print(f"Deduplicated to {len(posts)} unique slugs")

    pull_summaries_to_markdown(posts)
    pull_tweets_to_markdown(posts)

    # Convert to metadata format (public only; no shareTweet in prod).
    # Exclude all tweet posts from public listing (tweets are always unpublished).
    def should_include_in_listing(post):
        if not post.get("published"):
            return False
        if (post.get("category") or "").lower() == "tweet":
            return False
        return True

    # Include body for all posts so Posts page search can match full text (tweets get body from export; others from overlay_body_from_markdown or export).
    published_metadata = [
        convert_post_to_metadata(
            post,
            include_body=True,
            include_share_tweet=False,
        )
        for post in posts
        if should_include_in_listing(post)
    ]

    # Include draft-only posts that have published: true in frontmatter (so they appear in public cache).
    # Include published manifest posts not already in public listing. This lets local publish metadata
    # override an export entry that may still be draft/unpublished.
    export_slugs = {p.get("slug") for p in posts if p.get("slug")}
    draft_only_metadata = metadata_for_draft_only_slugs(export_slugs)
    existing_public_slugs = {m.get("slug") for m in published_metadata if m.get("slug")}
    content_only_published = metadata_for_content_only_published(existing_public_slugs)
    published_metadata.extend(m for m in draft_only_metadata if m.get("published"))
    published_metadata.extend(content_only_published)

    # Sort by publishedDate (newest first), then slug asc for ties. Matches Post.tsx footer and Posts.tsx list order.
    published_metadata.sort(key=lambda m: (m.get("slug") or ""))
    published_metadata.sort(
        key=lambda m: (m.get("publishedDate") or "0000-01-01"),
        reverse=True,
    )

    # Prefer local .summary.md over export summary so full key takeaways deploy (export may have only first line)
    overlay_summaries_from_markdown(published_metadata)
    # Add or override body from .md so full-text search on Posts page works
    overlay_body_from_markdown(published_metadata)
    overlay_listing_excludes(published_metadata)
    overlay_alternative_slugs(published_metadata)
    overlay_content_manifest_fields(published_metadata)
    overlay_missing_hero_from_public_files(published_metadata)

    # Write public cache (published only)
    print(f"\nWriting public cache: {POSTS_JSON}")
    print(f"  Published posts: {len(published_metadata)}")
    write_json(POSTS_JSON, published_metadata)
    write_json(
        API_POSTS_JSON,
        {"url": f"{SITE_BASE}/api/posts.json", "posts": published_metadata},
    )
    localized_public = {
        locale: build_locale_posts(published_metadata, locale)
        for locale in SUPPORTED_LOCALES
    }
    non_default_locales = [locale for locale in SUPPORTED_LOCALES if locale != "en"]
    validate_initial_translation_batch(published_metadata, non_default_locales)
    report_translation_coverage(published_metadata, non_default_locales)
    validate_glossary_senses(
        {loc: posts for loc, posts in localized_public.items() if loc != "en"}
    )
    for locale in SUPPORTED_LOCALES:
        write_json(locale_posts_json(locale), localized_public[locale])
        write_json(
            locale_api_posts_json(locale),
            {
                "url": f"{SITE_BASE}/api/posts.{locale}.json",
                "posts": localized_public[locale],
            },
        )

    # Write private cache (all posts including drafts) for dev: /posts/draft and draft count.
    # Include body so dev can render posts that have no .md file yet.
    all_metadata = [convert_post_to_metadata(post, include_body=True) for post in posts]

    # Include draft-only posts (drafts/*.md and content posts.json drafts not in export) so they are viewable in dev.
    export_slugs = {p.get("slug") for p in posts if p.get("slug")}
    draft_only_metadata = metadata_for_draft_only_slugs(export_slugs)
    content_only_drafts = metadata_for_content_only_drafts(export_slugs)
    draft_slugs = {m.get("slug") for m in draft_only_metadata if m.get("slug")}
    for m in content_only_drafts:
        if m.get("slug") and m.get("slug") not in draft_slugs:
            draft_only_metadata.append(m)
    if draft_only_metadata:
        all_metadata.extend(draft_only_metadata)
        print(
            f"  Added {len(draft_only_metadata)} draft-only post(s) from drafts/*.md and content posts for dev."
        )

    # Sort: published by publishedDate desc, then drafts by updatedDate desc
    def private_sort_key(meta):
        if meta.get("published"):
            return (0, meta.get("publishedDate") or "0000-01-01", meta.get("title", ""))
        return (
            1,
            meta.get("updatedDate") or meta.get("createdDate") or "0000-01-01",
            meta.get("title", ""),
        )

    all_metadata.sort(key=private_sort_key, reverse=True)
    overlay_summaries_from_markdown(all_metadata)
    overlay_body_from_markdown(all_metadata)
    overlay_listing_excludes(all_metadata)
    overlay_alternative_slugs(all_metadata)
    overlay_content_manifest_fields(all_metadata)
    overlay_missing_hero_from_public_files(all_metadata)
    write_json(POSTS_PRIVATE_JSON, all_metadata)
    draft_count = sum(1 for p in posts if not p.get("published"))
    print(
        f"  Private cache: {POSTS_PRIVATE_JSON} ({len(all_metadata)} posts, {draft_count} drafts)"
    )

    print(f"\n{'=' * 60}")
    print("Cache generation complete!")
    print(f"  Public cache: {POSTS_JSON}")
    print(f"  API posts: {API_POSTS_JSON} (absolute URL: {SITE_BASE}/api/posts.json)")
    print("\nThe React app will load posts from these cache files.")


def generate_links_cache(links: list):
    """Generate links.json cache from Neotoma export links list."""
    print(f"\nUsing {len(links)} links from Neotoma export")
    if not links:
        print("WARNING: No links in export; skipping links cache.")
        return

    links.sort(key=lambda item: item.get("display_order") or 0)
    output = [
        {
            "name": link.get("name"),
            "url": link.get("url"),
            "icon": link.get("icon"),
            "description": link.get("description"),
        }
        for link in links
    ]

    print(f"Writing links cache: {LINKS_JSON}")
    write_json(LINKS_JSON, output)
    write_json(
        API_LINKS_JSON,
        {"url": f"{SITE_BASE}/api/links.json", "links": output},
    )


def generate_timeline_cache(timeline: list):
    """Generate timeline.json cache from Neotoma export timeline list."""
    print(f"\nUsing {len(timeline)} timeline entries from Neotoma export")
    if not timeline:
        print("WARNING: No timeline in export; skipping timeline cache.")
        return

    timeline.sort(key=lambda item: item.get("display_order") or 0)
    output = []
    for entry in timeline:
        description = entry.get("description") or ""
        description_list = []
        if isinstance(description, list):
            description_list = description
        elif isinstance(description, str) and description.strip():
            try:
                parsed = json.loads(description)
                if isinstance(parsed, list):
                    description_list = parsed
                else:
                    description_list = [str(parsed)]
            except (json.JSONDecodeError, TypeError):
                description_list = [description]

        output.append(
            {
                "role": entry.get("role"),
                "company": entry.get("company"),
                "date": entry.get("date"),
                "description": description_list,
            }
        )

    print(f"Writing timeline cache: {TIMELINE_JSON}")
    write_json(TIMELINE_JSON, output)
    write_json(
        API_TIMELINE_JSON,
        {"url": f"{SITE_BASE}/api/timeline.json", "timeline": output},
    )


def generate_markdown_files(posts: list):
    """Optional: Generate markdown files from post body (from Neotoma export)."""
    print("\nGenerating markdown files from export...")

    DRAFTS_DIR = WEBSITE_POSTS_DIR / "drafts"
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

    generated = 0
    for post in posts:
        slug = post["slug"]
        body = post.get("body", "")
        published = post.get("published", False)

        # Determine target directory
        if published:
            md_file = WEBSITE_POSTS_DIR / f"{slug}.md"
        else:
            md_file = DRAFTS_DIR / f"{slug}.md"

        # Write markdown file
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(body)

        generated += 1

    print(f"Generated {generated} markdown files")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate website cache from Neotoma export"
    )
    parser.add_argument(
        "--from-neotoma-json",
        type=Path,
        metavar="PATH",
        default=DEFAULT_NEOTOMA_EXPORT,
        help=f"Neotoma export JSON path (default: {DEFAULT_NEOTOMA_EXPORT})",
    )
    parser.add_argument(
        "--generate-markdown",
        action="store_true",
        help="Also generate markdown files from post body",
    )
    args = parser.parse_args()

    export_path = (
        args.from_neotoma_json.resolve()
        if args.from_neotoma_json
        else DEFAULT_NEOTOMA_EXPORT.resolve()
    )
    if not export_path.exists():
        export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(export_path, "w", encoding="utf-8") as f:
            json.dump({"posts": [], "links": [], "timeline": []}, f, indent=2)
        print(f"No Neotoma export at {export_path}; created minimal export.")
        print(
            "Building cache from drafts/*.md only. Export from Neotoma MCP to see full site."
        )

    print("=" * 60)
    print("Generating Website Cache from Neotoma Export")
    print("=" * 60)

    posts, links, timeline = load_from_neotoma_json(export_path)
    # Dedupe posts by slug (same logic as in generate_cache_files)
    seen = {}
    for p in posts:
        slug = p.get("slug")
        if not slug:
            continue
        this_date = p.get("updated_date") or p.get("published_date") or ""
        existing = seen.get(slug)
        existing_date = (
            (existing.get("updated_date") or existing.get("published_date") or "")
            if existing
            else ""
        )
        if existing is None or this_date >= existing_date:
            seen[slug] = p
    posts_deduped = list(seen.values())

    generate_cache_files(from_neotoma_json_path=export_path)
    generate_links_cache(links)
    generate_timeline_cache(timeline)

    if args.generate_markdown:
        generate_markdown_files(posts_deduped)


if __name__ == "__main__":
    main()
