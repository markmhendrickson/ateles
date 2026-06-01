#!/usr/bin/env python3
"""
Fetch To: recipients from sent Gmail messages for newsletter recipient analysis.
Uses Gmail API with OAuth from .creds/gmail-credentials.json.
Outputs to data/tmp/sent_mail_recipients.json

Usage:
  python fetch_sent_mail_recipients.py           # all sent mail
  python fetch_sent_mail_recipients.py --limit 100  # first 100 messages (for testing)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

# Credential paths - match Gmail MCP (run-gmail-mcp.sh)
CREDS_DIR = REPO_ROOT / ".creds"
TOKEN_FILE = CREDS_DIR / "gmail-credentials.json"  # OAuth tokens after auth
CLIENT_SECRETS = CREDS_DIR / "gcp-oauth.keys.json"  # OAuth client keys for re-auth

OUTPUT_FILE = REPO_ROOT / "data" / "tmp" / "sent_mail_recipients.json"


def parse_to_header(to_header: str) -> list[tuple[str, str]]:
    """Parse To header into (email, name) pairs. Handles 'Name <email>', 'email', comma-separated."""
    if not to_header or not to_header.strip():
        return []
    results = []
    # Split by comma but respect commas inside angle brackets
    parts = re.split(r",\s*(?![^<]*>)", to_header)
    for part in parts:
        part = part.strip()
        match = re.search(r"<([^>]+)>", part)
        if match:
            email = match.group(1).strip().lower()
            name = part[: match.start()].strip().strip('"')
            results.append((email, name))
        elif "@" in part and re.match(r"^[\w\.+-]+@[\w\.-]+\.\w+$", part):
            results.append((part.lower(), ""))
    return results


def main():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:
        print(
            "Install dependencies: pip install google-api-python-client google-auth-oauthlib"
        )
        raise SystemExit(1) from e

    # Use modify scope to match Gmail MCP token (includes read)
    SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

    creds = None
    if TOKEN_FILE.exists() and CLIENT_SECRETS.exists():
        try:
            token_data = json.loads(TOKEN_FILE.read_text())
            client_data = json.loads(CLIENT_SECRETS.read_text())
            installed = client_data.get("installed") or client_data.get("web", {})
            creds = Credentials(
                token=token_data.get("access_token"),
                refresh_token=token_data.get("refresh_token"),
                token_uri=installed.get(
                    "token_uri", "https://oauth2.googleapis.com/token"
                ),
                client_id=installed.get("client_id"),
                client_secret=installed.get("client_secret"),
                scopes=SCOPES,
            )
        except Exception as e:
            print(f"Warning: Could not load credentials: {e}", file=sys.stderr)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif CLIENT_SECRETS.exists():
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRETS), SCOPES
            )
            creds = flow.run_local_server(port=0)
        else:
            print("No valid credentials. Run: mcp/gmail/run-gmail-mcp.sh auth")
            raise SystemExit(1)
        if TOKEN_FILE:
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--limit", type=int, default=0, help="Max messages to process (0=all)"
    )
    args = parser.parse_args()

    service = build("gmail", "v1", credentials=creds)
    recipients: dict[str, dict] = {}  # email -> {name, count}
    to_headers: list[str | None] = []  # collect from batch callbacks

    def add_to_callback(_req_id: str, resp: dict | None, err: Exception | None) -> None:
        if err:
            return
        headers = (resp or {}).get("payload", {}).get("headers", [])
        to_header = next(
            (h["value"] for h in headers if h["name"].lower() == "to"), None
        )
        to_headers.append(to_header)

    page_token = None
    total = 0
    while True:
        result = (
            service.users()
            .messages()
            .list(
                userId="me",
                q="from:me after:2024/02/09",
                maxResults=500,
                pageToken=page_token,
            )
            .execute()
        )
        messages = result.get("messages", [])
        if not messages:
            break
        if args.limit and total >= args.limit:
            break
        if args.limit:
            messages = messages[: args.limit - total]
        # Batch fetch metadata (max 100 per batch)
        for i in range(0, len(messages), 100):
            batch = service.new_batch_http_request(callback=add_to_callback)
            for msg in messages[i : i + 100]:
                batch.add(
                    service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=msg["id"],
                        format="metadata",
                        metadataHeaders=["To"],
                    )
                )
            batch.execute()
        for to_header in to_headers:
            if not to_header:
                continue
            for email, name in parse_to_header(to_header):
                if not email or "@" not in email:
                    continue
                if email not in recipients:
                    recipients[email] = {"name": name or "", "count": 0}
                recipients[email]["count"] += 1
            total += 1
        to_headers.clear()
        if total % 100 == 0 and total > 0:
            print(f"  ... processed {total} messages", file=sys.stderr)
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"recipients": recipients, "messages_processed": total}, f, indent=2)
    print(
        f"Processed {total} sent messages, {len(recipients)} unique recipients -> {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
