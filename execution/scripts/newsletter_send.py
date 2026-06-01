#!/usr/bin/env python3
"""
Newsletter Send Script

Sends newsletter issues to all subscribed recipients via email delivery API.
Sovereignty-aligned: Uses user-owned subscriber database.

Usage:
    python newsletter_send.py --issue 1 --subject "Welcome to Newsletter" --html-file newsletter_issue_1.html
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

# Add parent directory to path
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(_repo_root / ".env")

# Configuration
EMAIL_DELIVERY_API = os.getenv("EMAIL_DELIVERY_API", "resend")
EMAIL_DELIVERY_API_KEY = os.getenv("EMAIL_DELIVERY_API_KEY", "")
FROM_EMAIL = os.getenv("NEWSLETTER_FROM_EMAIL", "newsletter@markmhendrickson.com")
NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Mark Hendrickson Newsletter")
DB_PATH = Path(os.getenv("NEWSLETTER_DB_PATH", "data/newsletter_subscribers.json"))


def fetch_subscribers_from_api(api_url: str, api_key: str) -> list[dict]:
    """Fetch subscribed recipients from newsletter API."""
    if not api_url or not api_key:
        print(
            "Error: NEWSLETTER_SUBSCRIBERS_API_URL and NEWSLETTER_ADMIN_API_KEY required",
            file=sys.stderr,
        )
        return []

    try:
        response = requests.get(
            api_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        response.raise_for_status()
        subscribers = response.json()
        if not isinstance(subscribers, list):
            return []
        return [s for s in subscribers if s.get("status") == "subscribed"]
    except Exception as e:
        print(f"Error fetching subscribers from API: {e}", file=sys.stderr)
        return []


def load_subscribers(
    subscribers_api_url: str | None = None,
    admin_api_key: str | None = None,
) -> list[dict]:
    """Load subscribed recipients from database or API."""
    api_url = subscribers_api_url or os.getenv("NEWSLETTER_SUBSCRIBERS_API_URL", "")
    api_key = admin_api_key or os.getenv("NEWSLETTER_ADMIN_API_KEY", "")
    if api_url:
        return fetch_subscribers_from_api(api_url, api_key)

    db_path = _repo_root / DB_PATH if not DB_PATH.is_absolute() else DB_PATH
    if not db_path.exists():
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        return []

    with open(db_path) as f:
        subscribers = json.load(f)

    return [s for s in subscribers if s.get("status") == "subscribed"]


def send_email_via_resend(
    to_email: str, subject: str, html_content: str, text_content: str
) -> bool:
    """Send email via Resend API."""
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {EMAIL_DELIVERY_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "from": FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html_content,
        "text": text_content,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending to {to_email}: {e}", file=sys.stderr)
        return False


def send_email_via_sendgrid(
    to_email: str, subject: str, html_content: str, text_content: str
) -> bool:
    """Send email via SendGrid API."""
    url = "https://api.sendgrid.com/v3/mail/send"
    headers = {
        "Authorization": f"Bearer {EMAIL_DELIVERY_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": FROM_EMAIL},
        "subject": subject,
        "content": [
            {"type": "text/html", "value": html_content},
            {"type": "text/plain", "value": text_content},
        ],
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending to {to_email}: {e}", file=sys.stderr)
        return False


def send_email_via_mailgun(
    to_email: str, subject: str, html_content: str, text_content: str
) -> bool:
    """Send email via Mailgun API."""
    domain = FROM_EMAIL.split("@")[1]
    url = f"https://api.mailgun.net/v3/{domain}/messages"

    payload = {
        "from": FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html_content,
        "text": text_content,
    }

    try:
        response = requests.post(
            url, auth=("api", EMAIL_DELIVERY_API_KEY), data=payload, timeout=10
        )
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending to {to_email}: {e}", file=sys.stderr)
        return False


def send_newsletter(
    issue_number: str,
    subject: str,
    html_file: Path,
    text_file: Path | None = None,
    subscribers_api_url: str | None = None,
    admin_api_key: str | None = None,
    preview_to: str | None = None,
) -> dict:
    """Send newsletter to all subscribed recipients, or to one address if preview_to is set."""
    if not EMAIL_DELIVERY_API_KEY:
        return {"error": "EMAIL_DELIVERY_API_KEY not set"}, 1

    # Load HTML content
    if not html_file.exists():
        return {"error": f"HTML file not found: {html_file}"}, 1

    with open(html_file) as f:
        html_content = f.read()

    # Load text content (if provided)
    text_content = html_content  # Fallback to HTML if no text version
    if text_file and text_file.exists():
        with open(text_file) as f:
            text_content = f.read()

    # Preview mode: send only to one address for client preview (Gmail, Outlook, etc.)
    if preview_to:
        subscribers = [{"email": preview_to}]
        print(f"Preview mode: sending issue {issue_number} to {preview_to} only.")
    else:
        # Load subscribers
        subscribers = load_subscribers(subscribers_api_url, admin_api_key)
        if not subscribers:
            return {"error": "No subscribed recipients found"}, 1
        print(
            f"Sending newsletter issue {issue_number} to {len(subscribers)} recipients..."
        )

    # Send to each subscriber
    success_count = 0
    failure_count = 0

    for subscriber in subscribers:
        email = subscriber["email"]

        # Add unsubscribe link to HTML
        unsubscribe_url = f"https://markmhendrickson.com/newsletter/unsubscribe?email={email}&token=TOKEN"
        html_with_unsubscribe = html_content.replace(
            "{{UNSUBSCRIBE_URL}}", unsubscribe_url
        )

        # Send email
        if EMAIL_DELIVERY_API == "resend":
            success = send_email_via_resend(
                email, subject, html_with_unsubscribe, text_content
            )
        elif EMAIL_DELIVERY_API == "sendgrid":
            success = send_email_via_sendgrid(
                email, subject, html_with_unsubscribe, text_content
            )
        elif EMAIL_DELIVERY_API == "mailgun":
            success = send_email_via_mailgun(
                email, subject, html_with_unsubscribe, text_content
            )
        else:
            print(f"Error: Unknown email API '{EMAIL_DELIVERY_API}'", file=sys.stderr)
            success = False

        if success:
            success_count += 1
            print(f"✓ Sent to {email}")
        else:
            failure_count += 1
            print(f"✗ Failed to send to {email}")

    return {
        "success": True,
        "issue_number": issue_number,
        "total_recipients": len(subscribers),
        "success_count": success_count,
        "failure_count": failure_count,
    }, 0


def main():
    parser = argparse.ArgumentParser(description="Send newsletter to subscribers")
    parser.add_argument(
        "--issue", required=True, help='Issue number (e.g., "1", "2026-01")'
    )
    parser.add_argument("--subject", required=True, help="Email subject line")
    parser.add_argument(
        "--html-file", required=True, type=Path, help="Path to HTML newsletter file"
    )
    parser.add_argument(
        "--text-file", type=Path, help="Path to text newsletter file (optional)"
    )
    parser.add_argument(
        "--subscribers-api-url",
        type=str,
        help="Fetch subscribers from this URL (overrides NEWSLETTER_SUBSCRIBERS_API_URL)",
    )
    parser.add_argument(
        "--preview-to",
        type=str,
        metavar="EMAIL",
        help="Send only to this address (for client preview: open in Gmail, Outlook, Apple Mail)",
    )

    args = parser.parse_args()

    result, exit_code = send_newsletter(
        args.issue,
        args.subject,
        args.html_file,
        args.text_file,
        subscribers_api_url=args.subscribers_api_url,
        preview_to=args.preview_to,
    )

    print(json.dumps(result, indent=2))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
