#!/usr/bin/env python3
"""
Add a report record to $DATA_DIR/reports/reports.parquet.

Stores the report metadata and, by default, the full markdown in the `content` field.
Uses snapshot before write.

Usage:
  python execution/scripts/add_report_to_parquet.py [path_to_report.md]
  (default: strategy/operations/finance/yoga-payment-task-automation.md)

  python execution/scripts/add_report_to_parquet.py --update [path_to_report.md]
  Update existing record: backfill or refresh `content` for a report matched by file_path.
"""

import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

# Repo root: execution/scripts -> execution -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "execution" / "scripts"))

from config import get_data_dir

DATA_DIR = get_data_dir()
REPORTS_FILE = DATA_DIR / "reports" / "reports.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
SCHEMAS_DIR = DATA_DIR / "schemas"


def ensure_snapshot(path: Path) -> None:
    """Create timestamped snapshot of parquet file before modification."""
    if not path.exists():
        return
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    stem = path.stem
    snap = SNAPSHOTS_DIR / f"{stem}-{ts}.parquet"
    import shutil

    shutil.copy2(path, snap)


def build_report_record(
    report_path: Path,
    report_type: str = "operational_review",
    title: str | None = None,
    subject: str | None = None,
    executive_summary: str | None = None,
    category: str | None = None,
) -> dict:
    """Build a report record from a markdown file path."""
    content = report_path.read_text(encoding="utf-8")
    if title is None:
        title = report_path.stem.replace("-", " ").replace("_", " ").title()
    if subject is None:
        subject = title
    if executive_summary is None:
        # First non-empty line after first # heading
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if line.startswith("## ") and i + 1 < len(lines):
                executive_summary = lines[i + 1][:500] if lines[i + 1] else title
                break
        if executive_summary is None:
            executive_summary = content[:500].replace("\n", " ")
    today = date.today().isoformat()
    rel_path = (
        str(report_path)
        if not str(report_path).startswith(str(REPO_ROOT))
        else str(report_path.relative_to(REPO_ROOT))
    )
    return {
        "report_id": str(uuid4())[:16],
        "report_type": report_type,
        "title": title,
        "subject": subject,
        "report_date": today,
        "category": category,
        "executive_summary": executive_summary,
        "file_path": rel_path,
        "content": content,
        "status": "active",
        "created_date": today,
        "updated_date": today,
        "import_date": today,
        "import_source_file": rel_path,
    }


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--update" and not a.startswith("-")]
    update_existing = "--update" in sys.argv
    report_path_arg = args[0] if args else None
    if report_path_arg:
        report_path = Path(report_path_arg)
        if not report_path.is_absolute():
            report_path = REPO_ROOT / report_path
    else:
        report_path = (
            REPO_ROOT
            / "strategy"
            / "operations"
            / "finance"
            / "yoga-payment-task-automation.md"
        )

    if not report_path.exists():
        print(f"Report file not found: {report_path}", file=sys.stderr)
        sys.exit(1)

    # Doc-specific defaults
    if "yoga" in report_path.name.lower():
        title = "Yoga Payment Task Automation"
        subject = "Yoga payment task automation (private class with Manel)"
        executive_summary = (
            "Rule: The yoga payment task due date must be the day after the next Yoga with Manel class. "
            "Task: Pay €60 via Bitcoin after each private class. Update due date via script using Google Calendar or events.parquet."
        )
        category = "Finance"
        report_type = "operational_review"
    elif "relevance_analysis" in report_path.name.lower():
        title = "Ujjwal Chadha Micro-SaaS 2026 Relevance Analysis"
        subject = (
            "Micro-SaaS 2026 opportunity (X/Twitter) relevance to Ateles and Neotoma"
        )
        executive_summary = (
            "Relevance analysis of Ujjwal Chadha tweet framing micro-SaaS as 2026 opportunity for software engineers. "
            "Overall relevance: moderate for Ateles (positioning clarity); directly relevant for Neotoma GTM given explicit productization intention. "
            "Section 12 covers Neotoma-specific implications."
        )
        category = "Strategy"
        report_type = "strategic_assessment"
    elif (
        "techcrunch" in report_path.name.lower()
        or "sitemap_analysis" in report_path.name.lower()
    ):
        title = "TechCrunch Author Page and Sitemap Analysis"
        subject = "Discovering Mark Hendrickson TechCrunch articles at scale (author page, RSS, Apify, sitemaps)"
        executive_summary = (
            "TechCrunch has no author RSS; author page shows 3 articles; farewell post states 569 posts. "
            "Apify supports scale and discovery. TechCrunch sitemaps: 2,034 pages, ~400k URLs; no author in sitemap. "
            "Recommend: Apify Sitemap scraper, filter by date, crawl articles for author, persist to parquet."
        )
        category = "Content"
        report_type = "operational_review"
    else:
        title = subject = executive_summary = category = None
        report_type = "operational_review"

    record = build_report_record(
        report_path,
        report_type=report_type,
        title=title,
        subject=subject,
        executive_summary=executive_summary,
        category=category,
    )

    import pandas as pd

    REPORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ensure_snapshot(REPORTS_FILE)

    rel_path_str = record["file_path"]
    content = record["content"]

    if REPORTS_FILE.exists():
        df = pd.read_parquet(REPORTS_FILE)
    else:
        df = pd.DataFrame()

    if update_existing and not df.empty and "file_path" in df.columns:
        mask = (
            df["file_path"]
            .astype(str)
            .str.contains(rel_path_str.replace("\\", "/"), regex=False, na=False)
        )
        if mask.any():
            if "content" not in df.columns:
                df["content"] = None
            df.loc[mask, "content"] = content
            df.loc[mask, "updated_date"] = record["updated_date"]
            df.to_parquet(REPORTS_FILE, index=False)
            print(
                f"Updated content for report: {record['title']} (file_path={rel_path_str})"
            )
            print(f"File: {REPORTS_FILE}")
            return
    elif update_existing:
        print(
            "No existing report with matching file_path; adding new record.",
            file=sys.stderr,
        )

    if "content" not in df.columns and not df.empty:
        df["content"] = None
    new_row = pd.DataFrame([record])
    df = pd.concat([df, new_row], ignore_index=True)
    df.to_parquet(REPORTS_FILE, index=False)

    print(f"Added report: {record['title']} (report_id={record['report_id']})")
    print(f"File: {REPORTS_FILE}")


if __name__ == "__main__":
    main()
