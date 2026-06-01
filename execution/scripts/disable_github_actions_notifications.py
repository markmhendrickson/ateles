#!/usr/bin/env python3
"""
Disable GitHub Actions notifications for specified repositories.

Usage:
    python execution/scripts/disable_github_actions_notifications.py markmhendrickson/hendricksonserrano
    python execution/scripts/disable_github_actions_notifications.py owner/repo1 owner/repo2
"""

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

try:
    import requests
except ImportError:
    print("Error: requests library not installed. Run: pip install requests")
    sys.exit(1)

GITHUB_API_BASE = "https://api.github.com"


def get_github_token_from_env():
    """Get GitHub token from environment variable."""
    return os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")


def get_github_token_from_1password():
    """Get GitHub token from 1Password."""
    try:
        # Try common 1Password field names for GitHub tokens
        field_names = [
            "access token (classic)",
            "access_token (classic)",
            "token",
            "access_token",
            "api_token",
            "github_token",
            "personal_access_token",
        ]

        # Try with item IDs if multiple items exist (from the user's 1Password)
        item_ids = [
            "ttqomy5uxzez5nq3eo72mcntja",
            "lojdvg7c7fpdwrzvpi7aimgo7q",
        ]  # GitHub and Github

        for item_id in item_ids:
            for field in field_names:
                try:
                    result = subprocess.run(
                        [
                            "op",
                            "item",
                            "get",
                            item_id,
                            "--fields",
                            f"label={field}",
                            "--reveal",
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    token = result.stdout.strip()
                    # Skip if result looks like an error message
                    if token and not token.startswith("[") and len(token) > 10:
                        return token
                except (subprocess.CalledProcessError, FileNotFoundError):
                    continue

        # Try with "GitHub" as item name (may fail if multiple items)
        for field in field_names:
            try:
                result = subprocess.run(
                    [
                        "op",
                        "item",
                        "get",
                        "GitHub",
                        "--fields",
                        f"label={field}",
                        "--reveal",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                token = result.stdout.strip()
                if token and not token.startswith("[") and len(token) > 10:
                    return token
            except (subprocess.CalledProcessError, FileNotFoundError):
                continue

        # Try searching for GitHub item by URL
        for field in field_names:
            try:
                result = subprocess.run(
                    [
                        "op",
                        "item",
                        "get",
                        "github.com",
                        "--fields",
                        f"label={field}",
                        "--reveal",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                token = result.stdout.strip()
                if token and not token.startswith("[") and len(token) > 10:
                    return token
            except (subprocess.CalledProcessError, FileNotFoundError):
                continue

        return None
    except Exception:
        return None


def get_github_token():
    """Get GitHub token from environment or 1Password."""
    token = get_github_token_from_env()
    if token:
        print("✓ GitHub token loaded from environment")
        return token

    print("Token not found in environment, fetching from 1Password...")
    token = get_github_token_from_1password()

    if token:
        print("✓ GitHub token loaded from 1Password")
    else:
        print("\nTo set up:")
        print("1. Create a 1Password item titled 'GitHub' or with URL 'github.com'")
        print(
            "2. Add a field labeled 'token', 'access_token', or 'api_token' with your GitHub personal access token"
        )
        print("3. Or set environment variable: GITHUB_TOKEN or GH_TOKEN")
        print("4. Get your token from: https://github.com/settings/tokens")
        print("   Required scopes: 'notifications' and 'repo' (for private repos)")

    return token


def unsubscribe_from_repository(token, owner, repo):
    """Unsubscribe from repository notifications."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }

    # Unsubscribe from repository (stops all notifications including Actions)
    data = {
        "subscribed": False,
        "ignored": False,  # Don't ignore, just unsubscribe
    }

    response = requests.put(
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/subscription",
        headers=headers,
        json=data,
    )

    if response.status_code == 200:
        return True, None
    elif response.status_code == 404:
        return False, f"Repository {owner}/{repo} not found or no access"
    else:
        return False, f"API Error {response.status_code}: {response.text}"


def get_repository_subscription(token, owner, repo):
    """Get current subscription status for repository."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    response = requests.get(
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/subscription",
        headers=headers,
    )

    if response.status_code == 200:
        return response.json()
    elif response.status_code == 404:
        return None
    else:
        return None


def main():
    """Main function."""
    if len(sys.argv) < 2:
        print(
            "Usage: python execution/scripts/disable_github_actions_notifications.py <owner/repo1> [owner/repo2] ..."
        )
        print(
            "Example: python execution/scripts/disable_github_actions_notifications.py markmhendrickson/hendricksonserrano"
        )
        sys.exit(1)

    repos_to_disable = sys.argv[1:]

    print("Fetching GitHub token...")
    token = get_github_token()

    if not token:
        print("Error: Could not retrieve GitHub token.")
        sys.exit(1)

    print("\n" + "=" * 80)
    print("DISABLING GITHUB ACTIONS NOTIFICATIONS")
    print("=" * 80)
    print()

    results = []
    for repo_full_name in repos_to_disable:
        if "/" not in repo_full_name:
            print(
                f"✗ Invalid repository format: {repo_full_name} (expected owner/repo)"
            )
            results.append(
                {"repo": repo_full_name, "status": "invalid", "error": "Invalid format"}
            )
            continue

        owner, repo = repo_full_name.split("/", 1)
        print(f"Processing: {owner}/{repo}")

        # Check current subscription status
        current_sub = get_repository_subscription(token, owner, repo)
        if current_sub:
            print(
                f"  Current subscription: subscribed={current_sub.get('subscribed', False)}"
            )

        # Unsubscribe from repository
        success, error = unsubscribe_from_repository(token, owner, repo)

        if success:
            print(f"  ✓ Unsubscribed from {owner}/{repo} (notifications disabled)")
            results.append(
                {"repo": repo_full_name, "status": "unsubscribed", "error": None}
            )
        else:
            print(f"  ✗ Failed to unsubscribe from {owner}/{repo}: {error}")
            results.append({"repo": repo_full_name, "status": "failed", "error": error})

        print()

    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)

    success_count = sum(1 for r in results if r["status"] == "unsubscribed")
    failed_count = sum(1 for r in results if r["status"] == "failed")
    invalid_count = sum(1 for r in results if r["status"] == "invalid")

    print(f"Unsubscribed: {success_count}")
    print(f"Failed: {failed_count}")
    print(f"Invalid: {invalid_count}")

    if failed_count > 0 or invalid_count > 0:
        print("\nFailed repositories:")
        for r in results:
            if r["status"] != "unsubscribed":
                print(f"  - {r['repo']}: {r.get('error', 'Unknown error')}")

    if success_count == len(results):
        print("\n✓ All repositories unsubscribed successfully")
    elif success_count > 0:
        print(
            f"\n⚠ {success_count} repository(s) unsubscribed, {failed_count + invalid_count} failed"
        )
    else:
        print("\n✗ No repositories were unsubscribed")
        sys.exit(1)


if __name__ == "__main__":
    main()
