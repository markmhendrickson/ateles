#!/usr/bin/env python3
"""
Fetch last 7 days of Google Analytics performance via the Data API.

Uses the same credentials as the Google Analytics MCP (.env and
GOOGLE_APPLICATION_CREDENTIALS or .creds/google-analytics-credentials.json).

Usage:
  python execution/scripts/ga_last_7_days_report.py PROPERTY_ID
  python execution/scripts/ga_last_7_days_report.py 123456789

Install dependency (if not already installed):
  pip install google-analytics-data
"""

import sys
from pathlib import Path

# Repo root and .env
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if (REPO_ROOT / ".env").exists():
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
if not sys.path or REPO_ROOT != Path(sys.path[0]).resolve():
    sys.path.insert(0, str(REPO_ROOT))

import os

if (
    not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    and (REPO_ROOT / ".creds" / "google-analytics-credentials.json").exists()
):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(
        REPO_ROOT / ".creds" / "google-analytics-credentials.json"
    )

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)


def run_last_7_days_report(property_id: str) -> None:
    """Run a performance report for the last 7 days."""
    # Normalize property ID (strip properties/ prefix for display)
    prop = property_id.strip()
    if prop.isdigit():
        property_rn = f"properties/{prop}"
    elif prop.startswith("properties/"):
        property_rn = prop
    else:
        property_rn = f"properties/{prop}"

    client = BetaAnalyticsDataClient()
    request = RunReportRequest(
        property=property_rn,
        dimensions=[Dimension(name="date")],
        metrics=[
            Metric(name="sessions"),
            Metric(name="totalUsers"),
            Metric(name="screenPageViews"),
            Metric(name="engagementRate"),
            Metric(name="averageSessionDuration"),
        ],
        date_ranges=[
            DateRange(start_date="7daysAgo", end_date="today"),
        ],
    )
    response = client.run_report(request)

    # Header
    dim_headers = [h.name for h in response.dimension_headers]
    metric_headers = [h.name for h in response.metric_headers]
    headers = dim_headers + metric_headers
    col_widths = [max(len(h), 10) for h in headers]
    for i, row in enumerate(response.rows):
        for j, dv in enumerate(row.dimension_values):
            col_widths[j] = max(col_widths[j], len(dv.value or ""))
        for j, mv in enumerate(row.metric_values):
            col_widths[len(dim_headers) + j] = max(
                col_widths[len(dim_headers) + j], len(mv.value or "")
            )

    sep = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    print(sep)
    print("-" * len(sep))

    # Rows
    for row in response.rows:
        parts = []
        for i, dv in enumerate(row.dimension_values):
            parts.append((dv.value or "").ljust(col_widths[i]))
        for i, mv in enumerate(row.metric_values):
            parts.append((mv.value or "").ljust(col_widths[len(dim_headers) + i]))
        print("  ".join(parts))

    # Totals (if present)
    if response.totals:
        for total_row in response.totals:
            parts = []
            for i in range(len(dim_headers)):
                parts.append(("(total)" if i == 0 else "").ljust(col_widths[i]))
            for i, mv in enumerate(total_row.metric_values):
                parts.append((mv.value or "").ljust(col_widths[len(dim_headers) + i]))
            print("  ".join(parts))

    print(f"\nTotal rows: {response.row_count}")


def _normalize_page_path(path: str) -> str:
    """Strip trailing slash so /foo/ and /foo aggregate together."""
    p = (path or "").strip()
    return p.rstrip("/") or "/"


def run_top_pages_report(
    property_id: str,
    limit: int = 20,
    row_limit: int = 500,
    sort_by: str = "views",
    days: int = 7,
) -> None:
    """Run a report of top pages for the last N days.
    Paths with and without trailing slash are aggregated (counted together).
    sort_by: "views" (screenPageViews) or "users" (totalUsers).
    days: number of days (e.g. 7 for week, 30 for month).
    totalUsers is summed per normalized path; same user visiting both URL forms may be counted twice.
    """
    prop = property_id.strip()
    if prop.isdigit():
        property_rn = f"properties/{prop}"
    elif prop.startswith("properties/"):
        property_rn = prop
    else:
        property_rn = f"properties/{prop}"

    client = BetaAnalyticsDataClient()
    request = RunReportRequest(
        property=property_rn,
        dimensions=[
            Dimension(name="pagePath"),
            Dimension(name="pageTitle"),
        ],
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="sessions"),
            Metric(name="totalUsers"),
        ],
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        limit=row_limit,
    )
    response = client.run_report(request)

    # 1) Aggregate by normalized path (slash and no-slash counted together)
    by_path: dict[str, tuple[str, int, int, int]] = {}
    for row in response.rows:
        path = row.dimension_values[0].value or ""
        title = row.dimension_values[1].value or ""
        views = int(row.metric_values[0].value or 0)
        sessions = int(row.metric_values[1].value or 0)
        users = int(row.metric_values[2].value or 0)
        key = _normalize_page_path(path)
        if key in by_path:
            prev_title, pv, se, u = by_path[key]
            by_path[key] = (prev_title, pv + views, se + sessions, u + users)
        else:
            by_path[key] = (title, views, sessions, users)

    # 2) Aggregate by page title (same title = one row, summed metrics; keep representative path)
    by_title: dict[
        str, tuple[str, int, int, int, int]
    ] = {}  # title -> (best_path, best_path_views, sum_pv, sum_se, sum_u)
    for path, (title, pv, se, u) in by_path.items():
        if title not in by_title:
            by_title[title] = (path, pv, pv, se, u)
        else:
            bp, bpv, sp, ss, su = by_title[title]
            if pv > bpv:
                bp, bpv = path, pv
            by_title[title] = (bp, bpv, sp + pv, ss + se, su + u)

    # Sort by screenPageViews or totalUsers descending, take top `limit`
    # Row: (title, path, pv, se, u) -> sort_idx 2=pv, 4=u
    sort_idx = 4 if sort_by == "users" else 2
    sorted_rows = sorted(
        [(title, path, pv, se, u) for title, (path, _, pv, se, u) in by_title.items()],
        key=lambda x: x[sort_idx],
        reverse=True,
    )[:limit]

    if sort_by == "users":
        headers = ["pageTitle", "pagePath", "totalUsers", "screenPageViews", "sessions"]
    else:
        headers = ["pageTitle", "pagePath", "screenPageViews", "sessions", "totalUsers"]
    col_widths = [max(len(h), 12) for h in headers]
    for title, path, pv, se, u in sorted_rows:
        col_widths[0] = max(col_widths[0], min(len(title), 60))
        col_widths[1] = max(col_widths[1], min(len(path), 60))
        col_widths[2] = max(col_widths[2], len(str(u if sort_by == "users" else pv)))
        col_widths[3] = max(col_widths[3], len(str(pv if sort_by == "users" else se)))
        col_widths[4] = max(col_widths[4], len(str(se if sort_by == "users" else u)))

    sep = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    print(sep)
    print("-" * len(sep))
    for title, path, pv, se, u in sorted_rows:
        title_show = title[:60].ljust(col_widths[0])
        path_show = path[:60].ljust(col_widths[1])
        if sort_by == "users":
            print(
                "  ".join(
                    [
                        title_show,
                        path_show,
                        str(u).ljust(col_widths[2]),
                        str(pv).ljust(col_widths[3]),
                        str(se).ljust(col_widths[4]),
                    ]
                )
            )
        else:
            print(
                "  ".join(
                    [
                        title_show,
                        path_show,
                        str(pv).ljust(col_widths[2]),
                        str(se).ljust(col_widths[3]),
                        str(u).ljust(col_widths[4]),
                    ]
                )
            )
    print(
        f"\nTotal rows (aggregated): {len(sorted_rows)} (by title, slash/no-slash combined, last {days} days)"
    )


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: ga_last_7_days_report.py PROPERTY_ID [pages] [users]",
            file=sys.stderr,
        )
        print(
            "  PROPERTY_ID: GA4 property ID (numeric, e.g. 123456789)", file=sys.stderr
        )
        print(
            "  pages: output top pages (default: sorted by page views)", file=sys.stderr
        )
        print(
            "  users: with 'pages', sort by total users instead of page views",
            file=sys.stderr,
        )
        sys.exit(1)
    if len(sys.argv) > 2 and sys.argv[2].lower() == "pages":
        sort_by = (
            "users" if len(sys.argv) > 3 and sys.argv[3].lower() == "users" else "views"
        )
        days = 7
        if len(sys.argv) > 3 and sys.argv[3].isdigit():
            days = int(sys.argv[3])
        elif len(sys.argv) > 4 and sys.argv[4].isdigit():
            days = int(sys.argv[4])
        run_top_pages_report(sys.argv[1], sort_by=sort_by, days=days)
    else:
        run_last_7_days_report(sys.argv[1])


if __name__ == "__main__":
    main()
