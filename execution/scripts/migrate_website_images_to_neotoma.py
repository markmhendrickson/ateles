#!/usr/bin/env python3
"""
Discover website hero/OG image assets for migration to Neotoma storage.

Produces a JSON manifest of (slug, role, path) for each file under:
- execution/website/markmhendrickson/react-app/public/images/posts/
- execution/website/markmhendrickson/react-app/public/images/og/

Migration steps (run via Cursor with Neotoma MCP, or use output with MCP client):
1. For each manifest entry:
   a. store_unstructured(idempotency_key="website-{role}-{slug}", file_path=abs_path, interpret=false)
   b. store_structured(idempotency_key="website-image-{slug}-{role}", entities=[{entity_type: "image", source_id: <from a>, role, slug, filename}])
   c. create_relationship(EMBEDS, source_entity_id=post_entity_id, target_entity_id=image_entity_id)
2. Post entity_id must be resolved by slug: paginate Neotoma retrieve_entities(entity_type="post", limit=50, offset=0,50,100,...), and for each entity collect snapshot.slug -> entity_id into a map (e.g. data/tmp/post_entity_ids.json). Then use that map when creating EMBEDS (source_entity_id = post_entity_id for the manifest slug).

After migration, Neotoma holds the canonical copy; the site can continue serving from static
paths. When Neotoma exposes HTTP URLs for stored files, the export builder can resolve
EMBEDS to hero_image_url/og_image_url and the cache can use those URLs.

Usage:
  python execution/scripts/migrate_website_images_to_neotoma.py
  python execution/scripts/migrate_website_images_to_neotoma.py --output data/tmp/website_hero_manifest.json
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
POSTS_IMAGES = (
    REPO_ROOT
    / "execution"
    / "website"
    / "markmhendrickson"
    / "react-app"
    / "public"
    / "images"
    / "posts"
)
OG_IMAGES = (
    REPO_ROOT
    / "execution"
    / "website"
    / "markmhendrickson"
    / "react-app"
    / "public"
    / "images"
    / "og"
)


def discover_assets() -> list[dict]:
    """Discover hero and OG assets on disk. Returns list of {slug, role, filename, path}."""
    manifest = []
    # Hero set: {slug}-hero.{ext}, {slug}-hero-square.{ext}, {slug}-hero-og.{ext}
    if not POSTS_IMAGES.exists():
        return manifest
    seen_slugs = set()
    for f in POSTS_IMAGES.iterdir():
        if f.is_dir() or f.suffix in (".txt",):
            continue
        name = f.name
        if (
            "-hero.png" in name
            or name.endswith("-hero.jpg")
            or name.endswith("-hero.webp")
        ):
            slug = name.rsplit("-hero", 1)[0]
            role = "hero"
            seen_slugs.add(slug)
            manifest.append(
                {"slug": slug, "role": role, "filename": name, "path": str(f)}
            )
        elif "-hero-square" in name:
            slug = name.split("-hero-square")[0]
            role = "hero_square"
            manifest.append(
                {"slug": slug, "role": role, "filename": name, "path": str(f)}
            )
        elif "-hero-og" in name:
            slug = name.split("-hero-og")[0]
            role = "hero_og"
            manifest.append(
                {"slug": slug, "role": role, "filename": name, "path": str(f)}
            )
    # OG dir: {slug}-1200x630.jpg
    if OG_IMAGES.exists():
        for f in OG_IMAGES.iterdir():
            if not f.is_file() or f.suffix.lower() != ".jpg":
                continue
            if "-1200x630.jpg" in f.name:
                slug = f.name.replace("-1200x630.jpg", "")
                manifest.append(
                    {"slug": slug, "role": "og", "filename": f.name, "path": str(f)}
                )
    return manifest


def main():
    p = argparse.ArgumentParser(
        description="Discover website hero/OG assets for Neotoma migration"
    )
    p.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "data" / "tmp" / "website_hero_manifest.json",
    )
    args = p.parse_args()
    manifest = discover_assets()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {len(manifest)} asset entries to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
