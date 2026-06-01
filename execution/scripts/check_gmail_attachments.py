#!/usr/bin/env python3
"""
Check and download attachments from Gmail emails.
"""

import base64
import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

PROJECT_ROOT = Path(__file__).parent.parent.parent
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_gmail_service():
    """Get authenticated Gmail service."""
    creds = None

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as token:
            creds = Credentials.from_authorized_user_info(json.load(token))

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_attachments(service, message_id):
    """Get attachment information from a message."""
    try:
        message = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = message.get("payload", {})

        attachments_info = []

        def extract_attachments(parts):
            """Recursively extract attachments from message parts."""
            attachments = []
            for part in parts:
                if part.get("filename"):
                    attachment_id = part.get("body", {}).get("attachmentId")
                    if attachment_id:
                        attachments.append(
                            {
                                "filename": part.get("filename"),
                                "mimeType": part.get("mimeType"),
                                "size": part.get("body", {}).get("size", 0),
                                "attachmentId": attachment_id,
                            }
                        )
                if "parts" in part:
                    attachments.extend(extract_attachments(part["parts"]))
            return attachments

        if "parts" in payload:
            attachments_info = extract_attachments(payload["parts"])
        elif payload.get("filename"):
            attachment_id = payload.get("body", {}).get("attachmentId")
            if attachment_id:
                attachments_info.append(
                    {
                        "filename": payload.get("filename"),
                        "mimeType": payload.get("mimeType"),
                        "size": payload.get("body", {}).get("size", 0),
                        "attachmentId": attachment_id,
                    }
                )

        return attachments_info
    except Exception as e:
        print(f"Error getting attachments for message {message_id}: {e}")
        return []


def download_attachment(service, message_id, attachment_id, filename, output_dir):
    """Download an attachment from a message."""
    try:
        attachment = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )

        file_data = base64.urlsafe_b64decode(attachment["data"])
        output_path = output_dir / filename

        with open(output_path, "wb") as f:
            f.write(file_data)

        print(f"Downloaded: {filename} ({len(file_data)} bytes)")
        return output_path
    except Exception as e:
        print(f"Error downloading attachment {filename}: {e}")
        return None


def main():
    """Check attachments in invoice emails."""
    # Invoice message IDs
    invoice_ids = [
        "19a1f5fcd4f42c06",  # Invoice 4281044000204116001 - Oct 26
        "19a05685c1798b81",  # Invoice 4281044000204116001 - Oct 20
        "199eba9402b1a566",  # Invoice 4281044000204116001 - Oct 15
        "199e2899373846b4",  # Invoice 4281044000208048141 - Oct 14
    ]

    service = get_gmail_service()
    from scripts.config import get_data_dir

    output_dir = get_data_dir() / "attachments" / "online_taxman"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Checking attachments in {len(invoice_ids)} invoice emails...\n")

    for msg_id in invoice_ids:
        print(f"\nMessage ID: {msg_id}")
        attachments = get_attachments(service, msg_id)

        if attachments:
            print(f"Found {len(attachments)} attachment(s):")
            for att in attachments:
                print(f"  - {att['filename']} ({att['mimeType']}, {att['size']} bytes)")
                download_attachment(
                    service, msg_id, att["attachmentId"], att["filename"], output_dir
                )
        else:
            print("  No attachments found")


if __name__ == "__main__":
    main()
