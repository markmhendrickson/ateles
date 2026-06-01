#!/usr/bin/env python3
"""
Configure DNS for markmhendrickson.com to point to GitHub Pages for redirect.

This script creates a CNAME record pointing to GitHub Pages for the redirect site.

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
        DNSIMPLE_API_BASE,
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
DOMAIN = "markmhendrickson.com"
GITHUB_PAGES_TARGET = "markmhendrickson.github.io"

# GitHub Pages A record IPs (use A records for root domain since CNAME can't coexist with NS/SOA)
GITHUB_PAGES_IPS = [
    "185.199.108.153",
    "185.199.109.153",
    "185.199.110.153",
    "185.199.111.153",
]


def update_to_a_records(api_token, account_id, domain_name):
    """Update DNS to A records for GitHub Pages (required for root domain)."""
    print(f"\nConfiguring DNS for {domain_name}...")
    print(
        "Note: Using A records for root domain (CNAME cannot coexist with NS/SOA records)"
    )

    # List existing records
    print("Checking existing DNS records...")
    existing_records = list_dns_records(api_token, account_id, domain_name)

    # Find existing A records for root domain (empty name or "@")
    root_a_records = [
        r
        for r in existing_records
        if (r.get("name") == "" or r.get("name") == "@") and r.get("type") == "A"
    ]

    # Find existing URL redirect records (these conflict with A records)
    root_url_records = [
        r
        for r in existing_records
        if (r.get("name") == "" or r.get("name") == "@") and r.get("type") == "URL"
    ]

    # Delete URL records first (they conflict with A records)
    if root_url_records:
        print(
            f"Found {len(root_url_records)} URL redirect record(s) that need to be removed:"
        )
        for record in root_url_records:
            print(
                f"  - URL {record.get('name') or '@'} -> {record.get('content')} (ID: {record.get('id')})"
            )
        print("Deleting URL records...")
        for record in root_url_records:
            print(f"Deleting URL record (ID: {record.get('id')})...")
            if delete_dns_record(api_token, account_id, domain_name, record.get("id")):
                print("  ✓ Deleted")
            else:
                print("  ✗ Failed to delete")
                return False

    # Check if A records already point to GitHub Pages IPs
    existing_ips = {r.get("content") for r in root_a_records}
    github_ips_set = set(GITHUB_PAGES_IPS)
    if existing_ips == github_ips_set and len(root_a_records) == len(GITHUB_PAGES_IPS):
        print("✓ A records already exist and point to GitHub Pages IPs")
        return True

    if root_a_records:
        print(f"Found {len(root_a_records)} existing A record(s):")
        for record in root_a_records:
            print(
                f"  - A {record.get('name') or '@'} -> {record.get('content')} (ID: {record.get('id')})"
            )

        print(
            "\nWill delete existing A records and create new ones pointing to GitHub Pages"
        )
        print("Proceeding automatically...")

        # Delete existing A records
        for record in root_a_records:
            print(f"Deleting A record (ID: {record.get('id')})...")
            if delete_dns_record(api_token, account_id, domain_name, record.get("id")):
                print("  ✓ Deleted")
            else:
                print("  ✗ Failed to delete")

    # Create A records for GitHub Pages
    print("\nCreating A records pointing to GitHub Pages...")
    created_records = []

    for ip in GITHUB_PAGES_IPS:
        print(f"Creating A record: @ -> {ip}...")
        record = create_dns_record(
            api_token,
            account_id,
            domain_name,
            name="",  # Empty string for root domain
            record_type="A",
            content=ip,
            ttl=3600,
        )

        if record:
            print(f"  ✓ Created (ID: {record.get('id')})")
            created_records.append(record)
        else:
            print("  ✗ Failed to create")

    if len(created_records) == len(GITHUB_PAGES_IPS):
        print(
            f"\n✓ Successfully configured {len(created_records)} A records for {domain_name}"
        )
        print("\nA records created:")
        for record in created_records:
            print(f"  - A @ -> {record.get('content')}")
        print("\nNote: DNS propagation may take a few minutes.")
        print(
            "GitHub Pages should automatically detect the A records and configure SSL."
        )
        return True
    else:
        print(
            f"\n✗ Warning: Only {len(created_records)} of {len(GITHUB_PAGES_IPS)} records were created."
        )
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

    # Update to A records (required for root domain)
    success = update_to_a_records(api_token, account_id, DOMAIN)

    if success:
        print(f"\n✓ DNS configuration complete for {DOMAIN}")
        print("\nNext steps:")
        print("1. Wait a few minutes for DNS propagation")
        print(
            "2. Configure GitHub Pages for markmhendrickson.com in repository settings"
        )
        print("3. GitHub Pages should automatically configure SSL")
    else:
        print("\n✗ DNS configuration failed. Please check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
