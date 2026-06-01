#!/usr/bin/env python3
"""
Enable whois privacy (domain privacy) for specified DNSimple domains.

Usage:
    python execution/scripts/enable_whois_privacy.py dionysiandesigns.com
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


def get_whois_privacy_status(api_token, account_id, domain_name):
    """Get whois privacy status for a domain."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    response = requests.get(
        f"{DNSIMPLE_API_BASE}/{account_id}/registrar/domains/{domain_name}/whois_privacy",
        headers=headers,
    )

    if response.status_code == 200:
        whois_data = response.json().get("data", {})
        return True, whois_data.get("enabled", False), whois_data, None
    elif response.status_code == 404:
        return False, False, None, "Whois privacy not purchased or not available"
    else:
        error_text = response.text
        try:
            error_json = response.json()
            error_message = error_json.get("message", error_text)
        except Exception:
            error_message = error_text
        return False, False, None, f"API Error {response.status_code}: {error_message}"


def enable_whois_privacy(api_token, account_id, domain_name):
    """Enable whois privacy for a domain."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Enable/purchase whois privacy
    response = requests.put(
        f"{DNSIMPLE_API_BASE}/{account_id}/registrar/domains/{domain_name}/whois_privacy",
        headers=headers,
    )

    if response.status_code in [200, 201]:
        whois_data = response.json().get("data", {})
        return True, whois_data, None
    else:
        error_text = response.text
        try:
            error_json = response.json()
            error_message = error_json.get("message", error_text)
        except Exception:
            error_message = error_text
        return False, None, f"API Error {response.status_code}: {error_message}"


def main():
    """Main function."""
    if len(sys.argv) < 2:
        print(
            "Usage: python execution/scripts/enable_whois_privacy.py <domain1> [domain2] ..."
        )
        print(
            "Example: python execution/scripts/enable_whois_privacy.py dionysiandesigns.com"
        )
        sys.exit(1)

    domains_to_process = sys.argv[1:]

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
    print("CHECKING AND ENABLING WHOIS PRIVACY")
    print("=" * 80)
    print()

    results = []
    for domain_name in domains_to_process:
        print(f"Processing: {domain_name}")

        # Check current status
        found, enabled, whois_data, error = get_whois_privacy_status(
            api_token, account_id, domain_name
        )

        if not found:
            print(f"  ⚠ {error}")
            print("  → Attempting to enable whois privacy...")
        elif enabled:
            print("  ✓ Whois privacy is already enabled")
            if whois_data:
                expires_on = whois_data.get("expires_on")
                if expires_on:
                    print(f"    Expires on: {expires_on}")
            results.append(
                {
                    "domain": domain_name,
                    "status": "already_enabled",
                    "whois_privacy": whois_data,
                    "error": None,
                }
            )
            print()
            continue
        else:
            print("  ⚠ Whois privacy is not enabled")
            print("  → Enabling whois privacy...")

        # Enable whois privacy
        success, whois_data, error = enable_whois_privacy(
            api_token, account_id, domain_name
        )

        if success:
            print(f"  ✓ Whois privacy enabled for {domain_name}")
            if whois_data:
                expires_on = whois_data.get("expires_on")
                if expires_on:
                    print(f"    Expires on: {expires_on}")
            results.append(
                {
                    "domain": domain_name,
                    "status": "enabled",
                    "whois_privacy": whois_data,
                    "error": None,
                }
            )
        else:
            print(f"  ✗ Failed to enable whois privacy for {domain_name}: {error}")
            results.append(
                {
                    "domain": domain_name,
                    "status": "failed",
                    "whois_privacy": None,
                    "error": error,
                }
            )
        print()

    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    already_enabled = [r for r in results if r["status"] == "already_enabled"]
    enabled = [r for r in results if r["status"] == "enabled"]
    failed = [r for r in results if r["status"] == "failed"]

    if already_enabled:
        print(f"Already enabled: {len(already_enabled)}")
        for r in already_enabled:
            print(f"  ✓ {r['domain']}")

    if enabled:
        print(f"\nSuccessfully enabled: {len(enabled)}")
        for r in enabled:
            print(f"  ✓ {r['domain']}")

    if failed:
        print(f"\nFailed: {len(failed)}")
        for r in failed:
            print(f"  ✗ {r['domain']}: {r['error']}")

    # Save results
    from scripts.config import get_data_dir

    output_file = get_data_dir() / "logs" / "whois_privacy_enabled.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w") as f:
        json.dump(
            {
                "timestamp": str(Path(__file__).stat().st_mtime),
                "results": results,
            },
            f,
            indent=2,
            default=str,
        )

    print(f"\nResults saved to: {output_file}")


if __name__ == "__main__":
    main()
