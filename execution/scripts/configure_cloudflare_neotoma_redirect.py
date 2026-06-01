#!/usr/bin/env python3
"""
Configure neotoma.io temporary redirect to GitHub repo via Cloudflare API.

Requires: neotoma.io zone in Cloudflare and CLOUDFLARE_API_TOKEN (or 1Password
Cloudflare item with API token). Token needs Zone > Single Redirect > Edit.

Usage:
  python configure_cloudflare_neotoma_redirect.py          # add or update redirect
  python configure_cloudflare_neotoma_redirect.py --remove # remove redirect rule
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    load_dotenv(env_file)

REDIRECT_PHASE = "http_request_dynamic_redirect"
REDIRECT_REF = "neotoma_io_to_github"
TARGET_URL = "https://github.com/markmhendrickson/neotoma"
ZONE_NAME = "neotoma.io"


def get_cloudflare_token():
    """Get Cloudflare API token from environment or 1Password."""
    token = (os.getenv("CLOUDFLARE_API_TOKEN") or "").strip()
    if token:
        return token
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
            for f in data.get("fields", []):
                label = (f.get("label") or "").lower()
                if "api token" in label or "token" == label:
                    return f.get("value", "").strip() or None
            # Fallback: first password or concealed
            for f in data.get("fields", []):
                if f.get("type") in ("CONCEALED", "PASSWORD") and f.get("value"):
                    return f.get("value", "").strip()
    except Exception:
        pass
    return None


def get_zone_id(api_token):
    """Return zone ID for ZONE_NAME."""
    resp = requests.get(
        "https://api.cloudflare.com/client/v4/zones",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        params={"name": ZONE_NAME},
        timeout=30,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code != 200:
        errs = data.get("errors", [])
        msg = errs[0].get("message", resp.text) if errs else resp.text
        print(f"Cloudflare API error {resp.status_code}: {msg}")
        if resp.status_code == 403:
            print(
                "Token may need: Zone > Zone > Read, and Zone > Single Redirect > Edit."
            )
        return None
    zones = data.get("result", [])
    if not zones:
        print(f"Zone {ZONE_NAME} not found in Cloudflare.")
        return None
    return zones[0]["id"]


def rule_to_serializable(rule):
    """Return only fields needed for API update (no id, version, last_updated)."""
    out = {
        "expression": rule.get("expression"),
        "action": rule.get("action"),
        "description": rule.get("description"),
    }
    if rule.get("ref"):
        out["ref"] = rule["ref"]
    if rule.get("action_parameters"):
        out["action_parameters"] = rule["action_parameters"]
    return out


def get_redirect_rule():
    """Return the redirect rule definition."""
    return {
        "ref": REDIRECT_REF,
        "expression": '(http.host eq "neotoma.io" or http.host eq "www.neotoma.io")',
        "description": "Temporary redirect neotoma.io to GitHub repo",
        "action": "redirect",
        "action_parameters": {
            "from_value": {
                "target_url": {"value": TARGET_URL},
                "status_code": 302,
                "preserve_query_string": False,
            }
        },
    }


def ensure_apex_dns_records(api_token, zone_id):
    """Ensure A records exist for apex and www so the domain resolves (proxied for redirect)."""
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    resp = requests.get(
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
        headers=headers,
        params={"type": "A", "per_page": 10},
        timeout=30,
    )
    if resp.status_code != 200:
        print("Could not list DNS records (add Zone > DNS > Read to token to fix).")
        return
    data = resp.json()
    existing = {r["name"]: r for r in data.get("result", [])}
    for name, content in [("neotoma.io", "192.0.2.1"), ("www.neotoma.io", "192.0.2.1")]:
        if name in existing:
            continue
        cre = requests.post(
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
            headers=headers,
            json={
                "type": "A",
                "name": name,
                "content": content,
                "ttl": 1,
                "proxied": True,
            },
            timeout=30,
        )
        if cre.status_code in (200, 201):
            print(f"Created A record for {name}.")
        else:
            err = (cre.json() or {}).get("errors", [{}])[0].get("message", cre.text)
            print(
                f"Could not create A record for {name}: {err}. Add Zone > DNS > Edit to token."
            )


def get_entrypoint(api_token, zone_id):
    """GET phase entrypoint ruleset. Returns (ruleset_result, None) or (None, error)."""
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/rulesets/phases/{REDIRECT_PHASE}/entrypoint"
    resp = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if resp.status_code == 404:
        return None, None  # no ruleset yet
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        return None, data.get("errors", [{"message": "Unknown error"}])
    return data.get("result"), None


def put_entrypoint(api_token, zone_id, rules, description=None):
    """Create or update phase entrypoint with given rules."""
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/rulesets/phases/{REDIRECT_PHASE}/entrypoint"
    body = {"rules": rules}
    if description is not None:
        body["description"] = description
    resp = requests.put(
        url,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(data.get("errors", [{"message": "Unknown error"}]))


def main():
    parser = argparse.ArgumentParser(
        description="Configure neotoma.io redirect via Cloudflare API"
    )
    parser.add_argument(
        "--remove", action="store_true", help="Remove the redirect rule"
    )
    args = parser.parse_args()

    api_token = get_cloudflare_token()
    if not api_token:
        print("Error: Set CLOUDFLARE_API_TOKEN or configure Cloudflare in 1Password.")
        sys.exit(1)
    print(f"Using CLOUDFLARE_API_TOKEN (length {len(api_token)})")

    zone_id = get_zone_id(api_token)
    if not zone_id:
        sys.exit(1)

    if not args.remove:
        ensure_apex_dns_records(api_token, zone_id)

    result, err = get_entrypoint(api_token, zone_id)
    if err:
        print("Error fetching ruleset:", err)
        sys.exit(1)

    existing_rules = (result.get("rules") or []) if result else []
    if args.remove:
        # Keep all rules that are not our redirect
        rules = [
            rule_to_serializable(r)
            for r in existing_rules
            if r.get("ref") != REDIRECT_REF
        ]
        if len(rules) == len(existing_rules):
            print("Redirect rule not found; nothing to remove.")
            return
        put_entrypoint(
            api_token, zone_id, rules, result.get("description") if result else None
        )
        print("Redirect rule removed.")
        return

    # Add or keep redirect rule
    has_redirect = any(r.get("ref") == REDIRECT_REF for r in existing_rules)
    if has_redirect:
        # Keep existing rules (with redirect already in place)
        rules = [rule_to_serializable(r) for r in existing_rules]
    else:
        rules = [rule_to_serializable(r) for r in existing_rules] + [
            get_redirect_rule()
        ]

    put_entrypoint(
        api_token, zone_id, rules, result.get("description") if result else None
    )
    print("Redirect configured: neotoma.io and www.neotoma.io ->", TARGET_URL, "(302)")


if __name__ == "__main__":
    main()
