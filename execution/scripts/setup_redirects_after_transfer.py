#!/usr/bin/env python3
"""
Proactively set up DNS forwarding/redirects for markmhendrickson.com and markh.io
once they're transferred to DNSimple.

This script will:
1. Check if domains are available in DNSimple
2. Configure DNS records for GitHub Pages redirects
3. Verify the configuration

Run this script after domain transfers complete.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import DNS configuration functions
try:
    from scripts.configure_markmhendrickson_redirect_dns import (
        DOMAIN as MARKMHENDRIKSON_DOMAIN,
    )
    from scripts.configure_markmhendrickson_redirect_dns import (
        get_account_id,
        get_dnsimple_token,
        update_to_a_records,
    )

    # Import A records function for markh.io (root domain needs A records, not CNAME)
    MARKH_DOMAIN = "markh.io"
except ImportError as e:
    print(f"Error importing DNS configuration functions: {e}")
    sys.exit(1)

import requests

DNSIMPLE_API_BASE = "https://api.dnsimple.com/v2"


def check_domain_available(api_token, account_id, domain_name):
    """Check if a domain is available in DNSimple."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    try:
        # Try to list DNS records (will fail if domain not in DNSimple)
        response = requests.get(
            f"{DNSIMPLE_API_BASE}/{account_id}/zones/{domain_name}/records",
            headers=headers,
            params={"per_page": 1},
        )

        if response.status_code == 200:
            return True
        elif response.status_code == 404:
            return False
        else:
            print(f"  Warning: Unexpected status {response.status_code}")
            return False
    except Exception as e:
        print(f"  Error checking domain: {e}")
        return False


def setup_markmhendrickson_redirect(api_token, account_id):
    """Set up DNS for markmhendrickson.com redirect."""
    print(f"\n{'=' * 80}")
    print("Setting up markmhendrickson.com redirect")
    print(f"{'=' * 80}")

    if not check_domain_available(api_token, account_id, MARKMHENDRIKSON_DOMAIN):
        print(f"✗ {MARKMHENDRIKSON_DOMAIN} not yet available in DNSimple")
        print("  Waiting for transfer to complete...")
        return False

    print(f"✓ {MARKMHENDRIKSON_DOMAIN} is available in DNSimple")
    print("Configuring A records for GitHub Pages...")

    success = update_to_a_records(api_token, account_id, MARKMHENDRIKSON_DOMAIN)

    if success:
        print(f"\n✓ DNS configured for {MARKMHENDRIKSON_DOMAIN}")
        print("  Next: Configure GitHub Pages custom domain in repository settings")
        return True
    else:
        print(f"\n✗ Failed to configure DNS for {MARKMHENDRIKSON_DOMAIN}")
        return False


def setup_markh_redirect(api_token, account_id):
    """Set up DNS for markh.io redirect."""
    print(f"\n{'=' * 80}")
    print("Setting up markh.io redirect")
    print(f"{'=' * 80}")

    if not check_domain_available(api_token, account_id, MARKH_DOMAIN):
        print(f"✗ {MARKH_DOMAIN} not yet available in DNSimple")
        print("  Waiting for transfer to complete...")
        return False

    print(f"✓ {MARKH_DOMAIN} is available in DNSimple")
    print("Configuring A records for GitHub Pages (root domain requires A records)...")

    success = update_to_a_records(api_token, account_id, MARKH_DOMAIN)

    if success:
        print(f"\n✓ DNS configured for {MARKH_DOMAIN}")
        print("  Next: Configure GitHub Pages custom domain in repository settings")
        return True
    else:
        print(f"\n✗ Failed to configure DNS for {MARKH_DOMAIN}")
        return False


def main():
    """Main function."""
    print("=" * 80)
    print("PROACTIVE REDIRECT SETUP")
    print("=" * 80)
    print(
        "\nThis script will configure DNS for redirects once domains are transferred."
    )
    print("Domains: markmhendrickson.com, markh.io")
    print("Target: mark.hendricksonserrano.com")

    api_token = get_dnsimple_token()
    if not api_token:
        print("\nError: Could not retrieve DNSimple API token.")
        sys.exit(1)

    try:
        account_id = get_account_id(api_token)
        print(f"\n✓ Account ID: {account_id}")
    except Exception as e:
        print(f"Error fetching account: {e}")
        sys.exit(1)

    # Check and configure each domain
    markmhendrickson_ready = setup_markmhendrickson_redirect(api_token, account_id)
    markh_ready = setup_markh_redirect(api_token, account_id)

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    if markmhendrickson_ready:
        print("✓ markmhendrickson.com: DNS configured")
    else:
        print("⏳ markmhendrickson.com: Waiting for transfer to complete")

    if markh_ready:
        print("✓ markh.io: DNS configured")
    else:
        print("⏳ markh.io: Waiting for transfer to complete")

    if markmhendrickson_ready and markh_ready:
        print("\n✓ Both domains configured!")
        print("\nNext steps:")
        print(
            "1. Wait for DNS propagation (check with: dig +short markmhendrickson.com)"
        )
        print("2. Configure GitHub Pages custom domains in repository settings")
        print("3. GitHub will automatically provision SSL certificates")
    else:
        print("\n⏳ Some domains still transferring")
        print("Run this script again after transfers complete to configure DNS")


if __name__ == "__main__":
    main()
