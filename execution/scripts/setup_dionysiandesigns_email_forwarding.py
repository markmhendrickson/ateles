#!/usr/bin/env python3
"""
Set up email forwarding for dionysiandesigns.com via DNSimple API.

Forwards contact@dionysiandesigns.com to email address from environment variable.

Note: DNSimple email forwarding may require enabling the service first via web interface,
as it's a paid add-on service. This script will attempt to create the forwarding rule
via API, but you may need to enable email forwarding in DNSimple dashboard first.

Usage:
    python execution/scripts/setup_dionysiandesigns_email_forwarding.py

Environment Variables:
    DIONYSIANDESIGNS_FORWARD_EMAIL - Email address to forward to (required)
"""

import os
import sys
from pathlib import Path

import requests

# Add parent directory to path for imports
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Define DNSimple API functions directly to avoid import issues
DNSIMPLE_API_BASE = "https://api.dnsimple.com/v2"


def get_dnsimple_token():
    """Get DNSimple API token from environment or 1Password."""
    import os

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("DNSIMPLE_API_TOKEN")

    if token:
        return token

    # Try 1Password
    try:
        from execution.scripts.extract_1password_nonsensitive import get_credential

        for field_name in ["access_token", "api_token", "token"]:
            try:
                token = get_credential("DNSimple", field=field_name)
                if token:
                    return token
            except Exception:
                continue
    except ImportError:
        pass

    print("Error: Could not retrieve DNSimple API token.")
    print("Set DNSIMPLE_API_TOKEN in .env or configure 1Password item.")
    return None


def get_account_id(api_token):
    """Get DNSimple account ID - list accounts and use first one."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    try:
        # First try whoami to see if account is set
        response = requests.get(f"{DNSIMPLE_API_BASE}/whoami", headers=headers)
        response.raise_for_status()
        data = response.json()

        # Check if account is in whoami response
        if data and "data" in data:
            account_data = data.get("data", {})
            if account_data and "account" in account_data:
                account = account_data.get("account")
                if account and isinstance(account, dict) and "id" in account:
                    return account.get("id")

        # If no account in whoami, list accounts
        print("  No account in whoami response, listing accounts...")
        response = requests.get(f"{DNSIMPLE_API_BASE}/accounts", headers=headers)
        response.raise_for_status()
        data = response.json()

        if data and "data" in data:
            accounts = data.get("data", [])
            if accounts and len(accounts) > 0:
                # Use first account
                account_id = accounts[0].get("id")
                if account_id:
                    print(f"  Using account ID: {account_id}")
                    return account_id

        print("  Warning: No accounts found in API response")
        return None
    except Exception as e:
        print(f"Error getting account ID: {e}")
        if hasattr(e, "response") and e.response is not None:
            try:
                print(f"  Response: {e.response.text}")
            except Exception:
                pass
        return None


DOMAIN = "dionysiandesigns.com"
FROM_EMAIL = "contact"
# Email forwarding destination - use environment variable or query from accounts/user_accounts parquet
TO_EMAIL = os.getenv("DIONYSIANDESIGNS_FORWARD_EMAIL", "")
if not TO_EMAIL:
    print(
        "Error: DIONYSIANDESIGNS_FORWARD_EMAIL not found in environment variables",
        file=sys.stderr,
    )
    print(
        "Set it in .env file or query $DATA_DIR/user_accounts/user_accounts.parquet",
        file=sys.stderr,
    )
    sys.exit(1)


def check_email_forwarding_enabled(api_token, account_id, domain_name):
    """Check if email forwarding is enabled for the domain."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    try:
        # Check if email forwarding service is available
        # Note: DNSimple API endpoint may vary - this is a best guess
        response = requests.get(
            f"{DNSIMPLE_API_BASE}/{account_id}/services", headers=headers
        )

        # Try to get email forwarding status for domain
        # DNSimple may use a different endpoint structure
        response = requests.get(
            f"{DNSIMPLE_API_BASE}/{account_id}/domains/{domain_name}/email_forwards",
            headers=headers,
        )

        if response.status_code == 200:
            return True, response.json()
        elif response.status_code == 404:
            return False, None
        else:
            # Service might not be enabled
            return None, None
    except Exception as e:
        print(f"  Note: Could not check email forwarding status: {e}")
        return None, None


def list_email_forwards(api_token, account_id, domain_name):
    """List existing email forwards for the domain."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    try:
        response = requests.get(
            f"{DNSIMPLE_API_BASE}/{account_id}/domains/{domain_name}/email_forwards",
            headers=headers,
        )

        if response.status_code == 200:
            data = response.json()
            forwards = data.get("data", [])
            return forwards
        else:
            print(
                f"  Warning: Could not list email forwards (status {response.status_code})"
            )
            return []
    except Exception as e:
        print(f"  Error listing email forwards: {e}")
        return []


def create_email_forward(api_token, account_id, domain_name, from_name, to_email):
    """Create an email forwarding rule."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    payload = {
        "from": f"{from_name}@{domain_name}",
        "to": to_email,
    }

    try:
        response = requests.post(
            f"{DNSIMPLE_API_BASE}/{account_id}/domains/{domain_name}/email_forwards",
            headers=headers,
            json=payload,
        )

        if response.status_code == 201:
            data = response.json()
            return True, data.get("data")
        elif response.status_code == 400:
            error_data = response.json()
            error_msg = error_data.get("message", "Unknown error")
            return False, f"Bad request: {error_msg}"
        elif response.status_code == 402:
            return (
                False,
                "Email forwarding service not enabled. Please enable it in DNSimple dashboard first.",
            )
        elif response.status_code == 404:
            return (
                False,
                "Domain not found or email forwarding not available for this domain.",
            )
        else:
            return False, f"API returned status {response.status_code}: {response.text}"
    except Exception as e:
        return False, f"Error creating email forward: {e}"


def main():
    print("Setting up email forwarding for dionysiandesigns.com")
    print("=" * 60)
    print()

    # Get API token
    print("Fetching DNSimple API token...")
    api_token = get_dnsimple_token()
    if not api_token:
        print("\nError: Could not retrieve DNSimple API token.")
        print("\nTo fix this:")
        print("1. Set DNSIMPLE_API_TOKEN in .env file, or")
        print("2. Create a 1Password item titled 'DNSimple' with API token")
        print("3. Get your API token from: https://dnsimple.com/user")
        sys.exit(1)

    print("✓ API token retrieved")
    print()

    # Get account ID
    print("Fetching DNSimple account ID...")
    account_id = get_account_id(api_token)
    if not account_id:
        print("\nError: Could not retrieve DNSimple account ID.")
        sys.exit(1)

    print(f"✓ Account ID: {account_id}")
    print()

    # Check existing forwards
    print(f"Checking existing email forwards for {DOMAIN}...")
    existing_forwards = list_email_forwards(api_token, account_id, DOMAIN)

    if existing_forwards:
        print(f"  Found {len(existing_forwards)} existing forward(s):")
        for forward in existing_forwards:
            from_addr = forward.get("from", "unknown")
            to_addr = forward.get("to", "unknown")
            print(f"    - {from_addr} → {to_addr}")
    else:
        print("  No existing forwards found")
    print()

    # Check if contact@ already exists
    target_from = f"{FROM_EMAIL}@{DOMAIN}"
    for forward in existing_forwards:
        if forward.get("from") == target_from:
            print(
                f"✓ Email forward already exists: {target_from} → {forward.get('to')}"
            )
            print("\nNo action needed. Forwarding is already configured.")
            return

    # Create email forward
    print(f"Creating email forward: {target_from} → {TO_EMAIL}")
    success, result = create_email_forward(
        api_token, account_id, DOMAIN, FROM_EMAIL, TO_EMAIL
    )

    if success:
        print("✓ Email forward created successfully!")
        print("\nConfiguration:")
        print(f"  From: {target_from}")
        print(f"  To: {TO_EMAIL}")
        print("\nNext steps:")
        print("  1. Wait 15-60 minutes for DNS propagation")
        print(f"  2. Send a test email to {target_from}")
        print("  3. Check your Gmail inbox for the forwarded message")
        print(f"  4. Update website templates to use {target_from}")
    else:
        print("\n✗ Failed to create email forward")
        print(f"  Error: {result}")
        print("\nPossible solutions:")
        print("  1. Enable email forwarding service in DNSimple dashboard:")
        print("     - Go to https://dnsimple.com")
        print(f"     - Navigate to {DOMAIN}")
        print("     - Go to 'Email Forwarding' section")
        print("     - Enable the service (may require paid plan)")
        print("  2. Or set up manually via DNSimple web interface:")
        print("     - Log into DNSimple")
        print(f"     - Go to {DOMAIN} → Email Forwarding")
        print(f"     - Add forward: {FROM_EMAIL} → {TO_EMAIL}")
        sys.exit(1)


if __name__ == "__main__":
    main()
