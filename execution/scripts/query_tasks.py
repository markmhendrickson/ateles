#!/usr/bin/env python3
"""
Query Tasks - View and Filter Tasks by Due Date and Priority

Provides analytical views of tasks organized by due date.
Supports filtering by domain, status, and other attributes.

Usage:
    python query_tasks.py [view] [--domain DOMAIN] [--status STATUS] [--limit N]

Views:
    today           - Tasks due today or overdue
    this_week       - Tasks due this week
    high_benefit    - Tasks due later than 30 days or undated (backlog)
    domain          - Tasks by domain (requires --domain)
    all             - All active tasks sorted by due date

Examples:
    python query_tasks.py today
    python query_tasks.py this_week --status pending
    python query_tasks.py high_benefit
    python query_tasks.py domain --domain finance
    python query_tasks.py all --limit 50
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configuration
from scripts.config import DATA_DIR

TASKS_FILE = DATA_DIR / "tasks" / "tasks.parquet"


class TaskQueryEngine:
    """Query and filter tasks with various views."""

    STATUS_ORDER = ["in_progress", "pending", "blocked", "completed", "canceled"]

    def __init__(self):
        if not TASKS_FILE.exists():
            raise FileNotFoundError(f"Tasks file not found: {TASKS_FILE}")

        self.df = pd.read_parquet(TASKS_FILE)

        # Convert date columns
        date_cols = [
            "due_date",
            "start_date",
            "completed_date",
            "created_date",
            "updated_date",
            "import_date",
        ]
        for col in date_cols:
            if col in self.df.columns:
                self.df[col] = pd.to_datetime(self.df[col]).dt.date

    def apply_filters(
        self, df: pd.DataFrame, domain: str | None = None, status: str | None = None
    ) -> pd.DataFrame:
        """Apply common filters to dataframe."""
        if domain:
            df = df[df["domain"] == domain]

        if status:
            df = df[df["status"] == status]

        return df

    def sort_by_due_date(self, df: pd.DataFrame) -> pd.DataFrame:
        """Sort by due date (soonest first)."""
        return df.sort_values(
            "due_date",
            ascending=True,
            na_position="last",
        )

    def format_task(self, task: pd.Series, show_full: bool = False) -> str:
        """Format a single task for display."""
        # Status indicator
        status_icons = {
            "pending": "⬜",
            "in_progress": "🔵",
            "blocked": "🔴",
            "completed": "✅",
            "canceled": "❌",
        }
        status_icon = status_icons.get(task["status"], "  ")

        # Due date formatting
        if pd.notna(task["due_date"]):
            try:
                due = task["due_date"]
                today = date.today()
                days_diff = (due - today).days

                if days_diff < 0:
                    due_str = f"⚠️  OVERDUE ({abs(days_diff)}d ago)"
                elif days_diff == 0:
                    due_str = "📅 TODAY"
                elif days_diff <= 3:
                    due_str = f"📅 in {days_diff}d"
                else:
                    due_str = f"📅 {due.strftime('%Y-%m-%d')}"
            except (TypeError, AttributeError):
                due_str = ""
        else:
            due_str = ""

        # Basic line
        line = f"{status_icon}  {task['title'][:80]}"

        if due_str:
            line += f" {due_str}"

        if show_full:
            line += f"\n    Domain: {task['domain']} | Status: {task['status']}"
            if pd.notna(task["description"]) and task["description"]:
                desc = task["description"][:200]
                line += f"\n    {desc}"
            if pd.notna(task["execution_plan_path"]) and task["execution_plan_path"]:
                line += f"\n    Plan: {task['execution_plan_path']}"

        return line

    def view_today(
        self,
        domain: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Tasks due today or overdue."""
        today = date.today()
        df = self.df.copy()
        df["due_date_parsed"] = pd.to_datetime(df["due_date"], errors="coerce").dt.date
        df = df[df["due_date_parsed"] <= today].drop(columns=["due_date_parsed"])

        # Filter to active tasks by default
        if status is None:
            df = df[df["status"].isin(["pending", "in_progress", "blocked"])]
        else:
            df = self.apply_filters(df, domain, status)

        df = self.sort_by_due_date(df)

        if limit:
            df = df.head(limit)

        return df

    def view_this_week(
        self,
        domain: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Tasks due this week (or overdue)."""
        today = date.today()
        end_of_week = today + timedelta(days=(6 - today.weekday()))
        df = self.df.copy()
        df["due_date_parsed"] = pd.to_datetime(df["due_date"], errors="coerce").dt.date
        mask = df["due_date_parsed"].notna() & (df["due_date_parsed"] <= end_of_week)
        df = df[mask].drop(columns=["due_date_parsed"], errors="ignore")

        # Filter to active tasks by default
        if status is None:
            df = df[df["status"].isin(["pending", "in_progress", "blocked"])]
        else:
            df = self.apply_filters(df, domain, status)

        df = self.sort_by_due_date(df)

        if limit:
            df = df.head(limit)

        return df

    def view_high_benefit(
        self,
        domain: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Backlog: tasks due later than 30 days or undated."""
        today = date.today()
        cutoff = today + timedelta(days=30)
        df = self.df.copy()
        df["due_date_parsed"] = pd.to_datetime(df["due_date"], errors="coerce").dt.date
        mask = df["due_date_parsed"].isna() | (df["due_date_parsed"] > cutoff)
        df = df[mask].drop(columns=["due_date_parsed"], errors="ignore")

        # Filter to active tasks by default
        if status is None:
            df = df[df["status"].isin(["pending", "in_progress", "blocked"])]
        else:
            df = self.apply_filters(df, domain, status)

        df = self.sort_by_due_date(df)

        if limit:
            df = df.head(limit)

        return df

    def view_by_domain(
        self, domain: str, status: str | None = None, limit: int | None = None
    ) -> pd.DataFrame:
        """All tasks in a specific domain."""
        df = self.df[self.df["domain"] == domain].copy()

        # Filter to active tasks by default
        if status is None:
            df = df[df["status"].isin(["pending", "in_progress", "blocked"])]
        else:
            df = self.apply_filters(df, None, status)

        df = self.sort_by_due_date(df)

        if limit:
            df = df.head(limit)

        return df

    def view_all(
        self,
        domain: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """All tasks, sorted by due date."""
        df = self.df.copy()

        # Filter to active tasks by default
        if status is None:
            df = df[df["status"].isin(["pending", "in_progress", "blocked"])]

        df = self.apply_filters(df, domain, status)
        df = self.sort_by_due_date(df)

        if limit:
            df = df.head(limit)

        return df

    def print_summary(self, df: pd.DataFrame, title: str):
        """Print a formatted summary of tasks."""
        print(f"\n{'=' * 80}")
        print(f"{title}")
        print(f"{'=' * 80}\n")

        if len(df) == 0:
            print("No tasks found.\n")
            return

        print(f"Total: {len(df)} tasks\n")

        # List individual tasks
        print("Tasks:")
        print("-" * 80)
        for idx, task in df.iterrows():
            print(self.format_task(task))

        print()

    def get_stats(self) -> dict:
        """Get overall task statistics."""
        active = self.df[self.df["status"].isin(["pending", "in_progress", "blocked"])]

        stats = {
            "total": len(self.df),
            "active": len(active),
            "completed": len(self.df[self.df["status"] == "completed"]),
            "by_domain": dict(active["domain"].value_counts()),
            "by_status": dict(self.df["status"].value_counts()),
        }

        return stats


def main():
    parser = argparse.ArgumentParser(description="Query and view tasks")
    parser.add_argument(
        "view",
        nargs="?",
        default="today",
        choices=["today", "this_week", "high_benefit", "domain", "all", "stats"],
        help="View to display",
    )
    parser.add_argument("--domain", type=str, help="Filter by domain")
    parser.add_argument("--status", type=str, help="Filter by status")
    parser.add_argument("--limit", type=int, help="Limit number of results")

    args = parser.parse_args()

    try:
        engine = TaskQueryEngine()

        if args.view == "stats":
            stats = engine.get_stats()
            print(f"\n{'=' * 80}")
            print("Task Statistics")
            print(f"{'=' * 80}\n")
            print(f"Total tasks: {stats['total']}")
            print(f"Active tasks: {stats['active']}")
            print(f"Completed tasks: {stats['completed']}")
            print("\nBy Domain:")
            for domain, count in sorted(
                stats["by_domain"].items(), key=lambda x: x[1], reverse=True
            ):
                print(f"  {domain:12s}: {count:4d}")
            print()
            return

        # Execute view
        if args.view == "today":
            df = engine.view_today(args.domain, args.status, args.limit)
            title = "Tasks Due Today"
        elif args.view == "this_week":
            df = engine.view_this_week(args.domain, args.status, args.limit)
            title = "Tasks Due This Week"
        elif args.view == "high_benefit":
            df = engine.view_high_benefit(args.domain, args.status, args.limit)
            title = (
                "High-Benefit Backlog (Critical/High Priority, Soon/Backlog Urgency)"
            )
        elif args.view == "domain":
            if not args.domain:
                print("Error: --domain required for domain view", file=sys.stderr)
                sys.exit(1)
            df = engine.view_by_domain(args.domain, args.status, args.limit)
            title = f"Tasks in Domain: {args.domain.title()}"
        elif args.view == "all":
            df = engine.view_all(args.domain, args.status, args.limit)
            title = "All Active Tasks"

        engine.print_summary(df, title)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
