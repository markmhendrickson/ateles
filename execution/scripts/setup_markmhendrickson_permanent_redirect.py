#!/usr/bin/env python3
"""
Set up permanent 301 redirect from markmhendrickson.com to mark.hendricksonserrano.com
using DNSimple's URL redirect record.

This script will:
1. Delete existing A records (if any) that point to GitHub Pages
2. Create a URL redirect record for permanent 301 redirect

Requirements:
    - DNSimple API token stored in 1Password (item titled "DNSimple" or "dnsimple.com")
    - requests library: pip install requests
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import DNS functions from the mark subdomain script
try:
    from scripts.configure_mark_subdomain_dns import (
        create_dns_record,
        delete_dns_record,
        get_account_id,
        get_dnsimple_token,
        list_dns_records,
    )
except ImportError:
    print("Error: Could not import DNSimple functions.")
    sys.exit(1)


# Domain configuration
# NOTE: This script is deprecated - markmhendrickson.com now hosts the website
# Use configure_markmhendrickson_website_hosting.py instead
DOMAIN = "markmhendrickson.com"
REDIRECT_TARGET = "https://mark.hendricksonserrano.com"  # OLD - no longer used

# GitHub Pages A record IPs (to identify and remove if present)
GITHUB_PAGES_IPS = [
    "185.199.108.153",
    "185.199.109.153",
    "185.199.110.153",
    "185.199.111.153",
]


def setup_permanent_redirect(api_token, account_id, domain_name, target_url):
    """Set up permanent 301 redirect using DNSimple URL redirect record."""
    print(f"\nConfiguring permanent redirect for {domain_name}...")
    print(f"Target: {target_url}")

    # List existing records
    print("Checking existing DNS records...")
    existing_records = list_dns_records(api_token, account_id, domain_name)

    # Find existing A records for root domain (empty name or "@")
    root_a_records = [
        r
        for r in existing_records
        if (r.get("name") == "" or r.get("name") == "@") and r.get("type") == "A"
    ]

    # Find existing URL redirect records
    root_url_records = [
        r
        for r in existing_records
        if (r.get("name") == "" or r.get("name") == "@") and r.get("type") == "URL"
    ]

    # Check if URL redirect already exists and is correct
    if root_url_records:
        existing_url = root_url_records[0].get("content", "")
        if existing_url == target_url or existing_url.rstrip("/") == target_url.rstrip(
            "/"
        ):
            print(f"✓ URL redirect already exists and is correct: -> {target_url}")
            return True
        else:
            print(f"Found existing URL redirect pointing to: {existing_url}")
            print(f"Will update to: {target_url}")

    # Delete A records if they exist (they conflict with URL redirect)
    if root_a_records:
        print(f"\nFound {len(root_a_records)} A record(s) that need to be removed:")
        for record in root_a_records:
            print(
                f"  - A {record.get('name') or '@'} -> {record.get('content')} (ID: {record.get('id')})"
            )
        print("Deleting A records (they conflict with URL redirect)...")
        for record in root_a_records:
            print(f"Deleting A record (ID: {record.get('id')})...")
            if delete_dns_record(api_token, account_id, domain_name, record.get("id")):
                print("  ✓ Deleted")
            else:
                print("  ✗ Failed to delete")
                return False

    # Delete existing URL redirect if it points to wrong target
    if root_url_records and root_url_records[0].get("content") != target_url:
        print("Deleting existing URL redirect...")
        for record in root_url_records:
            if delete_dns_record(api_token, account_id, domain_name, record.get("id")):
                print("  ✓ Deleted old URL redirect")
            else:
                print("  ✗ Failed to delete")
                return False

    # Create or update URL redirect record
    if root_url_records and root_url_records[0].get("content") == target_url:
        print("\n✓ URL redirect already configured correctly")
        return True

    print(f"\nCreating URL redirect record: @ -> {target_url}...")
    record = create_dns_record(
        api_token,
        account_id,
        domain_name,
        name="",  # Empty string for root domain
        record_type="URL",
        content=target_url,
        ttl=3600,
    )

    if record:
        print(f"  ✓ Created URL redirect (ID: {record.get('id')})")
        print(f"\n✓ Successfully configured permanent redirect for {domain_name}")
        print(f"  {domain_name} -> {target_url} (301 Permanent Redirect)")
        print("\nNote: DNS propagation may take a few minutes.")
        print("The redirect will be active once DNS propagates.")
        return True
    else:
        print("  ✗ Failed to create URL redirect")
        return False


def main():
    """Main function."""
    api_token = get_dnsimple_token()

    if not api_token:
        print("\nError: Could not retrieve DNSimple API token.")
        sys.exit(1)

    print("Fetching account information...")
    try:
        account_id = get_account_id(api_token)
        print(f"✓ Account ID: {account_id}")
    except Exception as e:
        print(f"Error fetching account: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    # Set up permanent redirect
    success = setup_permanent_redirect(api_token, account_id, DOMAIN, REDIRECT_TARGET)

    if success:
        print(f"\n✓ Redirect configuration complete for {DOMAIN}")
        print(f"\nThe domain {DOMAIN} will permanently redirect to {REDIRECT_TARGET}")
        print("Wait a few minutes for DNS propagation, then test with:")
        print(f"  curl -I http://{DOMAIN}")
        print(f"  curl -I https://{DOMAIN}")
    else:
        print("\n✗ Redirect configuration failed. Please check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
