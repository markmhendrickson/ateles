#!/usr/bin/env python3
"""
Download XEDEX certification Partidas PDFs using Gmail API.
Fixes the path length issue with MCP tool by using direct Gmail API.
"""

import base64
import json
import os
import sys
from pathlib import Path

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
except ImportError:
    print("Error: Missing Google API libraries.")
    print(
        "Install with: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client"
    )
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent.parent
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"
OUTPUT_DIR = PROJECT_ROOT / "operations" / "admin" / "legrand-netatmo-docs"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Message IDs and their corresponding Partidas PDF attachment IDs
ATTACHMENTS_TO_DOWNLOAD = [
    {
        "msg_id": "17d76a17d5e22e31",  # Cert 20
        "attachment_id": "ANGjdJ_p0_3YgxtZmeo0lLjs4QIkD2pAWoYe6bHBNcPIiaZklftwOPdJKv59lBdz1Z_ikGT3OebGRKnxIiqsYTHQelb0BLN0VIe94iMjri4XfOSMC8x7UcQTnG3733Y9ABx4CHmgCjhRcu9z3QzoRfbvkeioTdeRVKIq_vhSnnk6_3krWuikTSWrXYRp6Z_xmocNu26ydOiH4ULg8k5L_-QZz-zYXyEU9zdFH7nryKgVIKMsRbvRth8qj-5VzknH6gcbKTd-6Me_XoJGDTyfnkpiI4GNGYFTgLLqJwCmq7_qJUqQ9e1318cxXHhRhQfA4NOo965OYpbNidUBHjjBnjSdDNcw2VefDePtwVuTZbfRl1gy2f-YJnmgFPcSHez0Y_NFgqPsilSuYpxenHsm",
        "filename": "cert-20-partidas.pdf",
    },
    {
        "msg_id": "17ce16638dc6105a",  # Cert 19
        "attachment_id": "ANGjdJ-2rkaPrcgWpeevsfAGPLhwoeuqOCCIWUTjeGv2i62cfkFVK150gGm2t3G-TzoMdEtLK2tHQahpp4eaJj2YpdVTXsEM1s9IK-02x-QJeKXe1UPwqpuGWvPt0Gm6H6aSTm7IVi6fiBZHWWH0jB0i4WxdivIBx719KRg-bQK8lLn3uJ4fC5cPVtCp_FVcZF39_KXzqxtIZYwGftvTJCn8kR-c56nU2mdpta_n9m3P-cIqp3dvmH2QBsUPlaelas24ysY1x2dPP3IcdEClsFYDZxsi-YKXKS9mYwc8upBgyWt26tsKpu88SeRN40-MCB5hIW44AALI1j7mZIiWqN6XaYrljO4FBxTjZeLWQmN0PXn0s3-L1Zz_hUD3imMQqlkEzR9hPjkIGsDIe7g_",
        "filename": "cert-19-partidas.pdf",
    },
    {
        "msg_id": "17be4e5daf78fd76",  # Cert 18
        "attachment_id": "ANGjdJ8EnEzqQyTq6PK-PQoW3fTqZ0cyyFCbbGQqVbBBr8o6pQhq52usWAlBi19VxTZEnz5KIttIrMsNRZr3MCK_F9Vv8psu9uyTQvcvM45OiVZ334kZcGn1ZHWzv7_Mv3ri22CGOBVV68QCS8IAY3H_Rqlj9ACXtQY0lsTObqdPjwUJoOA7AL9aS7B6yZEekW3t6XyCz36uWKA1OlDqw6U1Zf-CnsfSgGj7U1Ajy1JobviLE6ySN76E7HYhQ_hLOiMBA_Z0hhwR116C6B06zdl4PXTQwOGboq-xc7qWaLAzxTcNeBvebpPyhCPDPOVQWjhW5kGi2rEQDvWTb32dev451Ht2Ne-ZMVbqVRtcQRoLV2qBWNWVxfZtSsb7WKy38ld1e6RXSMhg7Z8qBhGd",
        "filename": "cert-18-partidas.pdf",
    },
    {
        "msg_id": "17b1ae1db1ef77bf",  # Cert 17
        "attachment_id": "ANGjdJ8gRcmK4nlUnQb05I3ohz86bueosq0ktaZ9TMwaNkwHNi3vqEFzY4_wCuWlf5WiwQc6NMLkZXJtUKJkJbYoBtTMW4pDMDaR7yRNLOnj6tpFNElR0rFK_32GJBAyd7eHfPShgRYBZnmwnWQNAT7pVUWW3Y_pS3Nq_9AVZo10BNwfrcgPHuSJ7xGJ2zw_mqDEyVZSq1sboYYr02sNMedvKmi6Rh971npJUbs-b0VxMZZ5DaBmZ1F-7Yf2cqdLop9gdvWH2rqQazrzrud6oMRRYA61P7CpYHZVim_yn6wRid9Bt37b6--SnATcSleSLuEM1fOcdURsBsUOQO-XXOxvmPrXFhPXxSuFCHm2pwd9U0jDkSe0Hr5DKn72gxXrtC0jDt_zZlUcMe3R1JXd",
        "filename": "cert-17-partidas.pdf",
    },
]


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
            if not CREDENTIALS_FILE.exists():
                print(f"Error: {CREDENTIALS_FILE} not found.")
                print("Please set up Gmail API credentials first.")
                sys.exit(1)
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def download_attachment(service, message_id, attachment_id, filename):
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
        output_path = OUTPUT_DIR / filename

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        with open(output_path, "wb") as f:
            f.write(file_data)

        print(f"✓ Downloaded: {filename} ({len(file_data):,} bytes)")
        return output_path
    except Exception as e:
        print(f"✗ Error downloading {filename}: {e}")
        return None


def main():
    """Download all certification Partidas PDFs."""
    print("Downloading XEDEX Certification Partidas PDFs")
    print("=" * 60)

    if not CREDENTIALS_FILE.exists():
        print(f"\nError: Gmail API credentials not found at {CREDENTIALS_FILE}")
        print("\nTo set up:")
        print("1. Go to https://console.cloud.google.com/")
        print("2. Create a project and enable Gmail API")
        print("3. Create OAuth 2.0 credentials")
        print("4. Download credentials.json to project root")
        sys.exit(1)

    service = get_gmail_service()

    print(f"\nDownloading to: {OUTPUT_DIR}\n")

    downloaded = 0
    for item in ATTACHMENTS_TO_DOWNLOAD:
        print(f"Downloading {item['filename']}...")
        result = download_attachment(
            service, item["msg_id"], item["attachment_id"], item["filename"]
        )
        if result:
            downloaded += 1

    print(f"\n{'=' * 60}")
    print(f"Downloaded {downloaded}/{len(ATTACHMENTS_TO_DOWNLOAD)} PDFs")
    print(
        "\nNext: Run 'python3 scripts/search-emergency-flood-lights.py' to extract model numbers"
    )


if __name__ == "__main__":
    main()
