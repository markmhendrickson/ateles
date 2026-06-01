"""
Inspect 1Password vault structure to help map environment variables.

This script lists 1Password items and their field structures WITHOUT revealing
any secret values. Use this to identify which items/fields map to neotoma env vars.

Safety:
- Only shows item names, IDs, and field labels/IDs
- NEVER prints secret values
- Safe to run - only displays structure/metadata

Usage:
    python execution/scripts/inspect_1password_for_neotoma.py
    python execution/scripts/inspect_1password_for_neotoma.py --vault Private
    python execution/scripts/inspect_1password_for_neotoma.py --search "neotoma"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def op_list_items(vault: str | None = None) -> list[dict]:
    """List 1Password items (structure only, no secrets)."""
    try:
        cmd = ["op", "item", "list", "--format", "json"]
        if vault:
            cmd.extend(["--vault", vault])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: 1Password CLI error: {e.stderr}", file=sys.stderr)
        print("Make sure you're signed in: op signin", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse 1Password output: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("ERROR: 1Password CLI not found", file=sys.stderr)
        print("Install with: brew install --cask 1password-cli", file=sys.stderr)
        sys.exit(1)


def op_get_item_structure(item_id: str, vault: str | None = None) -> dict:
    """Get item structure (fields, labels, IDs) without secret values."""
    try:
        cmd = ["op", "item", "get", item_id, "--format", "json"]
        if vault:
            cmd.extend(["--vault", vault])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)

        # Extract only structure - field labels and IDs, not values
        structure = {
            "id": data.get("id"),
            "title": data.get("title"),
            "vault": (
                data.get("vault", {}).get("name")
                if isinstance(data.get("vault"), dict)
                else None
            ),
            "fields": [],
        }

        for field in data.get("fields", []):
            # Skip fields that contain secret values - only show structure
            field_info = {
                "label": field.get("label", ""),
                "id": field.get("id", ""),
                "type": field.get("type", ""),
                "purpose": field.get("purpose", ""),
                "reference": field.get("reference", ""),  # Include the op:// reference
            }
            structure["fields"].append(field_info)

        return structure
    except subprocess.CalledProcessError as e:
        print(
            f"WARNING: Could not get structure for {item_id}: {e.stderr}",
            file=sys.stderr,
        )
        return None
    except Exception as e:
        print(f"WARNING: Error processing {item_id}: {e}", file=sys.stderr)
        return None


def search_items(items: list[dict], search_term: str) -> list[dict]:
    """Filter items by search term (case-insensitive)."""
    if not search_term:
        return items

    search_lower = search_term.lower()
    return [
        item
        for item in items
        if search_lower in item.get("title", "").lower()
        or search_lower in item.get("id", "").lower()
    ]


def print_item_structure(item: dict, vault: str | None = None):
    """Print item structure with field information."""
    print(f"\n{'=' * 60}")
    print(f"Item: {item.get('title', 'Unknown')}")
    print(f"ID: {item.get('id', 'Unknown')}")

    structure = op_get_item_structure(item["id"], vault)
    if not structure:
        print("  (Could not retrieve structure)")
        return

    if structure.get("vault"):
        print(f"Vault: {structure['vault']}")

    fields = structure.get("fields", [])
    if fields:
        print(f"\nFields ({len(fields)}):")
        for field in fields:
            label = field.get("label", "")
            field_id = field.get("id", "")
            field_type = field.get("type", "")
            purpose = field.get("purpose", "")
            op_ref = field.get("reference", "")

            # Use provided reference or build one
            if not op_ref:
                vault_name = structure.get("vault") or vault or "Private"
                item_id = structure.get("id", item.get("id", ""))
                field_ref = field_id or label
                op_ref = f"op://{vault_name}/{item_id}/{field_ref}"

            print(f"  - Label: {label}")
            print(f"    ID: {field_id}")
            print(f"    Type: {field_type}")
            if purpose:
                print(f"    Purpose: {purpose}")
            print(f"    Reference: {op_ref}")
            print()
    else:
        print("  (No fields found)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect 1Password vault structure for neotoma env var mapping"
    )
    parser.add_argument(
        "--vault",
        help="Specific vault to search (default: all vaults)",
    )
    parser.add_argument(
        "--search",
        help="Search term to filter items (case-insensitive)",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only list item names/IDs, don't show field structures",
    )
    args = parser.parse_args()

    print("Inspecting 1Password vault structure...")
    print("(This only shows structure - no secret values are displayed)")
    print()

    items = op_list_items(args.vault)

    if args.search:
        items = search_items(items, args.search)
        print(f"Found {len(items)} items matching '{args.search}'")
    else:
        print(f"Found {len(items)} items")

    if not items:
        print("No items found.")
        return 0

    if args.list_only:
        print("\nItems:")
        for item in items:
            print(
                f"  - {item.get('title', 'Unknown')} (ID: {item.get('id', 'Unknown')})"
            )
    else:
        # Show structure for first 10 items (or all if fewer)
        items_to_show = items[:10]
        if len(items) > 10:
            print(f"\nShowing structure for first 10 items (of {len(items)} total):")
            print("(Use --search to filter or --list-only to see all names)")
        else:
            print("\nShowing structure for all items:")

        for item in items_to_show:
            print_item_structure(item, args.vault)

        if len(items) > 10:
            print(
                f"\n... and {len(items) - 10} more items (use --list-only to see all)"
            )

    print("\n" + "=" * 60)
    print("To use these in neotoma_sync_env_from_1password.py, add to ENV_TO_OP_REF:")
    print("  ENV_TO_OP_REF = {")
    print('    "DEV_SUPABASE_URL": "op://VaultName/ItemID/FieldID",')
    print("    # ... etc")
    print("  }")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
