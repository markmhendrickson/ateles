#!/usr/bin/env python3
"""
Configure DNS records in DNSimple for dionysiandesigns.com to point to Netlify.

Netlify requires:
- ALIAS/ANAME to apex-loadbalancer.netlify.com (if supported)
- OR A record to 75.2.60.5 (fallback)

DNSimple supports ALIAS records, so we'll use that for the recommended configuration.

Usage:
    python execution/scripts/configure_dionysiandesigns_netlify_dns.py
"""

import sys
from pathlib import Path

import requests

# Add parent directory to path for imports
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Define DNSimple API functions directly
DNSIMPLE_API_BASE = "https://api.dnsimple.com/v2"


def get_dnsimple_token():
    """Get DNSimple API token from environment or 1Password."""
    import os

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("DNSIMPLE_API_TOKEN")

    if token:
        return token

    # Try 1Password (simplified - avoid problematic imports)
    try:
        import subprocess

        result = subprocess.run(
            ["op", "item", "get", "DNSimple", "--fields", "label=access token,value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    print("Error: Could not retrieve DNSimple API token.")
    print("Set DNSIMPLE_API_TOKEN in .env or configure 1Password item.")
    return None


def get_account_id(api_token):
    """Get DNSimple account ID."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    try:
        response = requests.get(f"{DNSIMPLE_API_BASE}/whoami", headers=headers)
        response.raise_for_status()
        data = response.json()

        if data and "data" in data:
            account_data = data.get("data", {})
            if account_data and "account" in account_data:
                account = account_data.get("account")
                if account and isinstance(account, dict) and "id" in account:
                    return account.get("id")

        # If no account in whoami, list accounts
        response = requests.get(f"{DNSIMPLE_API_BASE}/accounts", headers=headers)
        response.raise_for_status()
        data = response.json()

        if data and "data" in data:
            accounts = data.get("data", [])
            if accounts and len(accounts) > 0:
                return accounts[0].get("id")

        return None
    except Exception as e:
        print(f"Error getting account ID: {e}")
        return None


def list_dns_records(api_token, account_id, domain_name):
    """List DNS records for a domain."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    try:
        response = requests.get(
            f"{DNSIMPLE_API_BASE}/{account_id}/zones/{domain_name}/records",
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("data", [])
    except Exception as e:
        print(f"Error listing DNS records: {e}")
        return []


def delete_dns_record(api_token, account_id, domain_name, record_id):
    """Delete a DNS record."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    try:
        response = requests.delete(
            f"{DNSIMPLE_API_BASE}/{account_id}/zones/{domain_name}/records/{record_id}",
            headers=headers,
        )
        return response.status_code in [200, 204]
    except Exception as e:
        print(f"Error deleting DNS record: {e}")
        return False


def create_dns_record(
    api_token, account_id, domain_name, name, record_type, content, ttl=3600
):
    """Create a DNS record."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    data = {"name": name, "type": record_type, "content": content, "ttl": ttl}

    try:
        response = requests.post(
            f"{DNSIMPLE_API_BASE}/{account_id}/zones/{domain_name}/records",
            headers=headers,
            json=data,
        )

        if response.status_code in [200, 201]:
            return True, response.json().get("data")
        else:
            error_data = response.json() if response.text else {}
            error_msg = error_data.get("message", f"Status {response.status_code}")
            return False, error_msg
    except Exception as e:
        return False, str(e)


DOMAIN = "dionysiandesigns.com"
NETLIFY_ALIAS = "apex-loadbalancer.netlify.com"
NETLIFY_A_RECORD = "75.2.60.5"
NETLIFY_APP = "dionysiandesigns.netlify.app"  # Netlify app URL for www subdomain


def configure_netlify_dns(api_token, account_id, domain_name):
    """Configure DNS records for Netlify deployment."""
    print(f"\nConfiguring DNS for {domain_name} to point to Netlify...")

    # List existing records
    print("Checking existing DNS records...")
    existing_records = list_dns_records(api_token, account_id, domain_name)

    # Find existing A or ALIAS records for root domain
    root_records = [
        r
        for r in existing_records
        if (r.get("name") == "" or r.get("name") == "@")
        and r.get("type") in ["A", "ALIAS", "ANAME"]
    ]

    if root_records:
        print(f"Found {len(root_records)} existing A/ALIAS record(s) for root domain:")
        for record in root_records:
            print(
                f"  - {record.get('type')} {record.get('name') or '@'} -> {record.get('content')} (ID: {record.get('id')})"
            )

        # Check if any already point to Netlify
        netlify_records = [
            r
            for r in root_records
            if NETLIFY_ALIAS in r.get("content", "")
            or NETLIFY_A_RECORD in r.get("content", "")
        ]

        if netlify_records:
            print("\n✓ DNS already configured for Netlify!")
            return True

        # Ask to replace or keep existing
        print(
            "\nExisting records found. They will be replaced with Netlify configuration."
        )
        for record in root_records:
            print(f"Deleting {record.get('type')} record (ID: {record.get('id')})...")
            if delete_dns_record(api_token, account_id, domain_name, record.get("id")):
                print("  ✓ Deleted")
            else:
                print("  ✗ Failed to delete")
                return False
    else:
        print("  No existing A/ALIAS records found for root domain")

    # Create ALIAS record (recommended by Netlify)
    print(f"\nCreating ALIAS record pointing to {NETLIFY_ALIAS}...")
    success, result = create_dns_record(
        api_token,
        account_id,
        domain_name,
        name="",  # Root domain
        record_type="ALIAS",
        content=NETLIFY_ALIAS,
        ttl=3600,
    )

    if success:
        print("✓ ALIAS record created successfully!")
        print("\nConfiguration:")
        print("  Type: ALIAS")
        print("  Name: @ (root domain)")
        print(f"  Content: {NETLIFY_ALIAS}")

        # Also create www CNAME record (recommended by Netlify)
        print(f"\nCreating www CNAME record pointing to {NETLIFY_APP}...")
        www_success, www_result = create_dns_record(
            api_token,
            account_id,
            domain_name,
            name="www",
            record_type="CNAME",
            content=NETLIFY_APP,
            ttl=3600,
        )

        if www_success:
            print("✓ www CNAME record created successfully!")
        else:
            print(f"  Note: www CNAME creation failed: {www_result}")
            print(f"  You can add it manually: www CNAME {NETLIFY_APP}")

        print("\nNext steps:")
        print("  1. Wait 15-60 minutes for DNS propagation")
        print("  2. Netlify will automatically verify DNS")
        print("  3. HTTPS certificate will be provisioned automatically")
        return True
    else:
        print(f"✗ Failed to create ALIAS record: {result}")
        print("\nTrying fallback A record...")

        # Fallback: Create A record
        success, result = create_dns_record(
            api_token,
            account_id,
            domain_name,
            name="",
            record_type="A",
            content=NETLIFY_A_RECORD,
            ttl=3600,
        )

        if success:
            print("✓ A record created successfully (fallback configuration)")
            print("\nConfiguration:")
            print("  Type: A")
            print("  Name: @ (root domain)")
            print(f"  Content: {NETLIFY_A_RECORD}")
            print(
                "\nNote: This is the fallback option. ALIAS is preferred for better CDN performance."
            )
            return True
        else:
            print(f"✗ Failed to create A record: {result}")
            return False


def main():
    print("Configuring DNS for dionysiandesigns.com → Netlify")
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

    # Configure DNS
    success = configure_netlify_dns(api_token, account_id, DOMAIN)

    if success:
        print("\n✓ DNS configuration complete!")
        print("\nNetlify will verify DNS automatically. This may take 15-60 minutes.")
    else:
        print("\n✗ DNS configuration failed. Please configure manually in DNSimple:")
        print("  1. Go to https://dnsimple.com")
        print(f"  2. Navigate to {DOMAIN} → DNS")
        print(f"  3. Add ALIAS record: @ → {NETLIFY_ALIAS}")
        print(f"     OR A record: @ → {NETLIFY_A_RECORD}")
        sys.exit(1)


if __name__ == "__main__":
    main()
