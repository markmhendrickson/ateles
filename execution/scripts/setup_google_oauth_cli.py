#!/usr/bin/env python3
"""
Automate Google Cloud OAuth client creation via CLI/API.

This script uses the Google Cloud IAM API to create OAuth clients programmatically.
Note: Some steps (like OAuth consent screen configuration) may still require web UI.

Requirements:
- pip install google-api-python-client google-auth google-auth-oauthlib
- Service account with IAM Admin permissions OR user authentication
- Project ID set

Usage:
    python execution/scripts/setup_google_oauth_cli.py \
        --project-id personal-412209 \
        --client-name "Gmail MCP Desktop Client" \
        --application-type desktop
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from google.auth.exceptions import DefaultCredentialsError
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except ImportError:
    print("ERROR: Required packages not installed.")
    print(
        "Install with: pip install google-api-python-client google-auth google-auth-oauthlib"
    )
    sys.exit(1)


def create_oauth_client_via_api(
    project_id: str, client_name: str, application_type: str = "desktop"
):
    """
    Create OAuth client using Google Cloud IAM API.

    Note: This uses the IAM API which has limitations. For full OAuth client creation,
    you may need to use the web UI or REST API directly.
    """
    try:
        # Try to use service account credentials
        creds_path = Path.home() / ".creds" / "gcp-service-account.json"
        if not creds_path.exists():
            # Try .creds in repo
            repo_root = Path(__file__).parent.parent.parent
            creds_path = repo_root / ".creds" / "gcp-service-account.json"

        if creds_path.exists():
            credentials = service_account.Credentials.from_service_account_file(
                str(creds_path),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        else:
            # Fall back to application default credentials
            from google.auth import default

            credentials, _ = default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
    except DefaultCredentialsError:
        print("ERROR: No credentials found.")
        print("Options:")
        print("1. Place service account key at: ~/.creds/gcp-service-account.json")
        print("2. Or run: gcloud auth application-default login")
        return None

    # Note: The IAM API doesn't directly support OAuth client creation
    # We need to use the OAuth2 API or IAP API (which has limitations)
    print("⚠️  Note: Full OAuth client creation via API is limited.")
    print("   The IAM API doesn't support general OAuth client creation.")
    print("   For desktop OAuth clients, use the web UI or REST API.")
    print()
    print("Alternative: Use gcloud CLI (if installed):")
    print("  1. Install: https://cloud.google.com/sdk/docs/install")
    print("  2. Authenticate: gcloud auth login")
    print("  3. Set project: gcloud config set project personal-412209")
    print()
    print("Or use the web UI:")
    print("  https://console.cloud.google.com/apis/credentials")

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Create Google Cloud OAuth client via CLI/API"
    )
    parser.add_argument(
        "--project-id", default="personal-412209", help="Google Cloud project ID"
    )
    parser.add_argument(
        "--client-name",
        default="Gmail MCP Desktop Client",
        help="Name for the OAuth client",
    )
    parser.add_argument(
        "--application-type",
        default="desktop",
        choices=["desktop", "web", "ios", "android"],
        help="Application type for OAuth client",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path for credentials JSON (default: print to stdout)",
    )

    args = parser.parse_args()

    print("Google Cloud OAuth Client Creation")
    print("=" * 50)
    print(f"Project ID: {args.project_id}")
    print(f"Client Name: {args.client_name}")
    print(f"Application Type: {args.application_type}")
    print()

    result = create_oauth_client_via_api(
        args.project_id, args.client_name, args.application_type
    )

    if result:
        if args.output:
            with open(args.output, "w") as f:
                json.dump(result, f, indent=2)
            print(f"✓ Credentials saved to: {args.output}")
        else:
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
