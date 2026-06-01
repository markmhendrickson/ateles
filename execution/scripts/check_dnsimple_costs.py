#!/usr/bin/env python3
"""
Query DNSimple API for domain pricing and renewal costs.

Requirements:
    - DNSimple API token stored in 1Password (item titled "DNSimple" or "dnsimple.com")
    - requests library: pip install requests
"""

import json
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from scripts.credentials import get_credential, get_credential_by_domain
except ImportError:
    print(
        "Error: Could not import credentials module. Ensure you're running from repo root."
    )
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Error: requests library not installed. Run: pip install requests")
    sys.exit(1)


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
                    # Remove quotes if present
                    if token.startswith('"') and token.endswith('"'):
                        token = token[1:-1]
                    elif token.startswith("'") and token.endswith("'"):
                        token = token[1:-1]
                    return token
    except Exception:
        pass

    return None


def save_token_to_env(token):
    """Save DNSimple API token to .env file."""
    try:
        # Read existing .env content
        existing_lines = []
        if ENV_FILE.exists():
            with open(ENV_FILE) as f:
                existing_lines = f.readlines()

        # Remove any existing DNSIMPLE_API_TOKEN line
        existing_lines = [
            line
            for line in existing_lines
            if not line.strip().startswith("DNSIMPLE_API_TOKEN=")
        ]

        # Add new token line
        existing_lines.append(f"DNSIMPLE_API_TOKEN={token}\n")

        # Write back to file
        with open(ENV_FILE, "w") as f:
            f.writelines(existing_lines)

        # Set restrictive permissions (owner read/write only)
        os.chmod(ENV_FILE, 0o600)
    except Exception as e:
        print(f"Warning: Could not save token to .env file: {e}")


def get_dnsimple_token_from_1password():
    """Get DNSimple API token from 1Password."""
    try:
        # Try to get token from DNSimple item (check multiple field name variations)
        field_names = ["access token", "api_token", "token", "api token"]
        for field_name in field_names:
            try:
                token = get_credential("DNSimple", field=field_name)
                if token:
                    return token
            except (ValueError, KeyError):
                continue

        # Try via domain lookup
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
    # First, try loading from .env
    token = load_token_from_env()
    if token:
        print("✓ API token loaded from .env")
        return token

    # If not in .env, fetch from 1Password
    print("Token not found in .env, fetching from 1Password...")
    token = get_dnsimple_token_from_1password()

    if token:
        # Save to .env for future use
        save_token_to_env(token)
        print("✓ Token saved to .env file for future use")
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

    # First, check whoami to see if account is available
    response = requests.get(f"{DNSIMPLE_API_BASE}/whoami", headers=headers)

    if response.status_code != 200:
        print(f"API Error: {response.status_code}")
        print(f"Response: {response.text}")
        response.raise_for_status()

    try:
        data = response.json()
    except json.JSONDecodeError:
        print(f"Invalid JSON response: {response.text}")
        raise

    if not data or "data" not in data:
        print(f"Unexpected API response structure: {json.dumps(data, indent=2)}")
        raise ValueError("Invalid API response: missing 'data' key")

    # Check if account is directly available
    account = data["data"].get("account")
    if account and account.get("id"):
        return account["id"]

    # If account is null, list accounts and use the first one
    print("Account not in whoami response, listing accounts...")
    response = requests.get(f"{DNSIMPLE_API_BASE}/accounts", headers=headers)

    if response.status_code != 200:
        print(f"API Error listing accounts: {response.status_code}")
        print(f"Response: {response.text}")
        response.raise_for_status()

    accounts_data = response.json()
    if not accounts_data or "data" not in accounts_data or not accounts_data["data"]:
        raise ValueError("No accounts found. You may need to create an account first.")

    # Use the first account
    account_id = accounts_data["data"][0]["id"]
    print(f"Using account ID: {account_id}")
    return account_id


def list_domains(api_token, account_id):
    """List all domains in the account."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    domains = []
    page = 1

    while True:
        response = requests.get(
            f"{DNSIMPLE_API_BASE}/{account_id}/domains",
            headers=headers,
            params={"page": page, "per_page": 100},
        )
        response.raise_for_status()

        data = response.json()
        domains.extend(data["data"])

        pagination = data.get("pagination", {})
        if pagination.get("current_page", 0) >= pagination.get("total_pages", 1):
            break

        page += 1

    return domains


def get_domain_pricing(api_token, account_id, domain_name):
    """Get pricing information for a specific domain."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    tld = domain_name.split(".")[-1]

    # Try to get domain registration info (may fail if not registered through DNSimple)
    domain_data = None
    try:
        response = requests.get(
            f"{DNSIMPLE_API_BASE}/{account_id}/registrar/domains/{domain_name}",
            headers=headers,
        )
        if response.status_code == 200:
            domain_data = response.json()["data"]
    except Exception:
        pass

    # Get TLD pricing (this should work for all TLDs)
    prices_data = []
    try:
        response = requests.get(
            f"{DNSIMPLE_API_BASE}/{account_id}/registrar/tlds/{tld}/prices",
            headers=headers,
        )
        if response.status_code == 200:
            result = response.json()
            prices_data = result.get("data", [])
        elif response.status_code == 404:
            # TLD might not be available through DNSimple
            pass
        else:
            print(
                f"    Warning: TLD pricing API returned {response.status_code}: {response.text[:100]}"
            )
    except Exception as e:
        print(f"    Warning: Error fetching TLD pricing: {e}")

    return {
        "domain": domain_data,
        "prices": prices_data,
    }


def format_price(price_data):
    """Format price for display."""
    if not price_data:
        return "N/A"

    amount = price_data.get("price", 0)
    currency = price_data.get("currency", "USD")
    return f"{amount} {currency}"


def main():
    """Main function."""
    api_token = get_dnsimple_token()

    if not api_token:
        print("\nError: Could not retrieve DNSimple API token.")
        print("Please ensure:")
        print("1. 1Password CLI is installed and authenticated")
        print("2. A 1Password item exists for DNSimple with API token")
        sys.exit(1)

    print()

    print("Fetching account information...")
    try:
        account_id = get_account_id(api_token)
        print(f"✓ Account ID: {account_id}\n")
    except Exception as e:
        print(f"Error fetching account: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    print("Fetching domains...")
    try:
        domains = list_domains(api_token, account_id)
        print(f"✓ Found {len(domains)} domains\n")
    except Exception as e:
        print(f"Error fetching domains: {e}")
        sys.exit(1)

    print("=" * 80)
    print("DOMAIN PRICING SUMMARY")
    print("=" * 80)
    print()

    total_renewal_cost = 0
    domain_details = []

    for domain in domains:
        domain_name = domain["name"]
        expires_at = domain.get("expires_at")
        auto_renew = domain.get("auto_renew", False)

        print(f"Domain: {domain_name}")
        print(f"  Expires: {expires_at or 'N/A'}")
        print(f"  Auto-renew: {auto_renew}")

        try:
            pricing = get_domain_pricing(api_token, account_id, domain_name)

            # Find renewal price
            renewal_price = None
            for price in pricing["prices"]:
                if price.get("operation") == "renew":
                    renewal_price = price
                    break

            if renewal_price:
                amount = float(renewal_price.get("price", 0))
                currency = renewal_price.get("currency", "USD")
                total_renewal_cost += amount

                print(f"  Renewal cost: {amount} {currency}")
                domain_details.append(
                    {
                        "domain": domain_name,
                        "expires_at": expires_at,
                        "auto_renew": auto_renew,
                        "renewal_price": amount,
                        "currency": currency,
                    }
                )
            else:
                print("  Renewal cost: N/A (pricing not available)")
                domain_details.append(
                    {
                        "domain": domain_name,
                        "expires_at": expires_at,
                        "auto_renew": auto_renew,
                        "renewal_price": None,
                        "currency": None,
                    }
                )
        except Exception as e:
            print(f"  Error fetching pricing: {e}")
            domain_details.append(
                {
                    "domain": domain_name,
                    "expires_at": expires_at,
                    "auto_renew": auto_renew,
                    "renewal_price": None,
                    "currency": None,
                    "error": str(e),
                }
            )

        print()

    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total domains: {len(domains)}")
    print(f"Total annual renewal cost: {total_renewal_cost:.2f} USD")
    print()

    # Show humans.name specifically
    humans_domain = next(
        (d for d in domain_details if d["domain"] == "humans.name"), None
    )
    if humans_domain:
        print("humans.name details:")
        print(f"  Expires: {humans_domain['expires_at'] or 'N/A'}")
        print(f"  Auto-renew: {humans_domain['auto_renew']}")
        if humans_domain["renewal_price"]:
            print(
                f"  Annual renewal: {humans_domain['renewal_price']} {humans_domain['currency']}"
            )
        else:
            print("  Annual renewal: N/A")
        print()

    # Save to JSON file
    from scripts.config import get_data_dir

    output_file = get_data_dir() / "logs" / "dnsimple_costs.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    output_data = {
        "total_domains": len(domains),
        "total_annual_renewal_cost": total_renewal_cost,
        "domains": domain_details,
    }

    with open(output_file, "w") as f:
        json.dump(output_data, f, indent=2)

    print(f"Detailed data saved to: {output_file}")


if __name__ == "__main__":
    main()
