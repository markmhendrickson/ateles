#!/usr/bin/env python3
"""
Set up Gmail MCP server OAuth credentials from 1Password.

This script retrieves OAuth credentials from 1Password and creates the
proper credentials.json file for the Gmail MCP server.

Security: Never prints secrets to stdout - only writes to file.
"""

import json
import subprocess
import sys
from pathlib import Path

# Gmail MCP server directory
GMAIL_SERVER_DIR = Path.home() / ".local" / "mcp-servers" / "mcp-gmail"
CREDENTIALS_FILE = GMAIL_SERVER_DIR / "credentials.json"
TOKEN_FILE = GMAIL_SERVER_DIR / "token.json"


def run_op_command(args: list[str]) -> dict:
    """Run 1Password CLI command and return JSON result."""
    try:
        cmd = ["op", *args]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"1Password CLI error: {e.stderr}\n"
            "Ensure 'op' is installed and authenticated: eval $(op signin)"
        ) from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse 1Password CLI output: {e}") from e
    except FileNotFoundError:
        raise RuntimeError(
            "1Password CLI not found. Install from: https://developer.1password.com/docs/cli"
        )


def op_read(field_ref: str) -> str:
    """Read a secret from 1Password using op read."""
    try:
        result = subprocess.run(
            ["op", "read", field_ref],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to read {field_ref} from 1Password: {e.stderr}"
        ) from e


def get_oauth_credentials_from_1password(
    item_name: str = "Gmail OAuth", vault: str = "Private"
):
    """
    Retrieve OAuth credentials from 1Password item.

    Expected fields in 1Password item:
    - OAuth client ID (or client_id)
    - OAuth client secret (or client_secret)
    - Project ID (or project_id) - optional, will be extracted from client_id if missing
    """
    print(
        f"Retrieving OAuth credentials from 1Password item: '{item_name}' (vault: {vault})"
    )

    # Get the item
    try:
        cmd = ["op", "item", "get", item_name, "--format", "json"]
        if vault:
            cmd.extend(["--vault", vault])
        item_data = run_op_command(cmd[1:])  # Skip 'op' since run_op_command adds it
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("\nMake sure:")
        print("1. 1Password CLI is installed and authenticated: eval $(op signin)")
        print(f"2. Item '{item_name}' exists in vault '{vault}'")
        print("3. Item has fields: 'OAuth client ID' and 'OAuth client secret'")
        sys.exit(1)

    # Extract fields
    client_id = None
    client_secret = None
    project_id = None

    # Look through fields
    for field in item_data.get("fields", []):
        label = field.get("label", "").lower().strip()
        value = field.get("value", "")
        reference = field.get("reference", "")

        # Try to match common field names
        if "client id" in label or label == "oauth client id" or label == "client_id":
            if reference:
                try:
                    client_id = op_read(reference)
                except RuntimeError:
                    client_id = value
            else:
                client_id = value
        elif (
            "client secret" in label
            or label == "oauth client secret"
            or label == "client_secret"
        ):
            if reference:
                try:
                    client_secret = op_read(reference)
                except RuntimeError:
                    client_secret = value
            else:
                client_secret = value
        elif "project id" in label or label == "project_id":
            if reference:
                try:
                    project_id = op_read(reference)
                except RuntimeError:
                    project_id = value
            else:
                project_id = value

    # Extract project_id from client_id if not found
    # Format: PROJECT_ID-NUMBERS.apps.googleusercontent.com
    if not project_id and client_id:
        parts = client_id.split("-")
        if len(parts) >= 2:
            # Project ID is typically the first part before the first dash
            project_id = parts[0]

    # Validate we have required fields
    if not client_id:
        print(
            "Error: 'OAuth client ID' field not found in 1Password item",
            file=sys.stderr,
        )
        print(
            f"Item fields found: {[f.get('label', '') for f in item_data.get('fields', [])]}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not client_secret:
        print(
            "Error: 'OAuth client secret' field not found in 1Password item",
            file=sys.stderr,
        )
        print(
            f"Item fields found: {[f.get('label', '') for f in item_data.get('fields', [])]}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not project_id:
        print(
            "Warning: 'Project ID' not found, extracting from client_id",
            file=sys.stderr,
        )
        # Try to extract from client_id format
        if "." in client_id:
            project_id = client_id.split(".")[0].split("-")[0]
        else:
            project_id = "gmail-mcp-project"  # Fallback

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "project_id": project_id,
    }


def create_credentials_json(oauth_creds: dict, output_path: Path):
    """Create Google OAuth credentials.json file."""
    credentials = {
        "installed": {
            "client_id": oauth_creds["client_id"],
            "project_id": oauth_creds["project_id"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": oauth_creds["client_secret"],
            "redirect_uris": ["http://localhost"],
        }
    }

    # Ensure directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write credentials file
    with open(output_path, "w") as f:
        json.dump(credentials, f, indent=2)

    print(f"✓ Created credentials.json at: {output_path}")


def main():
    """Main setup function."""
    print("Gmail MCP Server OAuth Setup from 1Password")
    print("=" * 50)
    print()

    # Check if server directory exists
    if not GMAIL_SERVER_DIR.exists():
        print(f"Error: Gmail MCP server directory not found: {GMAIL_SERVER_DIR}")
        print("Please install the Gmail MCP server first.")
        sys.exit(1)

    # Check for existing credentials
    if CREDENTIALS_FILE.exists():
        print(f"⚠️  Existing credentials.json found at: {CREDENTIALS_FILE}")
        response = input("Replace existing credentials? (y/N): ").strip().lower()
        if response != "y":
            print("Keeping existing credentials. Exiting.")
            sys.exit(0)

    # Check for existing token (should be deleted if OAuth client was deleted)
    if TOKEN_FILE.exists():
        print(f"⚠️  Existing token.json found at: {TOKEN_FILE}")
        print("This token references a deleted OAuth client and should be removed.")
        response = input("Delete old token file? (Y/n): ").strip().lower()
        if response != "n":
            TOKEN_FILE.unlink()
            print("✓ Deleted old token file")

    # Get credentials from 1Password
    try:
        oauth_creds = get_oauth_credentials_from_1password()
    except KeyboardInterrupt:
        print("\nCancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"Error retrieving credentials: {e}", file=sys.stderr)
        sys.exit(1)

    # Create credentials.json
    try:
        create_credentials_json(oauth_creds, CREDENTIALS_FILE)
    except Exception as e:
        print(f"Error creating credentials file: {e}", file=sys.stderr)
        sys.exit(1)

    print()
    print("Setup complete!")
    print()
    print("Next steps:")
    print("1. Restart Cursor")
    print("2. The Gmail MCP server will prompt for OAuth authentication on first use")
    print("3. A browser window will open for authorization")
    print("4. After authorization, token.json will be created automatically")
    print()


if __name__ == "__main__":
    main()
