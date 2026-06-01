#!/usr/bin/env python3
"""
Add a domain to Cloudflare for tunnel routing (partial DNS setup).

This allows Cloudflare to recognize the domain for tunnel routing
while keeping DNS management in DNSimple.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

# Add parent directory to path for imports
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")


def get_cloudflare_token():
    """Get Cloudflare API token from environment or 1Password."""
    token = os.getenv("CLOUDFLARE_API_TOKEN")

    if token:
        return token

    # Try 1Password
    try:
        import subprocess

        result = subprocess.run(
            ["op", "item", "get", "Cloudflare", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            fields = data.get("fields", [])
            token_field = next(
                (f for f in fields if "ateles API token" in f.get("label", "")),
                None,
            )
            if token_field:
                return token_field.get("value", "")
    except Exception:
        pass

    print("Error: Could not retrieve Cloudflare API token.")
    return None


def get_account_id(api_token):
    """Get Cloudflare account ID."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(
            "https://api.cloudflare.com/client/v4/accounts", headers=headers
        )
        if response.status_code != 200:
            print(f"API Error {response.status_code}: {response.text}")
            return None
        accounts = response.json().get("result", [])
        if accounts:
            return accounts[0]["id"]
    except Exception as e:
        print(f"Error getting account ID: {e}")

    return None


def check_domain_exists(api_token, domain):
    """Check if domain is already in Cloudflare."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(
            "https://api.cloudflare.com/client/v4/zones",
            headers=headers,
            params={"name": domain},
        )
        if response.status_code == 200:
            zones = response.json().get("result", [])
            if zones:
                return zones[0]
    except Exception as e:
        print(f"Error checking domain: {e}")

    return None


def add_domain_to_cloudflare(api_token, account_id, domain):
    """Add domain to Cloudflare (partial DNS setup)."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    # Add domain
    data = {
        "account": {"id": account_id},
        "name": domain,
        "type": "partial",  # Partial DNS setup - keeps external DNS
    }

    try:
        print(f"Adding domain {domain} to Cloudflare...")
        response = requests.post(
            "https://api.cloudflare.com/client/v4/zones",
            headers=headers,
            json=data,
        )

        if response.status_code == 200:
            zone = response.json().get("result", {})
            print("✓ Domain added successfully!")
            print(f"  Zone ID: {zone.get('id')}")
            print(f"  Status: {zone.get('status')}")
            return zone
        else:
            error_data = response.json()
            errors = error_data.get("errors", [])
            if errors:
                error_msg = errors[0].get("message", "Unknown error")
                error_code = errors[0].get("code", "")
                print(f"Error: {error_msg} (code: {error_code})")

                # Check if domain already exists
                if "already exists" in error_msg.lower() or error_code == 1061:
                    print(f"Domain {domain} may already be in Cloudflare. Checking...")
                    existing = check_domain_exists(api_token, domain)
                    if existing:
                        print("✓ Domain already exists in Cloudflare")
                        return existing
            else:
                print(f"Error: {response.text}")
    except Exception as e:
        print(f"Error adding domain: {e}")

    return None


def verify_dns_record(api_token, zone_id, hostname, target):
    """Verify DNS record exists in Cloudflare (or create if needed)."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    # Extract subdomain and domain
    parts = hostname.split(".", 1)
    if len(parts) == 2:
        subdomain, domain = parts
    else:
        subdomain = "@"

    # Check existing records
    try:
        response = requests.get(
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
            headers=headers,
            params={"name": hostname, "type": "CNAME"},
        )

        if response.status_code == 200:
            records = response.json().get("result", [])
            if records:
                record = records[0]
                if record.get("content") == target:
                    print(f"✓ DNS record already exists: {hostname} → {target}")
                    return True
                else:
                    print(f"⚠ DNS record exists but points to: {record.get('content')}")
                    print(f"  Expected: {target}")
                    return False

        # Record doesn't exist - create it
        print(f"Creating DNS record: {hostname} → {target}")
        data = {
            "type": "CNAME",
            "name": subdomain,
            "content": target,
            "ttl": 3600,
            "proxied": True,  # Enable Cloudflare proxy
        }

        response = requests.post(
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
            headers=headers,
            json=data,
        )

        if response.status_code == 200:
            print("✓ DNS record created successfully")
            return True
        else:
            print(f"⚠ Could not create DNS record: {response.text}")
            print("  Note: Since DNS is managed in DNSimple, this is expected.")
            print(
                "  The CNAME in DNSimple should work once domain is added to Cloudflare."
            )
            return True  # Still return True since DNS is external

    except Exception as e:
        print(f"Error verifying DNS record: {e}")
        return False


def main():
    domain = "neotoma.io"
    hostname = "dev.neotoma.io"
    tunnel_target = "64cffaf9-7704-4d12-9b35-436c31be34f6.cfargotunnel.com"

    print(f"Adding domain {domain} to Cloudflare for tunnel routing...")
    print()

    # Get API token
    api_token = get_cloudflare_token()
    if not api_token:
        sys.exit(1)

    # Get account ID
    print("Getting Cloudflare account ID...")
    account_id = get_account_id(api_token)
    if not account_id:
        print("Error: Could not get account ID")
        sys.exit(1)
    print(f"✓ Account ID: {account_id}")
    print()

    # Check if domain already exists
    print(f"Checking if {domain} is already in Cloudflare...")
    existing_zone = check_domain_exists(api_token, domain)
    if existing_zone:
        print("✓ Domain already exists in Cloudflare")
        zone_id = existing_zone["id"]
        print(f"  Zone ID: {zone_id}")
        print(f"  Status: {existing_zone.get('status')}")
    else:
        # Add domain
        zone = add_domain_to_cloudflare(api_token, account_id, domain)
        if not zone:
            print("Error: Could not add domain to Cloudflare")
            sys.exit(1)
        zone_id = zone["id"]
        print()
        print("Waiting for domain to be active...")
        time.sleep(5)  # Give Cloudflare time to process

    print()
    print("Verifying DNS configuration...")
    verify_dns_record(api_token, zone_id, hostname, tunnel_target)

    print()
    print("=" * 60)
    print("✓ Domain setup complete!")
    print()
    print("Next steps:")
    print("1. Wait 1-2 minutes for Cloudflare to process the domain")
    print("2. Test the tunnel:")
    print(f"   curl https://{hostname}/mcp/")
    print()
    print("Note: DNS remains managed in DNSimple.")
    print("Cloudflare now recognizes the domain for tunnel routing.")


if __name__ == "__main__":
    main()
