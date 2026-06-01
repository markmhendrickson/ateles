#!/usr/bin/env python3
"""
Credential management utility using 1Password CLI.

This module provides secure credential retrieval from 1Password without storing
secrets in the repository or requiring manual .env file management.

Requirements:
    - 1Password CLI (`op`) installed and authenticated
    - 1Password items stored with predictable titles or tags

Usage:
    from scripts.credentials import get_credential

    email, password = get_credential("Minted.com")
    # or
    api_key = get_credential("Service Name", field="api_key")
"""

import json
import subprocess
import sys


def _run_op_command(args: list[str]) -> dict:
    """Run 1Password CLI command and return JSON result."""
    try:
        cmd = ["op", *args]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"1Password CLI error: {e.stderr}\n"
            "Ensure 'op' is installed and authenticated: eval $(op signin)"
        ) from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse 1Password CLI output: {e}") from e
    except FileNotFoundError:
        raise RuntimeError(
            "1Password CLI not found. Install from: https://developer.1password.com/docs/cli"
        )


def find_item_by_title(title: str, vault: str | None = None) -> str | None:
    """
    Find 1Password item by title (fuzzy match).

    Args:
        title: Item title to search for
        vault: Optional vault name to limit search

    Returns:
        Item UUID if found, None otherwise
    """
    try:
        # List all items (or items in specific vault)
        cmd = ["item", "list", "--format=json"]
        if vault:
            cmd.extend(["--vault", vault])

        items = _run_op_command(cmd)

        # Filter items by title (case-insensitive partial match)
        title_lower = title.lower()
        for item in items:
            item_title = item.get("title", "").lower()
            if title_lower in item_title or item_title in title_lower:
                return item.get("id")
    except RuntimeError:
        pass

    return None


def get_item_fields(item_id: str) -> dict[str, str]:
    """
    Get all fields from a 1Password item.

    Args:
        item_id: 1Password item UUID

    Returns:
        Dictionary mapping field labels to values
    """
    item = _run_op_command(["item", "get", item_id, "--format=json"])

    fields = {}
    for field in item.get("fields", []):
        label = field.get("label", "").lower()
        value = field.get("value", "")
        if value:
            fields[label] = value

    # Also check for standard field IDs
    for field in item.get("fields", []):
        field_id = field.get("id", "")
        value = field.get("value", "")
        if value:
            # Map common field IDs to labels
            if field_id == "username":
                fields["username"] = value
            elif field_id == "password":
                fields["password"] = value
            elif field_id == "email":
                fields["email"] = value

    return fields


def get_credential(
    service_name: str,
    vault: str | None = None,
    field: str | None = None,
    item_id: str | None = None,
) -> str | tuple[str, str]:
    """
    Retrieve credential from 1Password.

    Args:
        service_name: Name of the service (used to find item by title)
        vault: Optional vault name to limit search
        field: Specific field to retrieve (e.g., "api_key", "password")
               If None, returns (username/email, password) tuple
        item_id: Optional direct item UUID (bypasses search)

    Returns:
        If field specified: single credential value
        If field is None: tuple of (username/email, password)

    Examples:
        # Get username and password
        email, password = get_credential("Minted.com")

        # Get specific field
        api_key = get_credential("Service Name", field="api_key")

        # Use specific vault
        token = get_credential("API Service", vault="Work", field="token")
    """
    # Use provided item_id or search for item
    if not item_id:
        item_id = find_item_by_title(service_name, vault)
        if not item_id:
            raise ValueError(
                f"Could not find 1Password item matching '{service_name}'. "
                f"Ensure item exists and title matches."
            )

    fields = get_item_fields(item_id)

    if field:
        # Return specific field
        field_lower = field.lower()
        if field_lower in fields:
            return fields[field_lower]

        # Try common variations
        variations = {
            "api_key": ["api key", "apikey", "api_key"],
            "token": ["token", "access token", "access_token"],
            "password": ["password", "pass"],
            "username": ["username", "user", "login"],
            "email": ["email", "e-mail"],
        }

        for var_key, var_list in variations.items():
            if field_lower == var_key:
                for var in var_list:
                    if var in fields:
                        return fields[var]

        raise ValueError(
            f"Field '{field}' not found in 1Password item. "
            f"Available fields: {list(fields.keys())}"
        )
    else:
        # Return username/email and password
        username = fields.get("username") or fields.get("email") or fields.get("e-mail")
        password = fields.get("password") or fields.get("pass")

        if not username:
            raise ValueError(
                "Could not find username/email field in 1Password item. "
                f"Available fields: {list(fields.keys())}"
            )
        if not password:
            raise ValueError(
                "Could not find password field in 1Password item. "
                f"Available fields: {list(fields.keys())}"
            )

        return username, password


def get_credential_by_domain(
    domain: str,
    vault: str | None = None,
    field: str | None = None,
) -> str | tuple[str, str]:
    """
    Retrieve credential by domain name (searches item URLs).

    Args:
        domain: Domain name (e.g., "minted.com", "github.com")
        vault: Optional vault name to limit search
        field: Specific field to retrieve, or None for (username, password)

    Returns:
        If field specified: single credential value
        If field is None: tuple of (username/email, password)
    """
    try:
        # List all items (or items in specific vault)
        cmd = ["item", "list", "--format=json"]
        if vault:
            cmd.extend(["--vault", vault])

        items = _run_op_command(cmd)

        # Search for items with matching domain in URLs
        domain_lower = domain.lower().replace("www.", "")
        matching_items = []

        for item in items:
            # Check item URLs
            urls = item.get("urls", [])
            for url_obj in urls:
                url = (
                    url_obj.get("href", "").lower()
                    if isinstance(url_obj, dict)
                    else str(url_obj).lower()
                )
                if domain_lower in url:
                    matching_items.append(item)
                    break

        if not matching_items:
            # Fallback: check title for domain
            for item in items:
                title = item.get("title", "").lower()
                if domain_lower in title:
                    matching_items.append(item)
                    break

        if not matching_items:
            raise ValueError(
                f"Could not find 1Password item with domain '{domain}'. "
                "Ensure item has URL field set or title contains domain."
            )

        item_id = matching_items[0].get("id")
        return get_credential(service_name="", item_id=item_id, field=field)
    except RuntimeError as e:
        raise ValueError(f"Error searching 1Password: {e}") from e


if __name__ == "__main__":
    # CLI interface for testing
    if len(sys.argv) < 2:
        print("Usage: python credentials.py <service_name> [field] [vault]")
        print("Example: python credentials.py 'Minted.com'")
        print("Example: python credentials.py 'Minted.com' password")
        print("Example: python credentials.py 'API Service' api_key Work")
        sys.exit(1)

    service_name = sys.argv[1]
    field = sys.argv[2] if len(sys.argv) > 2 else None
    vault = sys.argv[3] if len(sys.argv) > 3 else None

    try:
        result = get_credential(service_name, vault=vault, field=field)
        if isinstance(result, tuple):
            print(f"Username/Email: {result[0]}")
            print(f"Password: {result[1]}")
        else:
            print(result)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
