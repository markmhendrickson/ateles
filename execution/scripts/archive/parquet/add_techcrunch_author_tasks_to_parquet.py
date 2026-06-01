#!/usr/bin/env python3
"""
Add task(s) to tasks.parquet for remaining work from TechCrunch author/sitemap chat.

Remaining work:
1. Run Apify Sitemap to URL Scraper for TechCrunch sitemap.xml
2. Filter sitemap URLs to article paths and date range 2007-2009
3. Crawl article URLs and extract author (Mark Hendrickson)
4. Import confirmed Mark Hendrickson articles into posts or related_materials parquet

Uses DATA_DIR from config. Creates snapshot before write.
Ref: tmp/techcrunch_author_sitemap_analysis.md, reports.parquet (TechCrunch Author Page and Sitemap Analysis).
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "execution" / "scripts"))

from config import get_data_dir

DATA_DIR = get_data_dir()
TASKS_PATH = DATA_DIR / "tasks" / "tasks.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"


def ensure_snapshot(path: Path) -> None:
    if not path.exists():
        return
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    stem = path.stem
    snap = SNAPSHOTS_DIR / f"{stem}-{ts}.parquet"
    import shutil

    shutil.copy2(path, snap)


def build_tasks() -> list[dict]:
    import pandas as pd

    today_d = date.today()
    today = today_d.isoformat()
    due_d = date.today() + timedelta(days=60)
    due_d.isoformat()

    def row(**kwargs) -> dict:
        base = {
            "task_id": str(uuid4())[:16],
            "title": "",
            "description": None,
            "description_html": None,
            "description_html_remote": None,
            "domain": "Admin",
            "status": "pending",
            "due_date": None,
            "start_date": None,
            "completed_date": None,
            "recurrence": None,
            "execution_plan_path": None,
            "notes": None,
            "project_ids": None,
            "project_names": None,
            "outcome_ids": None,
            "outcome_names": None,
            "section_ids": None,
            "section_names": None,
            "my_tasks_section_ids": None,
            "my_tasks_section_names": None,
            "assignee_gid": None,
            "assignee_name": None,
            "created_at": pd.Timestamp.now(tz="UTC"),
            "updated_at": pd.Timestamp.now(tz="UTC"),
            "asana_workspace": None,
            "asana_source_gid": None,
            "asana_target_gid": None,
            "parent_task_id": None,
            "permalink_url": None,
            "followers_gids": None,
            "follower_names": None,
            "import_date": today_d,
            "import_source_file": "chat_techcrunch_author_sitemap_2026_01_13",
            "created_date": today,
            "updated_date": today,
            "sync_log": None,
            "sync_datetime": None,
            "tags": None,
        }
        base.update(kwargs)
        return base

    return [
        row(
            task_id=str(uuid4())[:16],
            title="Run Apify Sitemap scraper for TechCrunch",
            description="Use Apify Sitemap to URL Scraper on https://techcrunch.com/sitemap.xml to extract full URL set from all 2,034 sitemap pages (~400k URLs). Output: list of article URLs (path pattern /YYYY/MM/DD/...).",
            due_date=due_d,
            notes="Chat 2026-01-13: TechCrunch has sitemap index with 2,034 child sitemaps; no author in sitemap. Apify actor: logiover/sitemap-to-url-crawler or equivalent. Ref: tmp/techcrunch_author_sitemap_analysis.md.",
        ),
        row(
            task_id=str(uuid4())[:16],
            title="Filter TechCrunch sitemap URLs by date 2007-2009",
            description="From Apify sitemap output, filter to article URLs (path contains /YYYY/MM/DD/) and date range 2007-2009 to narrow candidate set for Mark Hendrickson articles (569 posts per farewell post).",
            due_date=due_d,
            notes="Chat 2026-01-13: Mark Hendrickson wrote 569 posts (2007-2009); author page shows only 3. Filter by URL date to reduce crawl volume. Ref: tmp/techcrunch_author_sitemap_analysis.md.",
        ),
        row(
            task_id=str(uuid4())[:16],
            title="Crawl TechCrunch article URLs and extract author",
            description='For each filtered article URL (or sample), fetch page and check author: <meta name="author" content="Mark Hendrickson" /> or schema/JSON-LD. Collect URL, title, date, author. Use Apify or custom Crawlee actor with concurrency/proxy if needed.',
            due_date=due_d,
            notes="Chat 2026-01-13: Author not in sitemap; must crawl articles. Apify supports Request Queue, concurrency, proxy pool. Ref: tmp/techcrunch_author_sitemap_analysis.md.",
        ),
        row(
            task_id=str(uuid4())[:16],
            title="Import Mark Hendrickson TechCrunch articles to parquet",
            description="Persist discovered Mark Hendrickson articles (url, title, date, author) to parquet: e.g. posts.parquet (if website posts) or related_materials/publications/external_articles. Rebuild website post cache if posts updated.",
            due_date=due_d,
            notes="Chat 2026-01-13: After crawl yields author-confirmed list, add records via parquet MCP or script. If posts: run generate_posts_cache.py after. Ref: tmp/techcrunch_author_sitemap_analysis.md, .cursor/rules/persistence.mdc.",
        ),
    ]


def main() -> int:
    import pandas as pd

    TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ensure_snapshot(TASKS_PATH)

    if TASKS_PATH.exists():
        df = pd.read_parquet(TASKS_PATH)
    else:
        df = pd.DataFrame()

    new_records = build_tasks()
    new_df = pd.DataFrame(new_records)
    for c in df.columns:
        if c not in new_df.columns:
            new_df[c] = None
    if not df.empty:
        new_df = new_df.reindex(columns=df.columns, fill_value=None)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat([df, new_df], ignore_index=True)
    df.to_parquet(TASKS_PATH, index=False)
    print(f"Added {len(new_records)} task(s) to {TASKS_PATH}")
    for t in new_records:
        print(f"  - {t['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
