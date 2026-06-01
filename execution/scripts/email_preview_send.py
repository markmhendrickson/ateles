#!/usr/bin/env python3
"""
Send a single test email via Resend/SendGrid/Mailgun for client preview.

Use to see how a draft renders in real clients (Gmail, Outlook, Apple Mail).
Send to your own address, then open the message in each client (or on different
devices). Uses same env as newsletter: EMAIL_DELIVERY_API, EMAIL_DELIVERY_API_KEY,
NEWSLETTER_FROM_EMAIL (or EMAIL_PREVIEW_FROM_EMAIL).

Usage:
  python execution/scripts/email_preview_send.py --to you@example.com --subject "Preview" --html-file draft.html
  python execution/scripts/email_preview_send.py --to you@example.com --subject "Preview" --html-file draft.html --text-file draft.txt
"""

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

_repo_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_repo_root / ".env")

EMAIL_DELIVERY_API = os.getenv("EMAIL_DELIVERY_API", "resend")
EMAIL_DELIVERY_API_KEY = os.getenv("EMAIL_DELIVERY_API_KEY", "")
FROM_EMAIL = os.getenv("EMAIL_PREVIEW_FROM_EMAIL") or os.getenv(
    "NEWSLETTER_FROM_EMAIL", "newsletter@markmhendrickson.com"
)


def send_resend(to: str, subject: str, html: str, text: str) -> bool:
    r = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {EMAIL_DELIVERY_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": FROM_EMAIL,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        },
        timeout=10,
    )
    r.raise_for_status()
    return True


def send_sendgrid(to: str, subject: str, html: str, text: str) -> bool:
    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {EMAIL_DELIVERY_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [{"to": [{"email": to}]}],
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


def send_mailgun(to: str, subject: str, html: str, text: str) -> bool:
    domain = FROM_EMAIL.split("@")[1]
    r = requests.post(
        f"https://api.mailgun.net/v3/{domain}/messages",
        auth=("api", EMAIL_DELIVERY_API_KEY),
        data={
            "from": FROM_EMAIL,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        },
        timeout=10,
    )
    r.raise_for_status()
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Send one test email for client preview (Gmail, Outlook, etc.)"
    )
    parser.add_argument(
        "--to", required=True, help="Preview recipient (e.g. your address)"
    )
    parser.add_argument("--subject", required=True, help="Subject line")
    parser.add_argument(
        "--html-file", required=True, type=Path, help="Path to HTML body"
    )
    parser.add_argument(
        "--text-file", type=Path, help="Path to plain-text body (optional)"
    )
    args = parser.parse_args()

    if not EMAIL_DELIVERY_API_KEY:
        print("Error: EMAIL_DELIVERY_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    html_path = (
        args.html_file if args.html_file.is_absolute() else _repo_root / args.html_file
    )
    if not html_path.exists():
        print(f"Error: HTML file not found: {html_path}", file=sys.stderr)
        sys.exit(1)

    html = html_path.read_text(encoding="utf-8")
    text = html
    if args.text_file:
        tp = (
            args.text_file
            if args.text_file.is_absolute()
            else _repo_root / args.text_file
        )
        if tp.exists():
            text = tp.read_text(encoding="utf-8")

    senders = {
        "resend": send_resend,
        "sendgrid": send_sendgrid,
        "mailgun": send_mailgun,
    }
    if EMAIL_DELIVERY_API not in senders:
        print(
            f"Error: EMAIL_DELIVERY_API must be one of {list(senders.keys())}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        senders[EMAIL_DELIVERY_API](args.to, args.subject, html, text)
        print(
            f"Preview sent to {args.to} via {EMAIL_DELIVERY_API}. Open in Gmail/Outlook/Apple Mail to check rendering."
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
