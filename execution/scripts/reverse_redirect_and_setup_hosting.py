#!/usr/bin/env python3
"""
Reverse redirect direction and set up website hosting on markmhendrickson.com

This script will:
1. Delete CNAME record for mark.hendricksonserrano.com
2. Create URL redirect from mark.hendricksonserrano.com to https://markmhendrickson.com
3. Delete URL redirect from markmhendrickson.com
4. Set up markmhendrickson.com for GitHub Pages hosting (A records)

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
SUBDOMAIN_DOMAIN = "hendricksonserrano.com"
SUBDOMAIN_NAME = "mark"
ROOT_DOMAIN = "markmhendrickson.com"
REDIRECT_TARGET = "https://markmhendrickson.com"

# GitHub Pages A record IPs (for website hosting)
GITHUB_PAGES_IPS = [
    "185.199.108.153",
    "185.199.109.153",
    "185.199.110.153",
    "185.199.111.153",
]


def setup_subdomain_redirect(
    api_token, account_id, domain_name, subdomain_name, target_url
):
    """Set up URL redirect for subdomain."""
    print(f"\n{'=' * 80}")
    print(f"Setting up redirect: {subdomain_name}.{domain_name} -> {target_url}")
    print(f"{'=' * 80}")

    # List existing records
    records = list_dns_records(api_token, account_id, domain_name)
    subdomain_records = [r for r in records if r.get("name") == subdomain_name]

    # Find existing CNAME or URL records
    cname_records = [r for r in subdomain_records if r.get("type") == "CNAME"]
    url_records = [r for r in subdomain_records if r.get("type") == "URL"]

    # Delete CNAME records
    if cname_records:
        print(f"\nFound {len(cname_records)} CNAME record(s) to delete:")
        for record in cname_records:
            print(
                f"  - CNAME {subdomain_name} -> {record.get('content')} (ID: {record.get('id')})"
            )
        print("Deleting CNAME records...")
        for record in cname_records:
            if delete_dns_record(api_token, account_id, domain_name, record.get("id")):
                print(f"  ✓ Deleted CNAME (ID: {record.get('id')})")
            else:
                print(f"  ✗ Failed to delete CNAME (ID: {record.get('id')})")
                return False

    # Check if URL redirect already exists and is correct
    if url_records:
        existing_url = url_records[0].get("content", "")
        if existing_url == target_url or existing_url.rstrip("/") == target_url.rstrip(
            "/"
        ):
            print(f"\n✓ URL redirect already exists and is correct: -> {target_url}")
            return True
        else:
            print(f"\nFound existing URL redirect pointing to: {existing_url}")
            print(f"Will update to: {target_url}")
            # Delete old URL redirect
            for record in url_records:
                if delete_dns_record(
                    api_token, account_id, domain_name, record.get("id")
                ):
                    print(f"  ✓ Deleted old URL redirect (ID: {record.get('id')})")
                else:
                    print("  ✗ Failed to delete URL redirect")
                    return False

    # Create URL redirect record
    print(f"\nCreating URL redirect record: {subdomain_name} -> {target_url}...")
    record = create_dns_record(
        api_token,
        account_id,
        domain_name,
        name=subdomain_name,
        record_type="URL",
        content=target_url,
        ttl=3600,
    )

    if record:
        print(f"  ✓ Created URL redirect (ID: {record.get('id')})")
        return True
    else:
        print("  ✗ Failed to create URL redirect")
        return False


def setup_website_hosting(api_token, account_id, domain_name):
    """Set up GitHub Pages A records for website hosting."""
    print(f"\n{'=' * 80}")
    print(f"Setting up website hosting for {domain_name}")
    print(f"{'=' * 80}")

    # List existing records
    records = list_dns_records(api_token, account_id, domain_name)
    root_records = [r for r in records if (r.get("name") == "" or r.get("name") == "@")]

    # Find existing URL redirect records (need to delete these)
    url_records = [r for r in root_records if r.get("type") == "URL"]

    # Find existing A records
    a_records = [r for r in root_records if r.get("type") == "A"]

    # Delete URL redirect records
    if url_records:
        print(f"\nFound {len(url_records)} URL redirect record(s) to delete:")
        for record in url_records:
            print(f"  - URL @ -> {record.get('content')} (ID: {record.get('id')})")
        print("Deleting URL redirect records...")
        for record in url_records:
            if delete_dns_record(api_token, account_id, domain_name, record.get("id")):
                print(f"  ✓ Deleted URL redirect (ID: {record.get('id')})")
            else:
                print("  ✗ Failed to delete URL redirect")
                return False

    # Check if A records already point to GitHub Pages IPs
    existing_ips = {r.get("content") for r in a_records}
    github_ips_set = set(GITHUB_PAGES_IPS)

    if existing_ips == github_ips_set and len(a_records) == len(GITHUB_PAGES_IPS):
        print("\n✓ A records already exist and point to GitHub Pages IPs")
        return True

    # Delete existing A records if they don't match
    if a_records:
        print(f"\nFound {len(a_records)} existing A record(s):")
        for record in a_records:
            print(f"  - A @ -> {record.get('content')} (ID: {record.get('id')})")
        print("Deleting existing A records...")
        for record in a_records:
            if delete_dns_record(api_token, account_id, domain_name, record.get("id")):
                print(f"  ✓ Deleted A record (ID: {record.get('id')})")
            else:
                print("  ✗ Failed to delete A record")

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
        print("\nNote: Configure GitHub Pages custom domain in repository settings:")
        print("  https://github.com/markmhendrickson/hendricksonserrano/settings/pages")
        print("  Enter custom domain: markmhendrickson.com")
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

    # Step 1: Set up redirect from mark.hendricksonserrano.com to markmhendrickson.com
    print("\n" + "=" * 80)
    print("STEP 1: Set up redirect from mark.hendricksonserrano.com")
    print("=" * 80)
    redirect_success = setup_subdomain_redirect(
        api_token, account_id, SUBDOMAIN_DOMAIN, SUBDOMAIN_NAME, REDIRECT_TARGET
    )

    # Step 2: Set up website hosting on markmhendrickson.com
    print("\n" + "=" * 80)
    print("STEP 2: Set up website hosting on markmhendrickson.com")
    print("=" * 80)
    hosting_success = setup_website_hosting(api_token, account_id, ROOT_DOMAIN)

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    if redirect_success:
        print(
            f"✓ Redirect configured: {SUBDOMAIN_NAME}.{SUBDOMAIN_DOMAIN} -> {REDIRECT_TARGET}"
        )
    else:
        print(f"✗ Failed to configure redirect for {SUBDOMAIN_NAME}.{SUBDOMAIN_DOMAIN}")

    if hosting_success:
        print(f"✓ Website hosting configured for {ROOT_DOMAIN}")
        print("\nNext steps:")
        print("1. Configure GitHub Pages custom domain: markmhendrickson.com")
        print("2. Wait for DNS propagation (may take a few minutes)")
        print("3. GitHub will automatically provision SSL certificate")
    else:
        print(f"✗ Failed to configure website hosting for {ROOT_DOMAIN}")

    if redirect_success and hosting_success:
        print("\n✓ All changes completed successfully!")
    else:
        print("\n✗ Some changes failed. Please check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
