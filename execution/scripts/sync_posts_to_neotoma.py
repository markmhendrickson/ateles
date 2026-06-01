#!/usr/bin/env python3
"""
Sync local post markdown edits into Neotoma.

This script is the reverse of generate_posts_cache.py overlays:
- Reads post bodies from react-app/src/content/posts/**/*.md (including drafts/)
- Reads frontmatter metadata from those markdown files (title, excerpt, published, hero refs, etc.)
- Reads manifest metadata from react-app/src/content/posts/posts.json (dates, tags, read time, alternative slugs)
- Reads key takeaways from *.summary.md
- Reads share tweets from *.tweet.md

It compares local content against the current Neotoma snapshot for each slug and,
when different, writes updates to Neotoma via `neotoma store --file ...`.

Default behavior only considers files modified after the last Neotoma website export
(data/tmp/neotoma_website_export.json). Use --all to force checking everything, and
--slug <slug> to limit sync to a specific post.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
load_dotenv(REPO_ROOT / ".env")

EXPORT_PATH_DEFAULT = REPO_ROOT / "data" / "tmp" / "neotoma_website_export.json"
RAW_POSTS_PATH_DEFAULT = REPO_ROOT / "data" / "tmp" / "neotoma_posts_raw.json"
WEBSITE_POSTS_DIR = (
    REPO_ROOT
    / "execution"
    / "website"
    / "markmhendrickson"
    / "react-app"
    / "src"
    / "content"
    / "posts"
).resolve()
CONTENT_MANIFEST_JSON = WEBSITE_POSTS_DIR / "posts.json"


@dataclass(frozen=True)
class LocalPostEdits:
    slug: str
    fields: dict[str, object] = field(default_factory=dict)


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---\n"):
        return {}, content
    idx = content.find("\n---\n", 4)
    if idx == -1:
        return {}, content
    parsed: dict[str, str] = {}
    for line in content[4:idx].splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (
            v.startswith("'") and v.endswith("'")
        ):
            v = v[1:-1]
        parsed[k.strip().lower()] = v
    return parsed, content[idx + 5 :].lstrip("\n")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_content_manifest() -> dict[str, dict]:
    if not CONTENT_MANIFEST_JSON.exists():
        return {}
    try:
        raw = json.loads(CONTENT_MANIFEST_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, list):
        return {}
    out: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if isinstance(slug, str) and slug:
            out[slug] = entry
    return out


def _slug_from_path(path: Path) -> str:
    return path.stem.replace(".summary", "").replace(".tweet", "")


def _export_mtime(export_path: Path) -> float:
    try:
        return export_path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _load_raw_posts_by_slug(path: Path = RAW_POSTS_PATH_DEFAULT) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    entities = payload.get("entities") or []
    out: dict[str, dict] = {}
    for entity in entities:
        snapshot = entity.get("snapshot") or {}
        slug = snapshot.get("slug")
        if isinstance(slug, str) and slug:
            out[slug] = entity
    return out


def _neotoma_entities_list_post_by_slug(
    slug: str, raw_posts_by_slug: dict[str, dict] | None = None
) -> dict | None:
    if raw_posts_by_slug and slug in raw_posts_by_slug:
        return raw_posts_by_slug[slug]
    cmd = [
        "neotoma",
        "--json",
        "entities",
        "list",
        "--type",
        "post",
        "--search",
        slug,
        "--limit",
        "10",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Neotoma CLI failed: {' '.join(cmd)}\n{p.stderr.strip()}")
    payload = json.loads(p.stdout or "{}")
    entities = payload.get("entities") or []
    if not entities:
        return None
    for e in entities:
        snap = e.get("snapshot") or {}
        if snap.get("slug") == slug:
            return e
    for e in entities:
        if e.get("canonical_name") == slug:
            return e
    return entities[0]


def _snapshot_field(entity: dict | None, field: str) -> str:
    if not entity:
        return ""
    snap = entity.get("snapshot") or {}
    v = snap.get(field)
    return v if isinstance(v, str) else ("" if v is None else str(v))


def _snapshot_value(entity: dict | None, field: str) -> object:
    if not entity:
        return None
    return (entity.get("snapshot") or {}).get(field)


def _parse_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in {"true", "1", "yes"}:
            return True
        if lower in {"false", "0", "no"}:
            return False
    return None


def _frontmatter_metadata(frontmatter: dict[str, str]) -> dict[str, object]:
    out: dict[str, object] = {}
    mapping = {
        "title": "title",
        "excerpt": "excerpt",
        "category": "category",
        "published_date": "published_date",
        "publisheddate": "published_date",
        "hero_image": "hero_image",
        "hero_image_style": "hero_image_style",
        "hero_image_square": "hero_image_square",
        "og_image": "og_image",
        "hero_image_og": "og_image",
    }
    for src, dest in mapping.items():
        value = frontmatter.get(src)
        if value:
            out[dest] = value
    published = _parse_bool(frontmatter.get("published"))
    if published is not None:
        out["published"] = published
    read_time = frontmatter.get("read_time") or frontmatter.get("readtime")
    if read_time:
        try:
            out["read_time"] = int(read_time)
        except ValueError:
            pass
    return out


def _manifest_metadata(entry: dict | None) -> dict[str, object]:
    if not entry:
        return {}
    out: dict[str, object] = {}
    mapping = {
        "title": "title",
        "excerpt": "excerpt",
        "category": "category",
        "publishedDate": "published_date",
        "createdDate": "created_date",
        "updatedDate": "updated_date",
        "readTime": "read_time",
        "heroImage": "hero_image",
        "heroImageStyle": "hero_image_style",
        "heroImageSquare": "hero_image_square",
        "ogImage": "og_image",
    }
    for src, dest in mapping.items():
        value = entry.get(src)
        if value not in (None, ""):
            out[dest] = value
    if entry.get("published") is not None:
        out["published"] = bool(entry["published"])
    if isinstance(entry.get("tags"), list):
        out["tags"] = json.dumps(entry["tags"], ensure_ascii=False)
    if isinstance(entry.get("alternativeSlugs"), list) and entry["alternativeSlugs"]:
        out["alternative_slugs"] = json.dumps(
            entry["alternativeSlugs"], ensure_ascii=False
        )
    return out


def _normalize_compare_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _deterministic_idempotency_key(slug: str, edits: LocalPostEdits) -> str:
    h = hashlib.sha256()
    h.update(slug.encode("utf-8"))
    for key in sorted(edits.fields):
        h.update(b"\0")
        h.update(key.encode("utf-8"))
        h.update(b"=")
        h.update(_normalize_compare_value(edits.fields[key]).encode("utf-8"))
    return f"sync-post-{slug}-{h.hexdigest()[:16]}"


def _store_post_fields(slug: str, fields: dict, dry_run: bool) -> None:
    entity = {"entity_type": "post", "slug": slug, **fields}
    idempotency_key = _deterministic_idempotency_key(
        slug, LocalPostEdits(slug=slug, fields=fields)
    )
    if dry_run:
        print(f"DRY RUN: would store fields for {slug}: {sorted(fields.keys())}")
        return

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", delete=False
    ) as f:
        json.dump([entity], f, ensure_ascii=False)
        tmp_path = f.name
    try:
        cmd = [
            "neotoma",
            "store",
            "--file",
            tmp_path,
            "--idempotency-key",
            idempotency_key,
        ]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"Neotoma store failed for {slug}:\n{p.stderr.strip()}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def collect_local_edits(
    export_mtime: float, include_all: bool, slugs: set[str] | None = None
) -> list[LocalPostEdits]:
    if not WEBSITE_POSTS_DIR.exists():
        return []
    manifest = _load_content_manifest()

    candidates: dict[str, dict[str, Path]] = {}

    def consider(path: Path, kind: str) -> None:
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return
        if not include_all and mtime <= export_mtime:
            return
        slug = _slug_from_path(path)
        if slugs is not None and slug not in slugs:
            return
        candidates.setdefault(slug, {})[kind] = path

    for p in WEBSITE_POSTS_DIR.glob("*.md"):
        if (
            p.name == "README.md"
            or p.name.endswith(".summary.md")
            or p.name.endswith(".tweet.md")
        ):
            continue
        consider(p, "body")

    drafts_dir = WEBSITE_POSTS_DIR / "drafts"
    if drafts_dir.exists():
        for p in drafts_dir.glob("*.md"):
            if p.name.endswith(".summary.md") or p.name.endswith(".tweet.md"):
                continue
            consider(p, "body")

    for p in WEBSITE_POSTS_DIR.rglob("*.summary.md"):
        consider(p, "summary")
    for p in WEBSITE_POSTS_DIR.rglob("*.tweet.md"):
        consider(p, "tweet")

    edits: list[LocalPostEdits] = []
    for slug, paths in sorted(candidates.items()):
        fields: dict[str, object] = {}
        manifest_entry = manifest.get(slug)
        if (bp := paths.get("body")) is not None:
            raw = _read_text(bp)
            frontmatter, body = _parse_frontmatter(raw)
            fields["body"] = body
            fields.update(_manifest_metadata(manifest_entry))
            fields.update(_frontmatter_metadata(frontmatter))
        else:
            fields.update(_manifest_metadata(manifest_entry))
        if "summary" in paths:
            fields["summary"] = _read_text(paths["summary"]).strip()
        if "tweet" in paths:
            fields["share_tweet"] = _read_text(paths["tweet"]).strip()
        if fields:
            edits.append(LocalPostEdits(slug=slug, fields=fields))
    return edits


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync local post markdown edits into Neotoma."
    )
    parser.add_argument("--export-path", type=Path, default=EXPORT_PATH_DEFAULT)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Check all local post files (ignore export mtime).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--slug",
        action="append",
        dest="slugs",
        help="Limit sync to a specific slug. Repeat for multiple slugs.",
    )
    args = parser.parse_args()

    export_mtime = _export_mtime(args.export_path)
    if export_mtime:
        ts = datetime.fromtimestamp(export_mtime, tz=UTC).isoformat()
        print(f"Using export mtime baseline: {args.export_path} ({ts})")
    else:
        print(
            f"No export mtime baseline found at {args.export_path}; defaulting to 0 (all newer files)."
        )

    slug_filter = set(args.slugs) if args.slugs else None
    local_edits = collect_local_edits(
        export_mtime=export_mtime, include_all=args.all, slugs=slug_filter
    )
    if not local_edits:
        print("No local edits to sync.")
        return

    raw_posts_by_slug = _load_raw_posts_by_slug()
    updated = 0
    skipped_missing = 0
    for edits in local_edits:
        entity = _neotoma_entities_list_post_by_slug(edits.slug, raw_posts_by_slug)
        desired: dict[str, object] = {}
        for key, value in edits.fields.items():
            if _normalize_compare_value(value) != _normalize_compare_value(
                _snapshot_value(entity, key)
            ):
                desired[key] = value

        if not desired:
            continue

        if entity is None:
            print(f"CREATE: {edits.slug} -> {sorted(desired.keys())}")
            skipped_missing += 1
        else:
            print(f"UPDATE: {edits.slug} -> {sorted(desired.keys())}")
        _store_post_fields(edits.slug, desired, dry_run=args.dry_run)
        updated += 1

    print(f"Synced {updated} post(s). Skipped missing: {skipped_missing}.")


if __name__ == "__main__":
    main()
