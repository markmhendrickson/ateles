#!/usr/bin/env python3
"""
Configure DNS for markh.io to point to GitHub Pages for redirect.

This script creates a CNAME record pointing to GitHub Pages for the redirect site.

Requirements:
    - DNSimple API token stored in 1Password (item titled "DNSimple" or "dnsimple.com")
    - requests library: pip install requests
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import DNS functions
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
DOMAIN = "markh.io"
GITHUB_PAGES_TARGET = "markmhendrickson.github.io"


def update_to_cname(api_token, account_id, domain_name):
    """Update DNS to CNAME for GitHub Pages."""
    print(f"\nConfiguring DNS for {domain_name}...")

    # List existing records
    print("Checking existing DNS records...")
    existing_records = list_dns_records(api_token, account_id, domain_name)

    # Find existing A or CNAME records for root domain
    root_records = [
        r
        for r in existing_records
        if (r.get("name") == "" or r.get("name") == "@")
        and r.get("type") in ["A", "CNAME", "URL"]
    ]

    # Check if CNAME already exists and is correct
    cname_record = next((r for r in root_records if r.get("type") == "CNAME"), None)
    target_with_dot = GITHUB_PAGES_TARGET + "."
    if cname_record and (
        cname_record.get("content") == GITHUB_PAGES_TARGET
        or cname_record.get("content") == target_with_dot
    ):
        print(f"✓ CNAME record already exists and is correct: -> {GITHUB_PAGES_TARGET}")
        return True

    if root_records:
        print(f"Found {len(root_records)} existing record(s):")
        for record in root_records:
            print(
                f"  - {record.get('type')} {record.get('name') or '@'} -> {record.get('content')} (ID: {record.get('id')})"
            )

        print(
            f"\nWill delete existing records and create CNAME: @ -> {GITHUB_PAGES_TARGET}"
        )
        print("Proceeding automatically...")

        # Delete existing records
        for record in root_records:
            print(f"Deleting {record.get('type')} record (ID: {record.get('id')})...")
            if delete_dns_record(api_token, account_id, domain_name, record.get("id")):
                print("  ✓ Deleted")
            else:
                print("  ✗ Failed to delete")

    # Create CNAME record for root domain
    print(f"\nCreating CNAME record: @ -> {GITHUB_PAGES_TARGET}...")
    record = create_dns_record(
        api_token,
        account_id,
        domain_name,
        name="",  # Empty string for root domain
        record_type="CNAME",
        content=GITHUB_PAGES_TARGET,
        ttl=3600,
    )

    if record:
        print(f"  ✓ Created (ID: {record.get('id')})")
        print(f"\n✓ Successfully configured CNAME record for {domain_name}")
        print(f"  CNAME: @ -> {GITHUB_PAGES_TARGET}")
        print("\nNote: DNS propagation may take a few minutes.")
        print("GitHub Pages should automatically detect the CNAME and configure SSL.")
        return True
    else:
        print("  ✗ Failed to create CNAME record")
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

    # Update to CNAME
    success = update_to_cname(api_token, account_id, DOMAIN)

    if success:
        print(f"\n✓ DNS configuration complete for {DOMAIN}")
        print("\nNext steps:")
        print("1. Wait a few minutes for DNS propagation")
        print("2. Configure GitHub Pages for markh.io in repository settings")
        print("3. GitHub Pages should automatically configure SSL")
    else:
        print("\n✗ DNS configuration failed. Please check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
