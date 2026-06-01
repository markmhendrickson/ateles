#!/usr/bin/env python3
"""
Add a public hostname to a Cloudflare tunnel via API.

This is needed when the domain is managed by external DNS (e.g., DNSimple)
and you can't use `cloudflared tunnel route dns`.
"""

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

# Add parent directory to path for imports
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")


# Try to get Cloudflare API token from environment or 1Password
def get_cloudflare_token():
    """Get Cloudflare API token from environment or 1Password."""
    token = os.getenv("CLOUDFLARE_API_TOKEN")

    if token:
        return token

    # Try 1Password - check multiple possible field names
    field_names = [
        "API Token",
        "ateles API token (development)",
        "ateles API token",
        "api_token",
        "token",
        "API Key",
        "api_key",
    ]
    for field_name in field_names:
        try:
            import subprocess

            result = subprocess.run(
                [
                    "op",
                    "item",
                    "get",
                    "Cloudflare",
                    "--fields",
                    f"label={field_name},value",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            continue

    print("Error: Could not retrieve Cloudflare API token.")
    print()
    print("To add the hostname, you need a Cloudflare API token.")
    print()
    print("Option 1: Set environment variable:")
    print("  export CLOUDFLARE_API_TOKEN='your-token-here'")
    print()
    print("Option 2: Add to 1Password:")
    print("  1. Open 1Password item 'Cloudflare'")
    print("  2. Add a field labeled 'API Token' with your Cloudflare API token")
    print("  3. Get token from: https://dash.cloudflare.com/profile/api-tokens")
    print("     (Create token with permissions: Account.Cloudflare Tunnel:Edit)")
    print()
    print("Option 3: Add hostname manually in Cloudflare Dashboard:")
    print("  1. Go to: https://one.dash.cloudflare.com/")
    print("  2. Zero Trust → Networks → Tunnels → mcp-servers")
    print("  3. Public Hostnames tab → Add a public hostname")
    print("  4. Hostname: dev.neotoma.io")
    print("  5. Path: /mcp/*")
    print("  6. Service: http://localhost:8080")
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
        response.raise_for_status()
        accounts = response.json().get("result", [])
        if accounts:
            return accounts[0]["id"]
    except Exception as e:
        print(f"Error getting account ID: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"Response: {e.response.text}")

    return None


def get_tunnel_id(api_token, account_id, tunnel_name):
    """Get tunnel ID by name."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/cfd_tunnel",
            headers=headers,
            params={"name": tunnel_name},
        )
        response.raise_for_status()
        tunnels = response.json().get("result", [])
        if tunnels:
            return tunnels[0]["id"]
    except Exception as e:
        print(f"Error getting tunnel ID: {e}")

    return None


def get_tunnel_config(api_token, account_id, tunnel_id):
    """Get current tunnel configuration."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
            headers=headers,
        )
        response.raise_for_status()
        return response.json().get("result", {}).get("config", {})
    except Exception as e:
        print(f"Error getting tunnel config: {e}")
        return {}


def update_tunnel_config(api_token, account_id, tunnel_id, hostname, path, service):
    """Update tunnel configuration to add hostname."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    # Get current config
    current_config = get_tunnel_config(api_token, account_id, tunnel_id)
    ingress = current_config.get("ingress", [])

    # Check if hostname already exists
    for rule in ingress:
        if rule.get("hostname") == hostname and rule.get("path") == path:
            print(f"✓ Hostname {hostname}{path} already exists in tunnel configuration")
            return True

    # Add new ingress rule (before catch-all)
    new_rule = {
        "hostname": hostname,
        "path": path,
        "service": service,
    }

    # Insert before catch-all (last rule)
    if ingress and ingress[-1].get("service") == "http_status:404":
        ingress.insert(-1, new_rule)
    else:
        ingress.append(new_rule)
        # Add catch-all if missing
        if not any(r.get("service") == "http_status:404" for r in ingress):
            ingress.append({"service": "http_status:404"})

    config = {"config": {"ingress": ingress}}

    try:
        response = requests.put(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
            headers=headers,
            json=config,
        )
        response.raise_for_status()
        print(f"✓ Successfully added hostname {hostname}{path} to tunnel configuration")
        return True
    except Exception as e:
        print(f"Error updating tunnel config: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"Response: {e.response.text}")
        return False


def main():
    if len(sys.argv) < 4:
        print(
            "Usage: python add_tunnel_hostname.py <tunnel_name> <hostname> <path> [service]"
        )
        print(
            "Example: python add_tunnel_hostname.py mcp-servers dev.neotoma.io /mcp/* http://localhost:8080"
        )
        sys.exit(1)

    tunnel_name = sys.argv[1]
    hostname = sys.argv[2]
    path = sys.argv[3]
    service = sys.argv[4] if len(sys.argv) > 4 else "http://localhost:8080"

    print(f"Adding hostname {hostname}{path} to tunnel {tunnel_name}...")
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

    # Get tunnel ID
    print(f"Getting tunnel ID for '{tunnel_name}'...")
    tunnel_id = get_tunnel_id(api_token, account_id, tunnel_name)
    if not tunnel_id:
        print(f"Error: Could not find tunnel '{tunnel_name}'")
        sys.exit(1)
    print(f"✓ Tunnel ID: {tunnel_id}")

    # Update tunnel config
    print("Updating tunnel configuration...")
    success = update_tunnel_config(
        api_token, account_id, tunnel_id, hostname, path, service
    )

    if success:
        print()
        print("✓ Hostname added successfully!")
        print()
        print("Next steps:")
        print("1. Restart the tunnel: cloudflared tunnel run mcp-servers")
        print(f"2. Test: curl https://{hostname}{path}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
