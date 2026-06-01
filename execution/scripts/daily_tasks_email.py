#!/usr/bin/env python3
"""
Daily Tasks Email Summary

Generates and optionally sends a daily digest of your most urgent tasks
(overdue, due today, due this week). Suitable for cron.

Process:
1. Load tasks from $DATA_DIR/tasks/tasks.parquet
2. Filter and sort by due date (overdue, today, this week)
3. Generate plain text and HTML body
4. With --send: email via Resend/SendGrid/Mailgun (same env as newsletter).
   Without --send: print body to stdout (e.g. for Gmail MCP).

Usage:
  python execution/scripts/daily_tasks_email.py
  python execution/scripts/daily_tasks_email.py --send
  python execution/scripts/daily_tasks_email.py --send --to you@example.com

Cron (daily 7:00 AM): set DATA_DIR, DAILY_TASKS_TO_EMAIL (or use --to).
Env: For SendGrid use SENDGRID_API_KEY and SENDGRID_SENDER_EMAIL (or EMAIL_DELIVERY_API_KEY, NEWSLETTER_FROM_EMAIL).
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# Paths and config
REPO_ROOT = Path(__file__).parent.parent
_ATELES_ROOT = REPO_ROOT.parent
load_dotenv(_ATELES_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT))
from scripts.config import get_data_dir

DATA_DIR = get_data_dir()
TASKS_FILE = DATA_DIR / "tasks" / "tasks.parquet"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"

EMAIL_DELIVERY_API = os.getenv("EMAIL_DELIVERY_API", "sendgrid")
# Prefer SendGrid-specific env vars when using SendGrid
EMAIL_DELIVERY_API_KEY = os.getenv("EMAIL_DELIVERY_API_KEY") or os.getenv(
    "SENDGRID_API_KEY", ""
)
FROM_EMAIL = (
    os.getenv("DAILY_TASKS_FROM_EMAIL")
    or os.getenv("SENDGRID_SENDER_EMAIL")
    or os.getenv("NEWSLETTER_FROM_EMAIL", "newsletter@markmhendrickson.com")
)
DEFAULT_TO_EMAIL = os.getenv("DAILY_TASKS_TO_EMAIL", "")


def create_snapshot(df: pd.DataFrame) -> None:
    """Create timestamped snapshot before modifying tasks."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    snapshot_path = SNAPSHOTS_DIR / f"tasks-{timestamp}.parquet"
    df.to_parquet(snapshot_path, index=False)
    print(f"Created snapshot: {snapshot_path}", file=sys.stderr)


def generate_email_body(df: pd.DataFrame) -> str:
    """Generate email body from filtered and sorted tasks."""
    today = date.today()
    end_of_week = today + timedelta(days=(6 - today.weekday()))

    # Normalize dates
    for col in [
        "due_date",
        "start_date",
        "completed_date",
        "created_date",
        "updated_date",
    ]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date

    # Filter active tasks (pending, in_progress, blocked)
    active = df[df["status"].isin(["pending", "in_progress", "blocked"])]

    # Overdue: due_date < today
    overdue = active[active["due_date"].notna() & (active["due_date"] < today)]

    # Today: due_date == today
    today_mask = active["due_date"] == today
    section_today = active[today_mask]

    # Horizon: due_date in (today, end_of_week]
    horizon_mask = active["due_date"].notna() & (
        (active["due_date"] > today) & (active["due_date"] <= end_of_week)
    )
    section_horizon = active[horizon_mask]

    # Sort by due date
    def sort_df(d):
        if d.empty:
            return d
        return d.sort_values("due_date", ascending=True)

    section_today = sort_df(section_today)
    section_horizon = sort_df(section_horizon)
    overdue = sort_df(overdue)

    # Build email body
    lines = []
    lines.append(f"Daily Tasks Summary for {today.isoformat()}\n")

    def add_section(title, d):
        lines.append(title)
        if d.empty:
            lines.append("  (none)")
            lines.append("")
            return
        for _, r in d.iterrows():
            due = r["due_date"].isoformat() if pd.notna(r["due_date"]) else "none"
            desc = (r.get("description") or "").strip()
            base = f"- {r.get('title', '(no title)')} — due {due}"
            if desc:
                base += f" — {desc}"
            lines.append(base)
        lines.append("")

    add_section("Today:", section_today)
    add_section("This Week / Horizon:", section_horizon)
    add_section("Overdue:", overdue)

    return "\n".join(lines)


def generate_email_html(df: pd.DataFrame) -> str:
    """Generate HTML email body from same sections as plain text."""
    today = date.today()
    end_of_week = today + timedelta(days=(6 - today.weekday()))
    for col in [
        "due_date",
        "start_date",
        "completed_date",
        "created_date",
        "updated_date",
    ]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    active = df[df["status"].isin(["pending", "in_progress", "blocked"])]
    overdue = active[active["due_date"].notna() & (active["due_date"] < today)]
    section_today = active[active["due_date"] == today]
    horizon_mask = active["due_date"].notna() & (
        (active["due_date"] > today) & (active["due_date"] <= end_of_week)
    )
    section_horizon = active[horizon_mask]

    def sort_df(d):
        return d.sort_values("due_date", ascending=True) if not d.empty else d

    section_today = sort_df(section_today)
    section_horizon = sort_df(section_horizon)
    overdue = sort_df(overdue)

    def section_html(title: str, d: pd.DataFrame) -> str:
        lines = [f"<h3>{title}</h3>", "<ul>"]
        if d.empty:
            lines.append("<li>(none)</li>")
        else:
            for _, r in d.iterrows():
                due = r["due_date"].isoformat() if pd.notna(r["due_date"]) else "none"
                desc = (r.get("description") or "").strip()
                line = (
                    f"<li><strong>{r.get('title', '(no title)')}</strong> — due {due}"
                )
                if desc:
                    line += f" — {desc}"
                line += "</li>"
                lines.append(line)
        lines.append("</ul>")
        return "\n".join(lines)

    parts = [
        f"<p>Daily Tasks Summary for <strong>{today.isoformat()}</strong></p>",
        section_html("Today", section_today),
        section_html("This Week / Horizon", section_horizon),
        section_html("Overdue", overdue),
    ]
    return "<html><body>" + "\n".join(parts) + "</body></html>"


def _send_resend(to_email: str, subject: str, html: str, text: str) -> bool:
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {EMAIL_DELIVERY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": FROM_EMAIL,
                "to": [to_email],
                "subject": subject,
                "html": html,
                "text": text,
            },
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending email: {e}", file=sys.stderr)
        return False


def _send_sendgrid(to_email: str, subject: str, html: str, text: str) -> bool:
    try:
        r = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {EMAIL_DELIVERY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "personalizations": [{"to": [{"email": to_email}]}],
                "from": {"email": FROM_EMAIL},
                "subject": subject,
                "content": [
                    {"type": "text/html", "value": html},
                    {"type": "text/plain", "value": text},
                ],
            },
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending email: {e}", file=sys.stderr)
        return False


def _send_mailgun(to_email: str, subject: str, html: str, text: str) -> bool:
    try:
        domain = FROM_EMAIL.split("@")[1]
        r = requests.post(
            f"https://api.mailgun.net/v3/{domain}/messages",
            auth=("api", EMAIL_DELIVERY_API_KEY),
            data={
                "from": FROM_EMAIL,
                "to": to_email,
                "subject": subject,
                "html": html,
                "text": text,
            },
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending email: {e}", file=sys.stderr)
        return False


def send_daily_digest(to_email: str, text_body: str, html_body: str) -> bool:
    """Send the digest to to_email using configured provider."""
    if not EMAIL_DELIVERY_API_KEY:
        print("Error: EMAIL_DELIVERY_API_KEY not set", file=sys.stderr)
        return False
    subject = f"Daily Tasks Summary — {date.today().isoformat()}"
    sender = {
        "resend": _send_resend,
        "sendgrid": _send_sendgrid,
        "mailgun": _send_mailgun,
    }.get(EMAIL_DELIVERY_API.lower())
    if not sender:
        print(
            f"Error: unknown EMAIL_DELIVERY_API={EMAIL_DELIVERY_API}", file=sys.stderr
        )
        return False
    return sender(to_email, subject, html_body, text_body)


def main():
    """Main execution: load, generate, optionally send."""
    parser = argparse.ArgumentParser(description="Daily tasks summary (print or email)")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Send email via EMAIL_DELIVERY_API; requires --to or DAILY_TASKS_TO_EMAIL",
    )
    parser.add_argument(
        "--to",
        default=DEFAULT_TO_EMAIL or None,
        metavar="EMAIL",
        help="Recipient (default: DAILY_TASKS_TO_EMAIL)",
    )
    args = parser.parse_args()

    if not TASKS_FILE.exists():
        print(f"Error: Tasks file not found: {TASKS_FILE}", file=sys.stderr)
        sys.exit(1)

    df = pd.read_parquet(TASKS_FILE)
    text_body = generate_email_body(df)
    html_body = generate_email_html(df)

    if args.send:
        to_email = (args.to or "").strip()
        if not to_email:
            print(
                "Error: --send requires --to or DAILY_TASKS_TO_EMAIL",
                file=sys.stderr,
            )
            sys.exit(1)
        if send_daily_digest(to_email, text_body, html_body):
            sys.exit(0)
        sys.exit(1)

    print(text_body)


if __name__ == "__main__":
    main()
