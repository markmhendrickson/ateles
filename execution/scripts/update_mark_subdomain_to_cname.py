#!/usr/bin/env python3
"""
Update DNS records for mark.hendricksonserrano.com from A records to CNAME via DNSimple API.

This script deletes the existing A records and creates a CNAME record pointing to GitHub Pages.

Requirements:
    - DNSimple API token stored in 1Password (item titled "DNSimple" or "dnsimple.com")
    - requests library: pip install requests
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# DNSimple API configuration
DNSIMPLE_API_BASE = "https://api.dnsimple.com/v2"
ENV_FILE = Path(__file__).parent.parent / ".env"

# Import from configure script (which has all the DNSimple functions)
try:
    from scripts.configure_mark_subdomain_dns import (
        create_dns_record,
        delete_dns_record,
        get_account_id,
        get_dnsimple_token,
        list_dns_records,
    )
except ImportError:
    # If import fails, we'll define them inline
    import requests

    def load_token_from_env():
        if not ENV_FILE.exists():
            return None
        try:
            with open(ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DNSIMPLE_API_TOKEN="):
                        token = line.split("=", 1)[1].strip()
                        if token.startswith('"') and token.endswith('"'):
                            token = token[1:-1]
                        elif token.startswith("'") and token.endswith("'"):
                            token = token[1:-1]
                        return token
        except Exception:
            pass
        return None

    def get_dnsimple_token():
        token = load_token_from_env()
        if token:
            return token
        # Try 1Password if needed
        try:
            from scripts.credentials import get_credential

            field_names = ["access token", "api_token", "token", "api token"]
            for field_name in field_names:
                try:
                    token = get_credential("DNSimple", field=field_name)
                    if token:
                        return token
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def get_account_id(api_token):
        headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
        response = requests.get(f"{DNSIMPLE_API_BASE}/whoami", headers=headers)
        if response.status_code != 200:
            response.raise_for_status()
        data = response.json()
        account = data["data"].get("account")
        if account and account.get("id"):
            return account["id"]
        response = requests.get(f"{DNSIMPLE_API_BASE}/accounts", headers=headers)
        response.raise_for_status()
        accounts_data = response.json()
        return accounts_data["data"][0]["id"]

    def list_dns_records(api_token, account_id, domain_name):
        headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
        records = []
        page = 1
        while True:
            response = requests.get(
                f"{DNSIMPLE_API_BASE}/{account_id}/zones/{domain_name}/records",
                headers=headers,
                params={"page": page, "per_page": 100},
            )
            response.raise_for_status()
            data = response.json()
            records.extend(data.get("data", []))
            pagination = data.get("pagination", {})
            if pagination.get("current_page", 0) >= pagination.get("total_pages", 1):
                break
            page += 1
        return records

    def delete_dns_record(api_token, account_id, domain_name, record_id):
        headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
        response = requests.delete(
            f"{DNSIMPLE_API_BASE}/{account_id}/zones/{domain_name}/records/{record_id}",
            headers=headers,
        )
        return response.status_code in [200, 204]

    def create_dns_record(
        api_token, account_id, domain_name, name, record_type, content, ttl=3600
    ):
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        data = {"name": name, "type": record_type, "content": content, "ttl": ttl}
        response = requests.post(
            f"{DNSIMPLE_API_BASE}/{account_id}/zones/{domain_name}/records",
            headers=headers,
            json=data,
        )
        if response.status_code not in [200, 201]:
            return None
        return response.json().get("data")


# Domain and subdomain configuration
DOMAIN = "hendricksonserrano.com"
SUBDOMAIN = "mark"
FULL_SUBDOMAIN = f"{SUBDOMAIN}.{DOMAIN}"
GITHUB_PAGES_TARGET = "markmhendrickson.github.io"


def update_to_cname(api_token, account_id, domain_name, subdomain):
    """Update DNS from A records to CNAME for GitHub Pages."""
    print(f"\nUpdating DNS for {subdomain}.{domain_name} to use CNAME...")

    # List existing records
    print("Checking existing DNS records...")
    existing_records = list_dns_records(api_token, account_id, domain_name)

    # Find existing A or CNAME records for the subdomain
    subdomain_records = [
        r
        for r in existing_records
        if r.get("name") == subdomain and r.get("type") in ["A", "CNAME"]
    ]

    # Check if CNAME already exists and is correct
    cname_record = next(
        (r for r in subdomain_records if r.get("type") == "CNAME"), None
    )
    if cname_record and cname_record.get("content") == GITHUB_PAGES_TARGET + ".":
        print(
            f"✓ CNAME record already exists and is correct: {subdomain} -> {GITHUB_PAGES_TARGET}"
        )
        return True

    if subdomain_records:
        print(f"Found {len(subdomain_records)} existing record(s) for {subdomain}:")
        for record in subdomain_records:
            print(
                f"  - {record.get('type')} {record.get('name')} -> {record.get('content')} (ID: {record.get('id')})"
            )

        print(
            f"\nWill delete existing records and create CNAME: {subdomain} -> {GITHUB_PAGES_TARGET}"
        )
        print("Proceeding automatically...")

        # Delete existing records
        for record in subdomain_records:
            print(f"Deleting {record.get('type')} record (ID: {record.get('id')})...")
            if delete_dns_record(api_token, account_id, domain_name, record.get("id")):
                print("  ✓ Deleted")
            else:
                print("  ✗ Failed to delete")

    # Create CNAME record
    print(f"\nCreating CNAME record: {subdomain} -> {GITHUB_PAGES_TARGET}...")
    record = create_dns_record(
        api_token,
        account_id,
        domain_name,
        name=subdomain,
        record_type="CNAME",
        content=GITHUB_PAGES_TARGET,
        ttl=3600,
    )

    if record:
        print(f"  ✓ Created (ID: {record.get('id')})")
        print(f"\n✓ Successfully configured CNAME record for {subdomain}.{domain_name}")
        print(f"  CNAME: {subdomain} -> {GITHUB_PAGES_TARGET}")
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
    success = update_to_cname(api_token, account_id, DOMAIN, SUBDOMAIN)

    if success:
        print(f"\n✓ DNS update complete for {FULL_SUBDOMAIN}")
        print("\nNext steps:")
        print("1. Wait a few minutes for DNS propagation")
        print("2. Check DNS: dig +short mark.hendricksonserrano.com")
        print("3. GitHub Pages should automatically configure SSL")
    else:
        print("\n✗ DNS update failed. Please check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
