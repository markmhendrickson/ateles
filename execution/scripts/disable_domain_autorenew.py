#!/usr/bin/env python3
"""
Disable auto-renew for specified DNSimple domains.

Usage:
    python execution/scripts/disable_domain_autorenew.py humans.name bimbacoin.com
"""

import json
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from scripts.check_dnsimple_costs import (
        DNSIMPLE_API_BASE,
        get_account_id,
        get_dnsimple_token,
    )
except ImportError:
    print("Error: Could not import DNSimple functions.")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Error: requests library not installed. Run: pip install requests")
    sys.exit(1)


def disable_autorenew(api_token, account_id, domain_name):
    """Disable auto-renew for a domain."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Disable auto-renew by setting it to false
    data = {"auto_renew": False}

    # Use PATCH to update domain registration settings
    response = requests.patch(
        f"{DNSIMPLE_API_BASE}/{account_id}/registrar/domains/{domain_name}",
        headers=headers,
        json=data,
    )

    if response.status_code == 200:
        return True, None
    elif response.status_code == 404:
        return (
            False,
            f"Domain {domain_name} not found or not registered through DNSimple",
        )
    else:
        return False, f"API Error {response.status_code}: {response.text}"


def main():
    """Main function."""
    if len(sys.argv) < 2:
        print(
            "Usage: python execution/scripts/disable_domain_autorenew.py <domain1> [domain2] ..."
        )
        print(
            "Example: python execution/scripts/disable_domain_autorenew.py humans.name bimbacoin.com"
        )
        sys.exit(1)

    domains_to_disable = sys.argv[1:]

    print("Fetching DNSimple API token...")
    api_token = get_dnsimple_token()

    if not api_token:
        print("Error: Could not retrieve DNSimple API token.")
        sys.exit(1)

    print("Fetching account information...")
    try:
        account_id = get_account_id(api_token)
        print(f"✓ Account ID: {account_id}\n")
    except Exception as e:
        print(f"Error fetching account: {e}")
        sys.exit(1)

    print("=" * 80)
    print("DISABLING AUTO-RENEW FOR DOMAINS")
    print("=" * 80)
    print()

    results = []
    for domain_name in domains_to_disable:
        print(f"Processing: {domain_name}")
        success, error = disable_autorenew(api_token, account_id, domain_name)

        if success:
            print(f"  ✓ Auto-renew disabled for {domain_name}")
            results.append({"domain": domain_name, "status": "disabled", "error": None})
        else:
            print(f"  ✗ Failed to disable auto-renew for {domain_name}: {error}")
            results.append({"domain": domain_name, "status": "failed", "error": error})
        print()

    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    successful = [r for r in results if r["status"] == "disabled"]
    failed = [r for r in results if r["status"] == "failed"]

    print(f"Successfully disabled: {len(successful)}")
    for r in successful:
        print(f"  ✓ {r['domain']}")

    if failed:
        print(f"\nFailed: {len(failed)}")
        for r in failed:
            print(f"  ✗ {r['domain']}: {r['error']}")

    # Save results
    from scripts.config import get_data_dir

    output_file = get_data_dir() / "logs" / "domain_autorenew_disabled.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w") as f:
        json.dump(
            {
                "timestamp": str(Path(__file__).stat().st_mtime),
                "results": results,
            },
            f,
            indent=2,
        )

    print(f"\nResults saved to: {output_file}")


if __name__ == "__main__":
    main()
