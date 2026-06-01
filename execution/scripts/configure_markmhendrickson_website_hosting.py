#!/usr/bin/env python3
"""
Configure markmhendrickson.com to host the personal website (remove redirect, set up hosting).

This script will:
1. Delete existing URL redirect record (if any)
2. Set up A records pointing to GitHub Pages IPs for website hosting
3. Optionally set up CNAME for www subdomain

Requirements:
    - DNSimple API token stored in 1Password (item titled "DNSimple" or "dnsimple.com")
    - requests library: pip install requests
    - GitHub Pages repository configured with custom domain markmhendrickson.com
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
DOMAIN = "markmhendrickson.com"

# GitHub Pages A record IPs (required for root domain)
GITHUB_PAGES_IPS = [
    "185.199.108.153",
    "185.199.109.153",
    "185.199.110.153",
    "185.199.111.153",
]


def configure_website_hosting(api_token, account_id, domain_name):
    """Configure DNS for markmhendrickson.com to host website (remove redirect, add A records)."""
    print(f"\nConfiguring website hosting for {domain_name}...")
    print(
        "This will remove any existing redirect and set up A records for GitHub Pages."
    )

    # List existing records
    print("Checking existing DNS records...")
    existing_records = list_dns_records(api_token, account_id, domain_name)

    # Find existing URL redirect records (root domain)
    root_url_records = [
        r
        for r in existing_records
        if (r.get("name") == "" or r.get("name") == "@") and r.get("type") == "URL"
    ]

    # Find existing A records (root domain)
    root_a_records = [
        r
        for r in existing_records
        if (r.get("name") == "" or r.get("name") == "@") and r.get("type") == "A"
    ]

    # Delete URL redirect if it exists (we want to host, not redirect)
    if root_url_records:
        print(
            f"\nFound {len(root_url_records)} URL redirect record(s) that need to be removed:"
        )
        for record in root_url_records:
            print(f"  - URL @ -> {record.get('content')} (ID: {record.get('id')})")
        print("Deleting URL redirect (we want to host the website, not redirect)...")
        for record in root_url_records:
            print(f"Deleting URL redirect (ID: {record.get('id')})...")
            if delete_dns_record(api_token, account_id, domain_name, record.get("id")):
                print("  ✓ Deleted")
            else:
                print("  ✗ Failed to delete")
                return False

    # Check if A records already exist and point to GitHub Pages
    if root_a_records:
        existing_ips = [r.get("content") for r in root_a_records]
        if set(existing_ips) == set(GITHUB_PAGES_IPS):
            print("\n✓ A records already configured correctly for GitHub Pages")
            print("  Existing A records:")
            for record in root_a_records:
                print(f"    - A @ -> {record.get('content')}")
            return True
        else:
            print(f"\nFound {len(root_a_records)} A record(s) that need to be updated:")
            for record in root_a_records:
                print(f"  - A @ -> {record.get('content')} (ID: {record.get('id')})")
            print("Deleting existing A records to replace with GitHub Pages IPs...")
            for record in root_a_records:
                print(f"Deleting A record (ID: {record.get('id')})...")
                if delete_dns_record(
                    api_token, account_id, domain_name, record.get("id")
                ):
                    print("  ✓ Deleted")
                else:
                    print("  ✗ Failed to delete")
                    return False

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
        print("\nDNS records created:")
        for record in created_records:
            print(f"  - A @ -> {record.get('content')}")
        print("\nNext steps:")
        print(
            "1. Wait for DNS propagation (check with: dig +short markmhendrickson.com)"
        )
        print("2. Configure GitHub Pages custom domain: markmhendrickson.com")
        print("   - Go to repository Settings → Pages")
        print("   - Add custom domain: markmhendrickson.com")
        print("   - Enable 'Enforce HTTPS'")
        print(
            "3. GitHub will automatically provision SSL certificate once DNS propagates"
        )
        print("\nNote: DNS propagation may take up to 48 hours (usually much faster).")
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

    # Configure website hosting
    success = configure_website_hosting(api_token, account_id, DOMAIN)

    if success:
        print(f"\n✓ Website hosting configuration complete for {DOMAIN}")
        print(
            f"\nThe domain {DOMAIN} is now configured to host the website (not redirect)"
        )
        print("Wait for DNS propagation, then configure GitHub Pages custom domain.")
    else:
        print(
            "\n✗ Website hosting configuration failed. Please check the errors above."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
