#!/usr/bin/env python3
"""
Export website posts from Parquet for migration to Neotoma.

Reads posts via Parquet MCP, dedupes by slug (keeps latest updated_date), and writes
full records to JSON. Use Neotoma MCP store_structured (entity_type="post") to ingest
each record; then delete website data from Parquet per migration rules.

Usage:
  python execution/scripts/migrate_website_parquet_to_neotoma.py
  python execution/scripts/migrate_website_parquet_to_neotoma.py --slugs slug1,slug2
  python execution/scripts/migrate_website_parquet_to_neotoma.py --output /path/to/export.json

Output: data/tmp/website_posts_for_neotoma_export.json (or --output path).
JSON shape: {"posts": [{...full record...}, ...]} (parquet-like fields; add entity_type when storing).
"""

import json
import os
import sys
from pathlib import Path

# Add repo root and execution/scripts for imports
repo_root = Path(__file__).resolve().parent.parent.parent
scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(scripts_dir))

from dotenv import load_dotenv

load_dotenv(repo_root / ".env")

from parquet_client import ParquetMCPClient

PROJECT_ROOT = repo_root
DATA_DIR = Path(os.getenv("DATA_DIR", repo_root / "data"))
DEFAULT_OUTPUT = DATA_DIR / "tmp" / "website_posts_for_neotoma_export.json"


def get_parquet_client():
    parquet_server_path = PROJECT_ROOT / "mcp" / "parquet" / "parquet_mcp_server.py"
    if not parquet_server_path.exists():
        raise FileNotFoundError(
            f"Parquet MCP server not found at {parquet_server_path}. "
            "Run from repo root with mcp/parquet submodule initialized."
        )
    return ParquetMCPClient(parquet_server_path=str(parquet_server_path))


def read_all_posts_deduped(client: ParquetMCPClient) -> list[dict]:
    """Read all posts from Parquet and dedupe by slug (keep latest updated_date)."""
    result = client.call_tool_sync(
        "read_parquet",
        {
            "data_type": "posts",
            "limit": 5000,
            "sort_by": [
                {"column": "updated_date", "ascending": False, "na_position": "last"}
            ],
        },
    )
    posts = result.get("data") or []
    seen = {}
    for p in posts:
        slug = p.get("slug")
        if not slug:
            continue
        if slug not in seen:
            seen[slug] = p
    return list(seen.values())


def export_posts_for_neotoma(
    output_path: Path,
    slugs_filter: list[str] | None = None,
) -> int:
    """Export full post records to JSON for Neotoma store_structured. Returns count."""
    client = get_parquet_client()
    posts = read_all_posts_deduped(client)
    if slugs_filter:
        slugs_set = set(slugs_filter)
        posts = [p for p in posts if p.get("slug") in slugs_set]
    # Ensure output dir exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"posts": posts}, f, indent=2, ensure_ascii=False)
    return len(posts)


def write_batch_files(output_path: Path, batch_size: int = 5) -> int:
    """Read export JSON and write per-batch JSON files for store_structured. Returns number of batches."""
    import json

    data = json.loads(output_path.read_text(encoding="utf-8"))
    posts = data.get("posts", [])
    out_dir = output_path.parent
    for i in range(0, len(posts), batch_size):
        batch = posts[i : i + batch_size]
        for p in batch:
            p["entity_type"] = "post"
        batch_path = out_dir / f"website_posts_batch_{i // batch_size + 1}.json"
        batch_path.write_text(
            json.dumps({"entities": batch}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return (len(posts) + batch_size - 1) // batch_size


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Export website posts from Parquet for Neotoma migration"
    )
    parser.add_argument(
        "--output", "-o", type=Path, default=DEFAULT_OUTPUT, help="Output JSON path"
    )
    parser.add_argument(
        "--slugs", type=str, help="Comma-separated slugs to export (default: all)"
    )
    parser.add_argument(
        "--write-batches",
        action="store_true",
        help="After export, write batch JSON files for store_structured",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Posts per batch when --write-batches (default: 5)",
    )
    args = parser.parse_args()
    slugs_filter = [s.strip() for s in args.slugs.split(",")] if args.slugs else None
    n = export_posts_for_neotoma(args.output, slugs_filter=slugs_filter)
    print(f"Exported {n} posts to {args.output}")
    if args.write_batches:
        nb = write_batch_files(args.output, batch_size=args.batch_size)
        print(f"Wrote {nb} batch files to {args.output.parent}")
