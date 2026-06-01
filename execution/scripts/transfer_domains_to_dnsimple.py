#!/usr/bin/env python3
"""
Initiate domain transfers to DNSimple for markh.io and markmhendrickson.com.

This script helps initiate the transfer process. Manual steps are required at Squarespace first.

Requirements:
    - DNSimple API token stored in 1Password (item titled "DNSimple" or "dnsimple.com")
    - Authorization codes (EPP codes) from Squarespace for both domains
    - Domains must be unlocked at Squarespace
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


def get_dnsimple_token():
    """Get DNSimple API token from .env file or 1Password."""
    token = load_token_from_env()
    if token:
        return token
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
    """Get DNSimple account ID."""
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


try:
    import requests
except ImportError:
    print("Error: requests library not installed. Run: pip install requests")
    sys.exit(1)


DOMAINS_TO_TRANSFER = ["markh.io", "markmhendrickson.com"]


def get_transfer_pricing(api_token, account_id, tld):
    """Get transfer pricing for a TLD."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    try:
        response = requests.get(
            f"{DNSIMPLE_API_BASE}/{account_id}/registrar/tlds/{tld}/prices",
            headers=headers,
        )
        if response.status_code == 200:
            data = response.json()
            prices = data.get("data", [])
            transfer_price = next(
                (p for p in prices if p.get("operation") == "transfer"), None
            )
            return transfer_price
        else:
            return None
    except Exception as e:
        print(f"Error fetching transfer pricing: {e}")
        return None


def initiate_transfer(
    api_token, account_id, domain_name, auth_code, registrant_id=None
):
    """Initiate a domain transfer to DNSimple."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    data = {
        "registrant_id": registrant_id,  # Optional, will use account default if not provided
        "auth_code": auth_code,
    }

    # Remove None values
    data = {k: v for k, v in data.items() if v is not None}

    response = requests.post(
        f"{DNSIMPLE_API_BASE}/{account_id}/registrar/domains/{domain_name}/transfers",
        headers=headers,
        json=data,
    )

    if response.status_code in [200, 201]:
        return True, response.json().get("data")
    else:
        error_text = response.text
        try:
            error_json = response.json()
            error_message = error_json.get("message", error_text)
        except Exception:
            error_message = error_text
        return False, f"API Error {response.status_code}: {error_message}"


def get_registrant_id(api_token, account_id):
    """Get the default registrant ID for the account."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }

    try:
        response = requests.get(
            f"{DNSIMPLE_API_BASE}/{account_id}/registrants", headers=headers
        )
        if response.status_code == 200:
            data = response.json()
            registrants = data.get("data", [])
            if registrants:
                return registrants[0].get("id")
        return None
    except Exception as e:
        print(f"Error fetching registrant ID: {e}")
        return None


def main():
    """Main function."""
    print("=" * 80)
    print("DOMAIN TRANSFER TO DNSIMPLE")
    print("=" * 80)
    print("\nDomains to transfer:")
    for domain in DOMAINS_TO_TRANSFER:
        print(f"  - {domain}")

    print("\n" + "=" * 80)
    print("STEP 1: PREPARE DOMAINS AT SQUARESPACE")
    print("=" * 80)
    print("\nBefore initiating the transfer, you must:")
    print("1. Unlock both domains at Squarespace")
    print("2. Disable WHOIS privacy (if enabled)")
    print("3. Get authorization codes (EPP codes) for both domains")
    print("4. Verify contact email addresses are current")
    print("\nTo get authorization codes:")
    print("  - Log into Squarespace Domains")
    print("  - For each domain, go to domain settings")
    print("  - Look for 'Transfer' or 'Authorization Code' section")
    print("  - Request/copy the authorization code")

    print("\n" + "=" * 80)
    print("STEP 2: CHECK TRANSFER PRICING")
    print("=" * 80)

    api_token = get_dnsimple_token()
    if not api_token:
        print("\nError: Could not retrieve DNSimple API token.")
        sys.exit(1)

    try:
        account_id = get_account_id(api_token)
        print(f"✓ Account ID: {account_id}\n")
    except Exception as e:
        print(f"Error fetching account: {e}")
        sys.exit(1)

    # Check pricing for each domain
    pricing_info = {}
    for domain in DOMAINS_TO_TRANSFER:
        tld = domain.split(".")[-1]
        print(f"Checking transfer pricing for {domain} ({tld})...")
        price = get_transfer_pricing(api_token, account_id, tld)
        if price:
            amount = price.get("price", "N/A")
            currency = price.get("currency", "USD")
            pricing_info[domain] = {"amount": amount, "currency": currency}
            print(f"  Transfer cost: {amount} {currency}")
        else:
            print(
                "  Transfer pricing not available (may need to check DNSimple website)"
            )
            pricing_info[domain] = None
        print()

    print("=" * 80)
    print("STEP 3: INITIATE TRANSFER")
    print("=" * 80)
    print("\nTo initiate the transfer via API, you need:")
    print("  - Authorization codes from Squarespace")
    print("  - Domains unlocked at Squarespace")
    print("\nDo you have the authorization codes ready? (y/n)")

    # For automated execution, we'll provide instructions
    print("\n" + "-" * 80)
    print("MANUAL TRANSFER INSTRUCTIONS:")
    print("-" * 80)
    print("\nOption A: Use DNSimple Web Interface (Recommended)")
    print("1. Log into https://dnsimple.com")
    print("2. Go to 'Domains' → 'Transfer'")
    print("3. Enter domain name and authorization code")
    print("4. Complete checkout")
    print("5. Approve transfer emails from Squarespace")

    print("\nOption B: Use API (requires authorization codes)")
    print("Run this script with authorization codes:")
    print(
        "  python execution/scripts/transfer_domains_to_dnsimple.py --auth-code markh.io:YOUR_CODE --auth-code markmhendrickson.com:YOUR_CODE"
    )

    # Check if auth codes provided via command line
    auth_codes = {}
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg.startswith("--auth-code"):
                if ":" in arg:
                    domain, code = arg.split(":", 1)
                    domain = domain.replace("--auth-code", "").strip()
                    auth_codes[domain] = code

    if auth_codes:
        print("\n" + "=" * 80)
        print("INITIATING TRANSFERS WITH PROVIDED CODES")
        print("=" * 80)

        registrant_id = get_registrant_id(api_token, account_id)

        for domain in DOMAINS_TO_TRANSFER:
            if domain not in auth_codes:
                print(f"\n⚠ Skipping {domain}: No authorization code provided")
                continue

            print(f"\nInitiating transfer for {domain}...")
            success, result = initiate_transfer(
                api_token, account_id, domain, auth_codes[domain], registrant_id
            )

            if success:
                print("✓ Transfer initiated successfully")
                print(f"  Transfer ID: {result.get('id')}")
                print(f"  Status: {result.get('state')}")
                print("  Next: Check email for transfer approval")
            else:
                print(f"✗ Transfer failed: {result}")
    else:
        print("\n" + "=" * 80)
        print("NEXT STEPS")
        print("=" * 80)
        print("\n1. Complete Step 1 (prepare domains at Squarespace)")
        print("2. Get authorization codes from Squarespace")
        print("3. Either:")
        print("   a) Use DNSimple web interface to transfer")
        print("   b) Run this script again with --auth-code flags")
        print("\nAfter transfer completes, DNS records can be configured via API.")


if __name__ == "__main__":
    main()
