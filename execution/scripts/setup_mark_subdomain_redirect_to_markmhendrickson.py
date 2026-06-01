#!/usr/bin/env python3
"""
Set up permanent 301 redirect from mark.hendricksonserrano.com to markmhendrickson.com
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
PARENT_DOMAIN = "hendricksonserrano.com"
SUBDOMAIN = "mark"
FULL_SUBDOMAIN = f"{SUBDOMAIN}.{PARENT_DOMAIN}"
REDIRECT_TARGET = "https://markmhendrickson.com"

# GitHub Pages A record IPs (to identify and remove if present)
GITHUB_PAGES_IPS = [
    "185.199.108.153",
    "185.199.109.153",
    "185.199.110.153",
    "185.199.111.153",
]


def setup_subdomain_redirect(
    api_token, account_id, parent_domain, subdomain, target_url
):
    """Set up permanent 301 redirect for subdomain using DNSimple URL redirect record."""
    full_subdomain = f"{subdomain}.{parent_domain}"
    print(f"\nConfiguring permanent redirect for {full_subdomain}...")
    print(f"Target: {target_url}")

    # List existing records
    print("Checking existing DNS records...")
    existing_records = list_dns_records(api_token, account_id, parent_domain)

    # Find existing A or CNAME records for the subdomain
    subdomain_a_records = [
        r
        for r in existing_records
        if r.get("name") == subdomain and r.get("type") == "A"
    ]

    subdomain_cname_records = [
        r
        for r in existing_records
        if r.get("name") == subdomain and r.get("type") == "CNAME"
    ]

    # Find existing URL redirect records for the subdomain
    subdomain_url_records = [
        r
        for r in existing_records
        if r.get("name") == subdomain and r.get("type") == "URL"
    ]

    # Check if URL redirect already exists and is correct
    if subdomain_url_records:
        existing_url = subdomain_url_records[0].get("content", "")
        if existing_url == target_url or existing_url.rstrip("/") == target_url.rstrip(
            "/"
        ):
            print(f"✓ URL redirect already exists and is correct: -> {target_url}")
            return True
        else:
            print(f"Found existing URL redirect pointing to: {existing_url}")
            print(f"Will update to: {target_url}")

    # Delete A records if they exist (they conflict with URL redirect)
    if subdomain_a_records:
        print(
            f"\nFound {len(subdomain_a_records)} A record(s) that need to be removed:"
        )
        for record in subdomain_a_records:
            print(
                f"  - A {subdomain} -> {record.get('content')} (ID: {record.get('id')})"
            )
        print("Deleting A records (they conflict with URL redirect)...")
        for record in subdomain_a_records:
            print(f"Deleting A record (ID: {record.get('id')})...")
            if delete_dns_record(
                api_token, account_id, parent_domain, record.get("id")
            ):
                print("  ✓ Deleted")
            else:
                print("  ✗ Failed to delete")
                return False

    # Delete CNAME records if they exist (they conflict with URL redirect)
    if subdomain_cname_records:
        print(
            f"\nFound {len(subdomain_cname_records)} CNAME record(s) that need to be removed:"
        )
        for record in subdomain_cname_records:
            print(
                f"  - CNAME {subdomain} -> {record.get('content')} (ID: {record.get('id')})"
            )
        print("Deleting CNAME records (they conflict with URL redirect)...")
        for record in subdomain_cname_records:
            print(f"Deleting CNAME record (ID: {record.get('id')})...")
            if delete_dns_record(
                api_token, account_id, parent_domain, record.get("id")
            ):
                print("  ✓ Deleted")
            else:
                print("  ✗ Failed to delete")
                return False

    # Delete existing URL redirect if it points to wrong target
    if subdomain_url_records and subdomain_url_records[0].get("content") != target_url:
        print("Deleting existing URL redirect...")
        for record in subdomain_url_records:
            if delete_dns_record(
                api_token, account_id, parent_domain, record.get("id")
            ):
                print("  ✓ Deleted old URL redirect")
            else:
                print("  ✗ Failed to delete")
                return False

    # Create or update URL redirect record
    if subdomain_url_records and subdomain_url_records[0].get("content") == target_url:
        print("\n✓ URL redirect already configured correctly")
        return True

    print(f"\nCreating URL redirect record: {subdomain} -> {target_url}...")
    record = create_dns_record(
        api_token,
        account_id,
        parent_domain,
        name=subdomain,
        record_type="URL",
        content=target_url,
        ttl=3600,
    )

    if record:
        print(f"  ✓ Created URL redirect (ID: {record.get('id')})")
        print(f"\n✓ Successfully configured permanent redirect for {full_subdomain}")
        print(f"  {full_subdomain} -> {target_url} (301 Permanent Redirect)")
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
    success = setup_subdomain_redirect(
        api_token, account_id, PARENT_DOMAIN, SUBDOMAIN, REDIRECT_TARGET
    )

    if success:
        print(f"\n✓ Redirect configuration complete for {FULL_SUBDOMAIN}")
        print(
            f"\nThe subdomain {FULL_SUBDOMAIN} will permanently redirect to {REDIRECT_TARGET}"
        )
        print("Wait a few minutes for DNS propagation, then test with:")
        print(f"  curl -I http://{FULL_SUBDOMAIN}")
        print(f"  curl -I https://{FULL_SUBDOMAIN}")
    else:
        print("\n✗ Redirect configuration failed. Please check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
