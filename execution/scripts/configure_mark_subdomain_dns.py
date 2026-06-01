#!/usr/bin/env python3
"""
Configure DNS records for mark.hendricksonserrano.com via DNSimple API.

This script creates A records pointing to GitHub Pages IPs for the mark subdomain.

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


def load_token_from_env():
    """Load DNSimple API token from .env file."""
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


def get_dnsimple_token_from_1password():
    """Get DNSimple API token from 1Password."""
    try:
        from scripts.credentials import get_credential, get_credential_by_domain

        field_names = ["access token", "api_token", "token", "api token"]
        for field_name in field_names:
            try:
                token = get_credential("DNSimple", field=field_name)
                if token:
                    return token
            except (ValueError, KeyError):
                continue

        for field_name in field_names:
            try:
                token = get_credential_by_domain("dnsimple.com", field=field_name)
                if token:
                    return token
            except (ValueError, KeyError):
                continue

        return None
    except Exception as e:
        print(f"Error retrieving DNSimple API token from 1Password: {e}")
        return None


def get_dnsimple_token():
    """Get DNSimple API token from .env file or 1Password (and cache to .env)."""
    token = load_token_from_env()
    if token:
        print("✓ API token loaded from .env")
        return token

    print("Token not found in .env, fetching from 1Password...")
    token = get_dnsimple_token_from_1password()

    if token:
        # Save to .env for future use
        try:
            existing_lines = []
            if ENV_FILE.exists():
                with open(ENV_FILE) as f:
                    existing_lines = f.readlines()

            existing_lines = [
                line
                for line in existing_lines
                if not line.strip().startswith("DNSIMPLE_API_TOKEN=")
            ]
            existing_lines.append(f"DNSIMPLE_API_TOKEN={token}\n")

            with open(ENV_FILE, "w") as f:
                f.writelines(existing_lines)

            import os

            os.chmod(ENV_FILE, 0o600)
            print("✓ Token saved to .env file for future use")
        except Exception as e:
            print(f"Warning: Could not save token to .env: {e}")
    else:
        print("\nTo set up:")
        print("1. Create a 1Password item titled 'DNSimple' or with URL 'dnsimple.com'")
        print(
            "2. Add a field labeled 'access token', 'api_token', or 'token' with your DNSimple API token"
        )
        print("3. Get your API token from: https://dnsimple.com/user")

    return token


def get_account_id(api_token):
    """Get DNSimple account ID."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    response = requests.get(f"{DNSIMPLE_API_BASE}/whoami", headers=headers)

    if response.status_code != 200:
        print(f"API Error: {response.status_code}")
        print(f"Response: {response.text}")
        response.raise_for_status()

    data = response.json()

    if not data or "data" not in data:
        raise ValueError("Invalid API response: missing 'data' key")

    account = data["data"].get("account")
    if account and account.get("id"):
        return account["id"]

    print("Account not in whoami response, listing accounts...")
    response = requests.get(f"{DNSIMPLE_API_BASE}/accounts", headers=headers)

    if response.status_code != 200:
        print(f"API Error listing accounts: {response.status_code}")
        response.raise_for_status()

    accounts_data = response.json()
    if not accounts_data or "data" not in accounts_data or not accounts_data["data"]:
        raise ValueError("No accounts found.")

    account_id = accounts_data["data"][0]["id"]
    print(f"Using account ID: {account_id}")
    return account_id


try:
    import requests
except ImportError:
    print("Error: requests library not installed. Run: pip install requests")
    sys.exit(1)


# GitHub Pages A record IPs
GITHUB_PAGES_IPS = [
    "185.199.108.153",
    "185.199.109.153",
    "185.199.110.153",
    "185.199.111.153",
]

# Domain and subdomain
DOMAIN = "hendricksonserrano.com"
SUBDOMAIN = "mark"
FULL_SUBDOMAIN = f"{SUBDOMAIN}.{DOMAIN}"


def list_dns_records(api_token, account_id, domain_name):
    """List all DNS records for a domain."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    records = []
    page = 1

    while True:
        response = requests.get(
            f"{DNSIMPLE_API_BASE}/{account_id}/zones/{domain_name}/records",
            headers=headers,
            params={"page": page, "per_page": 100},
        )

        if response.status_code != 200:
            print(f"Error listing DNS records: {response.status_code}")
            print(f"Response: {response.text}")
            response.raise_for_status()

        data = response.json()
        records.extend(data.get("data", []))

        pagination = data.get("pagination", {})
        if pagination.get("current_page", 0) >= pagination.get("total_pages", 1):
            break

        page += 1

    return records


def delete_dns_record(api_token, account_id, domain_name, record_id):
    """Delete a DNS record."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    response = requests.delete(
        f"{DNSIMPLE_API_BASE}/{account_id}/zones/{domain_name}/records/{record_id}",
        headers=headers,
    )

    if response.status_code not in [200, 204]:
        print(f"Error deleting DNS record: {response.status_code}")
        print(f"Response: {response.text}")
        return False

    return True


def create_dns_record(
    api_token, account_id, domain_name, name, record_type, content, ttl=3600
):
    """Create a DNS record."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    data = {
        "name": name,
        "type": record_type,
        "content": content,
        "ttl": ttl,
    }

    response = requests.post(
        f"{DNSIMPLE_API_BASE}/{account_id}/zones/{domain_name}/records",
        headers=headers,
        json=data,
    )

    if response.status_code not in [200, 201]:
        print(f"Error creating DNS record: {response.status_code}")
        print(f"Response: {response.text}")
        return None

    return response.json().get("data")


def configure_mark_subdomain(api_token, account_id, domain_name, subdomain):
    """Configure DNS records for mark subdomain pointing to GitHub Pages."""
    print(f"\nConfiguring DNS for {subdomain}.{domain_name}...")

    # List existing records for the subdomain
    print("Checking existing DNS records...")
    existing_records = list_dns_records(api_token, account_id, domain_name)

    # Find existing A or CNAME records for the subdomain
    subdomain_records = [
        r
        for r in existing_records
        if r.get("name") == subdomain and r.get("type") in ["A", "CNAME"]
    ]

    if subdomain_records:
        print(f"Found {len(subdomain_records)} existing record(s) for {subdomain}:")
        for record in subdomain_records:
            print(
                f"  - {record.get('type')} {record.get('name')} -> {record.get('content')} (ID: {record.get('id')})"
            )

        response = input("\nDelete existing records and create new A records? (y/n): ")
        if response.lower() != "y":
            print("Aborted.")
            return False

        # Delete existing records
        for record in subdomain_records:
            print(f"Deleting {record.get('type')} record (ID: {record.get('id')})...")
            if delete_dns_record(api_token, account_id, domain_name, record.get("id")):
                print("  ✓ Deleted")
            else:
                print("  ✗ Failed to delete")

    # Create A records for GitHub Pages
    print("\nCreating A records pointing to GitHub Pages...")
    created_records = []

    for ip in GITHUB_PAGES_IPS:
        print(f"Creating A record: {subdomain} -> {ip}...")
        record = create_dns_record(
            api_token,
            account_id,
            domain_name,
            name=subdomain,
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
            f"\n✓ Successfully configured {len(created_records)} A records for {subdomain}.{domain_name}"
        )
        print("\nDNS records created:")
        for record in created_records:
            print(f"  - A {subdomain} -> {record.get('content')}")
        print("\nNote: DNS propagation may take up to 48 hours (usually much faster).")
        print(
            "Once DNS propagates, GitHub Pages will automatically provision an SSL certificate."
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
        print("Please ensure:")
        print("1. 1Password CLI is installed and authenticated")
        print("2. A 1Password item exists for DNSimple with API token")
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

    # Configure the mark subdomain
    success = configure_mark_subdomain(api_token, account_id, DOMAIN, SUBDOMAIN)

    if success:
        print(f"\n✓ DNS configuration complete for {FULL_SUBDOMAIN}")
        print("\nNext steps:")
        print(
            "1. Wait for DNS propagation (check with: dig +short mark.hendricksonserrano.com)"
        )
        print(
            "2. Ensure GitHub Pages is configured with custom domain: mark.hendricksonserrano.com"
        )
        print(
            "3. GitHub will automatically provision SSL certificate once DNS propagates"
        )
    else:
        print("\n✗ DNS configuration incomplete. Please check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
