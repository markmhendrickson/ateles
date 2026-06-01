#!/usr/bin/env python3
"""
Download Legrand/Netatmo installation documents from Gmail.
Extracts device specifications from invoices and purchase orders.
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
    """Download Legrand/Netatmo installation documents."""
    # Message IDs for key documents
    documents = [
        {
            "msg_id": "17d76a17d5e22e31",  # XEDEX Cert 20 - Invoice 148
            "description": "XEDEX Certification 20 - Invoice 148 (Dec 2021)",
            "target_files": ["3330 _ Certificación 20 _ Partidas.pdf"],
        },
        {
            "msg_id": "17ce16638dc6105a",  # XEDEX Cert 19 - Invoice 130
            "description": "XEDEX Certification 19 - Invoice 130 (Nov 2021)",
            "target_files": ["3330 _ Certificación 19 _ Partidas.pdf"],
        },
        {
            "msg_id": "17d5bb321335324d",  # PC 28 mecanismos inteligentes
            "description": "PC 28 Mecanismos Inteligentes (Nov 2021)",
            "target_files": [
                "PC28_Mecanismos inteligentes.pdf",
                "PC28.1_Mecanismos inteligentes.pdf",
            ],
        },
        {
            "msg_id": "18badd1e2089f57e",  # GRUPO KIAK PR-23-227
            "description": "GRUPO KIAK Budget PR-23-227 (Nov 2023)",
            "target_files": ["PR-23-227 MARK HENDRICKSON.PDF"],
        },
    ]

    service = get_gmail_service()
    output_dir = PROJECT_ROOT / "operations" / "admin" / "legrand-netatmo-docs"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Downloading Legrand/Netatmo installation documents...\n")

    for doc in documents:
        print(f"\n{'=' * 60}")
        print(f"Processing: {doc['description']}")
        print(f"Message ID: {doc['msg_id']}")
        print(f"{'=' * 60}")

        attachments = get_attachments(service, doc["msg_id"])

        if attachments:
            print(f"Found {len(attachments)} attachment(s):")
            for att in attachments:
                print(f"  - {att['filename']} ({att['mimeType']}, {att['size']} bytes)")

                # Download if it's a target file or if no specific targets specified
                if (
                    not doc.get("target_files")
                    or att["filename"] in doc["target_files"]
                ):
                    download_attachment(
                        service,
                        doc["msg_id"],
                        att["attachmentId"],
                        att["filename"],
                        output_dir,
                    )
        else:
            print("  No attachments found")

    print(f"\n\nDocuments downloaded to: {output_dir}")
    print(
        "\nNext step: Extract text from PDFs using pdftotext to find device model numbers."
    )


if __name__ == "__main__":
    main()
