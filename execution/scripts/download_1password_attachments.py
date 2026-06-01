#!/usr/bin/env python3
"""
Download attachments from a 1Password item.

Usage:
    python execution/scripts/download_1password_attachments.py "mark hendrickson" --output ./downloads
    python execution/scripts/download_1password_attachments.py <item-id> --output ./downloads
    python execution/scripts/download_1password_attachments.py "mark hendrickson" --vault Private --output ./downloads

Security: This script uses 1Password CLI but does not print secret values to stdout.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def op_list_items(vault: str | None = None) -> list[dict]:
    """List 1Password items (metadata only, no secrets)."""
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


def find_item_by_name(items: list[dict], search_name: str) -> dict | None:
    """Find item by name (case-insensitive partial match)."""
    search_lower = search_name.lower()
    for item in items:
        title = item.get("title", "").lower()
        if search_lower in title or title in search_lower:
            return item
    return None


def op_get_item_attachments(item_id: str, vault: str | None = None) -> list[dict]:
    """
    Get list of attachments for an item (metadata only).
    Returns list of attachment info without downloading.
    """
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
        item_data = json.loads(result.stdout)

        # Extract attachment metadata
        attachments = []

        # Check fields for file type
        for field in item_data.get("fields", []):
            if field.get("type") == "file":
                attachments.append(
                    {
                        "id": field.get("id"),
                        "label": field.get("label", ""),
                        "filename": field.get("label", "attachment"),
                    }
                )

        # Also check for files array (some 1Password items store files differently)
        if "files" in item_data:
            for file_info in item_data.get("files", []):
                attachments.append(
                    {
                        "id": file_info.get("id"),
                        "label": file_info.get("name", ""),
                        "filename": file_info.get("name", "attachment"),
                    }
                )

        return attachments
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Could not get item: {e.stderr}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return []


def op_download_attachments(
    item_id: str, vault: str | None = None, output_dir: Path = None
) -> list[Path]:
    """
    Download all attachments from a 1Password item.
    Returns list of downloaded file paths.
    """
    if output_dir is None:
        output_dir = Path.cwd()
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # First, get item metadata to find attachments
        item_cmd = ["op", "item", "get", item_id, "--format", "json"]
        if vault:
            item_cmd.extend(["--vault", vault])

        result = subprocess.run(
            item_cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        item_data = json.loads(result.stdout)

        downloaded = []
        vault_name = vault or item_data.get("vault", {}).get("name", "Private")

        # Download files from fields
        for field in item_data.get("fields", []):
            if field.get("type") == "file":
                field_id = field.get("id")
                filename = field.get("label", "attachment")

                if not field_id:
                    continue

                # Build op:// reference for the file field
                op_ref = f"op://{vault_name}/{item_id}/{field_id}"
                output_path = output_dir / filename

                try:
                    # Use op read to download the file
                    read_cmd = ["op", "read", op_ref, "--out-file", str(output_path)]
                    subprocess.run(
                        read_cmd,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    if output_path.exists() and output_path.stat().st_size > 0:
                        downloaded.append(output_path)
                        print(f"✓ Downloaded: {filename} -> {output_path}")
                    else:
                        print(
                            f"⚠ Downloaded file is empty: {filename}", file=sys.stderr
                        )
                except subprocess.CalledProcessError as e:
                    print(
                        f"⚠ Could not download {filename} via op read: {e.stderr}",
                        file=sys.stderr,
                    )

        # Also check for files array (some 1Password items store files differently)
        if "files" in item_data:
            for file_info in item_data.get("files", []):
                file_id = file_info.get("id")
                filename = file_info.get("name", "attachment")

                if not file_id:
                    continue

                op_ref = f"op://{vault_name}/{item_id}/{file_id}"
                output_path = output_dir / filename

                try:
                    read_cmd = ["op", "read", op_ref, "--out-file", str(output_path)]
                    subprocess.run(
                        read_cmd,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    if output_path.exists() and output_path.stat().st_size > 0:
                        downloaded.append(output_path)
                        print(f"✓ Downloaded: {filename} -> {output_path}")
                except subprocess.CalledProcessError as e:
                    print(
                        f"⚠ Could not download {filename}: {e.stderr}", file=sys.stderr
                    )

        return downloaded
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Could not get item: {e.stderr}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return []


def main():
    parser = argparse.ArgumentParser(
        description="Download attachments from a 1Password item"
    )
    parser.add_argument("item", help="Item name (partial match) or item ID")
    parser.add_argument("--vault", help="Vault name (optional)")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path.cwd() / "downloads",
        help="Output directory for attachments (default: ./downloads)",
    )
    parser.add_argument(
        "--list-only", action="store_true", help="Only list attachments, don't download"
    )

    args = parser.parse_args()

    # Check if item is an ID (UUID format) or a name
    item_id = args.item
    if len(args.item) < 20:  # Likely a name, not an ID
        print(f"Searching for item: {args.item}")
        items = op_list_items(args.vault)
        found_item = find_item_by_name(items, args.item)

        if not found_item:
            print(f"ERROR: Item '{args.item}' not found", file=sys.stderr)
            print("\nAvailable items:", file=sys.stderr)
            for item in items[:10]:
                print(f"  - {item.get('title', 'Unknown')}", file=sys.stderr)
            if len(items) > 10:
                print(f"  ... and {len(items) - 10} more", file=sys.stderr)
            sys.exit(1)

        item_id = found_item["id"]
        print(f"Found item: {found_item.get('title', 'Unknown')} (ID: {item_id})")

    # List or download attachments
    if args.list_only:
        attachments = op_get_item_attachments(item_id, args.vault)
        if attachments:
            print(f"\nFound {len(attachments)} attachment(s):")
            for att in attachments:
                print(f"  - {att['label']} (ID: {att['id']})")
        else:
            print("No attachments found in this item")
    else:
        print(f"\nDownloading attachments to: {args.output}")
        downloaded = op_download_attachments(item_id, args.vault, args.output)

        if downloaded:
            print(f"\n✓ Downloaded {len(downloaded)} attachment(s)")
            print(f"Location: {args.output}")
        else:
            print("\n⚠ No attachments found or downloaded")


if __name__ == "__main__":
    main()
