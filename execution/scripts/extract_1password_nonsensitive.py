#!/usr/bin/env python
"""
Extract non-sensitive metadata from a 1Password 1PUX export.

This script is intentionally conservative:
 - It NEVER emits any field values from items (no passwords, usernames, notes, etc.).
 - It only keeps high-level metadata (titles, categories, tags, URLs, vault names, timestamps,
   and field/section labels without their values).

Usage:
    python execution/scripts/extract_1password_nonsensitive.py path/to/export.1pux \
        --output sanitized_1password.json

The output file contains JSON with the following structure:

{
  "accounts": [...],   # as in the original export, but with sensitive subfields removed if present
  "vaults":   [...],   # vault metadata
  "items": [
    {
      "id": "...",
      "title": "...",
      "category": "...",
      "vault": "Vault Name",
      "tags": [...],
      "urls": ["https://example.com", ...],
      "createdAt": "...",
      "updatedAt": "...",
      "archived": false,
      "sections": [
        {
          "id": "...",
          "label": "Section label"
        }
      ],
      "fields": [
        {
          "id": "...",
          "label": "Field label",
          "section": "section-id-or-none",
          "type": "FIELD_TYPE"  # from 1Password export
          # NOTE: no 'value' key is ever included
        }
      ]
    },
    ...
  ]
}

This is designed so you can safely inspect and integrate 1Password structure and metadata
without ever storing secrets from the vault.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any


def find_export_data_member(zf: zipfile.ZipFile) -> str | None:
    """
    Locate the export.data file inside the 1PUX archive.

    Different 1Password versions may nest this file under different directories;
    we just search for a member ending with 'export.data'.
    """
    for name in zf.namelist():
        if name.endswith("export.data"):
            return name
    return None


def load_export_data(p: Path) -> dict[str, Any]:
    """Open a .1pux (ZIP) file and return the parsed JSON from export.data."""
    if not p.is_file():
        raise FileNotFoundError(f"Input file not found: {p}")

    with zipfile.ZipFile(p, "r") as zf:
        member = find_export_data_member(zf)
        if member is None:
            raise RuntimeError("Could not find 'export.data' inside 1PUX archive.")
        raw = zf.read(member)

    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"Failed to parse JSON from export.data: {exc}") from exc


def sanitize_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Pass through account-level metadata, dropping any obviously sensitive subkeys if present.
    In practice 1Password account objects are mostly metadata, but we stay conservative.
    """
    sanitized: list[dict[str, Any]] = []
    sensitive_keys = {
        "secret",
        "secretKey",
        "secret_key",
        "token",
        "apiKey",
        "api_key",
    }

    for acc in accounts:
        clean = {k: v for k, v in acc.items() if k not in sensitive_keys}
        sanitized.append(clean)

    return sanitized


def sanitize_item(item: dict[str, Any]) -> dict[str, Any]:
    """
    Build a non-sensitive projection of a single item.

    We keep:
      - Structural metadata: id, title, category, tags, URLs, vault name/id, timestamps, archived flag
      - Section metadata (ids + labels)
      - Field metadata (ids, labels, types, section ids)

    We explicitly drop all field *values* and any obvious secret-bearing keys.
    """
    vault_name: str | None = None
    vault = item.get("vault")
    if isinstance(vault, dict):
        vault_name = vault.get("name") or vault.get("id")
    elif isinstance(vault, str):
        vault_name = vault

    urls: list[str] = []
    for u in item.get("urls", []) or []:
        if isinstance(u, dict):
            href = u.get("href")
            if isinstance(href, str):
                urls.append(href)

    sections_out: list[dict[str, Any]] = []
    for sec in item.get("sections", []) or []:
        if not isinstance(sec, dict):
            continue
        sections_out.append(
            {
                "id": sec.get("id"),
                "label": sec.get("label"),
            }
        )

    fields_out: list[dict[str, Any]] = []
    for fld in item.get("fields", []) or []:
        if not isinstance(fld, dict):
            continue

        # We keep only non-value metadata about the field.
        fields_out.append(
            {
                "id": fld.get("id"),
                "label": fld.get("label"),
                "section": fld.get("section"),
                "type": fld.get("type") or fld.get("purpose"),
                # NOTE: no 'value' included by design
            }
        )

    sanitized: dict[str, Any] = {
        "id": item.get("id"),
        "title": item.get("title"),
        "category": item.get("category"),
        "vault": vault_name,
        "tags": item.get("tags") or [],
        "urls": urls,
        "createdAt": item.get("createdAt"),
        "updatedAt": item.get("updatedAt"),
        "archived": item.get("archived", False),
        "sections": sections_out,
        "fields": fields_out,
    }

    return sanitized


def sanitize_export(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a fully sanitized export dict containing only non-sensitive metadata."""
    accounts_raw = raw.get("accounts") or []
    vaults_raw = raw.get("vaults") or []
    items_raw = raw.get("items") or []

    # Accounts and vaults: keep as-is except for obviously secret keys in accounts.
    accounts_sanitized = (
        sanitize_accounts(accounts_raw) if isinstance(accounts_raw, list) else []
    )
    vaults_sanitized = vaults_raw if isinstance(vaults_raw, list) else []

    items_sanitized: list[dict[str, Any]] = []
    if isinstance(items_raw, list):
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            items_sanitized.append(sanitize_item(item))

    return {
        "accounts": accounts_sanitized,
        "vaults": vaults_sanitized,
        "items": items_sanitized,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract non-sensitive metadata from a 1Password 1PUX export."
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        help=(
            "Path to 1Password 1PUX export file (e.g., export.1pux). "
            "If omitted, the script will look in the user's Downloads directory "
            "for the most recent *.1pux file."
        ),
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        required=False,
        help=(
            "Path to write sanitized JSON output. "
            "If omitted, the script will write to data/imports/1password/ with a "
            "timestamped filename (1password-sanitized-YYYYMMDD-HHMMSS.json) "
            "relative to the repository root."
        ),
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output with indentation.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # If no explicit input path is provided, try to auto-detect the most recent
    # .1pux file in the user's Downloads directory.
    input_path: Path | None
    if args.input is not None:
        input_path = args.input
    else:
        downloads = Path(os.path.expanduser("~/Downloads"))
        if not downloads.is_dir():
            print(
                "No input path provided and Downloads directory not found; "
                "please specify a .1pux file explicitly.",
                file=sys.stderr,  # noqa: F823
            )
            return 1

        candidates = sorted(
            downloads.glob("*.1pux"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        if not candidates:
            print(
                "No input path provided and no *.1pux files found in Downloads; "
                "please export from 1Password and/or specify the file path.",
                file=sys.stderr,
            )
            return 1

        input_path = candidates[0]
        print(f"Using latest 1PUX from Downloads: {input_path}", file=sys.stderr)

    # Determine output path: explicit argument wins; otherwise default to
    # data/imports/1password/1password-sanitized-YYYYMMDD-HHMMSS.json under the repo root.
    if args.output is not None:
        output_path: Path = args.output
    else:
        # Assume this script lives under <repo>/scripts/
        script_path = Path(__file__).resolve()
        repo_root = script_path.parents[1]
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        import sys

        sys.path.insert(0, str(repo_root))
        from scripts.config import get_data_dir

        data_dir = get_data_dir()
        output_dir = data_dir / "imports" / "1password"
        output_path = output_dir / f"1password-sanitized-{timestamp}.json"
        print(f"No --output provided; writing to {output_path}", file=sys.stderr)

    try:
        raw_export = load_export_data(input_path)
    except Exception as exc:
        print(f"Error loading 1PUX export: {exc}", file=sys.stderr)
        return 1

    sanitized = sanitize_export(raw_export)

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            if args.pretty:
                json.dump(sanitized, f, ensure_ascii=False, indent=2)
            else:
                json.dump(sanitized, f, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:  # pragma: no cover - defensive
        print(f"Error writing output file {args.output}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
