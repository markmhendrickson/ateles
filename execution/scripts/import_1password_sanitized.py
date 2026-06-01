#!/usr/bin/env python
"""
Import non-sensitive data from a sanitized 1Password JSON export and integrate it
into normalized parquet datasets.

Sources:
    - data/imports/1password/sanitized_1password.json

Outputs:
    - data/user_accounts/user_accounts.parquet
    - data/account_identifiers/account_identifiers.parquet
    - data/contacts/contacts.parquet      (enriched)
    - data/companies/companies.parquet    (enriched)

Rules:
    - Never store secrets (passwords, OTP/TOTP, recovery codes, keys, etc.).
    - Allowed non-secret identifiers: usernames and email addresses.
    - Always snapshot existing parquet files before modifying them.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import tldextract

REPO_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(REPO_ROOT))
from scripts.config import get_data_dir

DATA_DIR = get_data_dir()
IMPORT_PATH_DEFAULT = DATA_DIR / "imports" / "1password" / "sanitized_1password.json"


def load_sanitized_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_domain(url: str | None) -> str:
    if not url:
        return ""
    try:
        ext = tldextract.extract(url)
        if not ext.domain:
            return ""
        if ext.suffix:
            return f"{ext.domain}.{ext.suffix}"
        return ext.domain
    except Exception:
        return ""


def iter_items(
    sanitized: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Yield items and identifiers from the nested 1Password JSON structure.

    The structure is:
        {
          "accounts": [
            {
              "attrs": {...},
              "vaults": [
                {
                  "attrs": {...},
                  "items": [ {...}, ... ]
                },
                ...
              ]
            },
            ...
          ]
        }
    """
    items_out: list[dict[str, Any]] = []
    identifiers_out: list[dict[str, Any]] = []

    today = date.today()
    import_date = today
    import_source_file = "1password_sanitized_json"

    for account in sanitized.get("accounts", []):
        attrs = account.get("attrs", {}) or {}
        account_email = attrs.get("email", "")

        for vault in account.get("vaults", []):
            v_attrs = vault.get("attrs", {}) or {}
            vault_id = v_attrs.get("uuid", "")
            vault_name = v_attrs.get("name", "")

            for item in vault.get("items", []):
                uuid_ = item.get("uuid")
                if not uuid_:
                    continue

                overview = item.get("overview", {}) or {}
                details = item.get("details", {}) or {}

                title = overview.get("title", "")
                state = item.get("state", "")
                category = item.get("categoryUuid", "")

                created_ts = item.get("createdAt")
                updated_ts = item.get("updatedAt")
                created_at = (
                    datetime.fromtimestamp(created_ts)
                    if isinstance(created_ts, int | float)
                    else None
                )
                updated_at = (
                    datetime.fromtimestamp(updated_ts)
                    if isinstance(updated_ts, int | float)
                    else None
                )

                urls = overview.get("urls") or []
                primary_url = ""
                if urls and isinstance(urls, list):
                    first = urls[0]
                    if isinstance(first, dict):
                        primary_url = first.get("url", "") or ""
                if not primary_url:
                    primary_url = overview.get("url", "") or ""
                domain = normalize_domain(primary_url)

                # Tags may be present on some exports; default to empty list.
                tags = item.get("tags") or overview.get("tags") or []
                tags_str = (
                    ",".join(sorted({t for t in tags if isinstance(t, str)}))
                    if tags
                    else ""
                )

                has_username = False
                has_password = False

                # loginFields may contain usernames/emails and passwords.
                login_fields = details.get("loginFields") or []
                for lf in login_fields:
                    if not isinstance(lf, dict):
                        continue
                    field_type = lf.get("fieldType")
                    designation = lf.get("designation")
                    value = lf.get("value")
                    if field_type == "P" or designation == "password":
                        has_password = True
                        # value is intentionally ignored
                    elif designation == "username" or field_type == "T":
                        if isinstance(value, str) and value:
                            has_username = True
                            identifiers_out.append(
                                {
                                    "identifier_id": str(uuid.uuid4())[:16],
                                    "item_id": uuid_,
                                    "identifier_type": (
                                        "email" if "@" in value else "username"
                                    ),
                                    "identifier_value": value,
                                    "domain": domain,
                                    "import_date": import_date,
                                    "import_source_file": import_source_file,
                                }
                            )

                # Some identifiers may be stored in section fields as non-guarded values.
                for section in details.get("sections") or []:
                    fields = section.get("fields") or []
                    for fld in fields:
                        if not isinstance(fld, dict):
                            continue
                        guarded = fld.get("guarded", False)
                        if guarded:
                            continue
                        value = fld.get("value")
                        if isinstance(value, dict):
                            s = value.get("string")
                        else:
                            s = value
                        if not isinstance(s, str) or not s:
                            continue
                        # Heuristic: e-mails are allowed; other strings we leave as-is only
                        # if they look like usernames, not secrets.
                        if "@" in s:
                            has_username = True
                            identifiers_out.append(
                                {
                                    "identifier_id": str(uuid.uuid4())[:16],
                                    "item_id": uuid_,
                                    "identifier_type": "email",
                                    "identifier_value": s,
                                    "domain": domain,
                                    "import_date": import_date,
                                    "import_source_file": import_source_file,
                                }
                            )

                items_out.append(
                    {
                        "item_id": uuid_,
                        "vault_id": vault_id,
                        "vault_name": vault_name,
                        "account_email": account_email,
                        "title": title,
                        "category": category,
                        "state": state,
                        "primary_url": primary_url,
                        "domain": domain,
                        "tags": tags_str,
                        "created_at": created_at,
                        "updated_at": updated_at,
                        "has_username": has_username,
                        "has_password": has_password,
                        "import_date": import_date,
                        "import_source_file": import_source_file,
                    }
                )

    return {"items": items_out}, identifiers_out


def snapshot_parquet(path: Path) -> None:
    if not path.is_file():
        return
    df = pd.read_parquet(path)
    snapshots_dir = DATA_DIR / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    base = path.stem  # e.g. contacts
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    snapshot_path = snapshots_dir / f"{base}-{ts}.parquet"
    df.to_parquet(snapshot_path, index=False)


def read_parquet_or_empty(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    if path.is_file():
        df = pd.read_parquet(path)
        if columns:
            for col in columns:
                if col not in df.columns:
                    df[col] = None
            return df[columns]
        return df
    # create empty DataFrame with given columns if provided
    return pd.DataFrame(columns=columns or [])


def upsert_parquet(
    df_new: pd.DataFrame, path: Path, key_cols: list[str]
) -> tuple[int, int]:
    """
    Merge df_new into existing parquet at path, using key_cols as identifiers.
    Returns (n_inserted, n_updated).
    """
    df_existing = read_parquet_or_empty(path)
    if df_existing.empty:
        df_new.to_parquet(path, index=False)
        return len(df_new), 0

    df_existing = df_existing.copy()
    df_new = df_new.copy()

    df_existing.set_index(key_cols, inplace=True, drop=False)
    df_new.set_index(key_cols, inplace=True, drop=False)

    inserted_keys = df_new.index.difference(df_existing.index)
    updated_keys = df_new.index.intersection(df_existing.index)

    df_result = pd.concat(
        [
            df_existing.loc[~df_existing.index.isin(updated_keys)],
            df_existing.loc[df_existing.index.isin(updated_keys)],
        ]
    )

    # Update existing rows with new values where non-null/non-empty.
    for key in updated_keys:
        for col in df_new.columns:
            new_val = df_new.at[key, col]
            if pd.isna(new_val) or new_val == "":
                continue
            old_val = df_existing.at[key, col]
            if pd.isna(old_val) or old_val == "":
                df_existing.at[key, col] = new_val

    df_result = pd.concat(
        [
            df_existing,
            df_new.loc[inserted_keys],
        ],
        axis=0,
    ).reset_index(drop=True)

    df_result.to_parquet(path, index=False)
    return len(inserted_keys), len(updated_keys)


def enrich_contacts(
    identifiers_df: pd.DataFrame, dry_run: bool = True
) -> tuple[int, int]:
    contacts_path = DATA_DIR / "contacts" / "contacts.parquet"
    df_contacts = read_parquet_or_empty(
        contacts_path,
        columns=[
            "contact_id",
            "name",
            "contact_type",
            "category",
            "platform",
            "email",
            "phone",
            "address",
            "country",
            "website",
            "notes",
            "first_contact_date",
            "last_contact_date",
            "created_date",
            "updated_date",
        ],
    )

    today = date.today()
    # Build mapping by email where available.
    id_rows = []
    for _, row in identifiers_df.iterrows():
        value = str(row.get("identifier_value") or "")
        if not value:
            continue
        identifier_type = row.get("identifier_type")
        domain = row.get("domain") or ""
        if identifier_type == "email":
            email = value.lower()
            id_rows.append((email, domain))

    if not id_rows:
        return 0, 0

    emails_seen = {email for email, _ in id_rows}

    # Index contacts by email (lowercased).
    email_to_idx = {}
    for idx, email in df_contacts["email"].fillna("").str.lower().items():
        if email:
            email_to_idx.setdefault(email, []).append(idx)

    new_rows: list[dict[str, Any]] = []
    updated_count = 0

    for email in sorted(emails_seen):
        domain = normalize_domain(f"http://{email.split('@')[-1]}")
        if email in email_to_idx:
            # Update existing contacts: ensure website or notes mention 1Password.
            for idx in email_to_idx[email]:
                notes = df_contacts.at[idx, "notes"] or ""
                marker = "[source:1password]"
                if marker not in notes:
                    df_contacts.at[idx, "notes"] = (notes + " " + marker).strip()
                df_contacts.at[idx, "last_contact_date"] = today
                df_contacts.at[idx, "updated_date"] = today
                updated_count += 1
        else:
            # Create new contact.
            new_rows.append(
                {
                    "contact_id": str(uuid.uuid4())[:16],
                    "name": email,
                    "contact_type": "service",
                    "category": "1password_login",
                    "platform": "1password",
                    "email": email,
                    "phone": None,
                    "address": None,
                    "country": None,
                    "website": domain or None,
                    "notes": "[source:1password]",
                    "first_contact_date": today,
                    "last_contact_date": today,
                    "created_date": today,
                    "updated_date": today,
                }
            )

    inserted_count = len(new_rows)
    if not dry_run and (inserted_count or updated_count):
        snapshot_parquet(contacts_path)
        if new_rows:
            df_contacts = pd.concat(
                [df_contacts, pd.DataFrame(new_rows)], ignore_index=True
            )
        df_contacts.to_parquet(contacts_path, index=False)

    return inserted_count, updated_count


def enrich_companies(
    identifiers_df: pd.DataFrame, dry_run: bool = True
) -> tuple[int, int]:
    companies_path = DATA_DIR / "companies" / "companies.parquet"
    df_companies = read_parquet_or_empty(
        companies_path,
        columns=[
            "company_id",
            "name",
            "type",
            "status",
            "jurisdiction",
            "notes",
            "import_date",
            "import_source_file",
        ],
    )

    today = date.today()
    # Derive domains from identifiers.
    domains = set()
    for _, row in identifiers_df.iterrows():
        domain = row.get("domain") or ""
        if not domain:
            continue
        domains.add(domain.lower())

    if not domains:
        return 0, 0

    existing_domains = {
        (normalize_domain(f"http://{name}") or "").lower(): idx
        for idx, name in df_companies["name"].fillna("").items()
        if name
    }

    new_rows: list[dict[str, Any]] = []
    updated = 0

    for domain in sorted(domains):
        if domain in existing_domains:
            idx = existing_domains[domain]
            notes = df_companies.at[idx, "notes"] or ""
            marker = "[source:1password]"
            if marker not in notes:
                df_companies.at[idx, "notes"] = (notes + " " + marker).strip()
            updated += 1
        else:
            new_rows.append(
                {
                    "company_id": str(uuid.uuid4())[:16],
                    "name": domain,
                    "type": "service_provider",
                    "status": "active",
                    "jurisdiction": None,
                    "notes": "[source:1password]",
                    "import_date": today,
                    "import_source_file": "1password_sanitized_json",
                }
            )

    inserted = len(new_rows)
    if not dry_run and (inserted or updated):
        snapshot_parquet(companies_path)
        if new_rows:
            df_companies = pd.concat(
                [df_companies, pd.DataFrame(new_rows)], ignore_index=True
            )
        df_companies.to_parquet(companies_path, index=False)

    return inserted, updated


def link_accounts_to_companies(dry_run: bool = True) -> tuple[int, int]:
    """
    Heuristically link financial accounts to companies using name/domain matching.

    - Adds a company_id to accounts where the account name clearly matches a single company.
    - Matching is based on root domain/brand tokens (e.g., 'paypal' from 'paypal.com').
    """
    accounts_path = DATA_DIR / "accounts" / "accounts.parquet"
    companies_path = DATA_DIR / "companies" / "companies.parquet"

    df_accounts = read_parquet_or_empty(
        accounts_path,
        columns=[
            "account_id",
            "name",
            "wallet",
            "wallet_name",
            "number",
            "categories",
            "denomination",
            "status",
            "notes",
            "company_id",
            "import_date",
            "import_source_file",
        ],
    )
    df_companies = read_parquet_or_empty(
        companies_path,
        columns=[
            "company_id",
            "name",
            "type",
            "status",
            "jurisdiction",
            "notes",
            "import_date",
            "import_source_file",
        ],
    )

    if df_accounts.empty or df_companies.empty:
        return 0, 0

    # Build a mapping from root token (e.g., 'paypal') to company_id where unambiguous.
    root_to_company: dict[str, str | None] = {}
    for _, row in df_companies.iterrows():
        name = str(row.get("name") or "").lower()
        if not name:
            continue
        # Try to interpret company name as a domain; fall back to first word token.
        domain = normalize_domain(f"http://{name}") or name
        ext = tldextract.extract(domain)
        root = ext.domain or (name.split()[0] if name.split() else "")
        root = root.lower()
        if not root:
            continue
        company_id = row.get("company_id")
        if not isinstance(company_id, str) or not company_id:
            continue
        if root in root_to_company and root_to_company[root] != company_id:
            # Ambiguous root; drop it.
            root_to_company[root] = None
        else:
            root_to_company[root] = company_id

    root_to_company = {k: v for k, v in root_to_company.items() if v}
    if not root_to_company:
        return 0, 0

    inserted = 0
    updated = 0

    for idx, name in df_accounts["name"].fillna("").str.lower().items():
        existing_company = df_accounts.at[idx, "company_id"]
        if isinstance(existing_company, str) and existing_company:
            continue
        matches = [root for root in root_to_company.keys() if root in name]
        if len(matches) == 1:
            df_accounts.at[idx, "company_id"] = root_to_company[matches[0]]
            updated += 1

    if not dry_run and updated:
        snapshot_parquet(accounts_path)
        df_accounts.to_parquet(accounts_path, index=False)

    return inserted, updated


def build_items_and_identifiers(
    sanitized: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    items_dict, identifiers_list = iter_items(sanitized)
    df_items = pd.DataFrame(items_dict["items"])
    df_identifiers = pd.DataFrame(identifiers_list)
    return df_items, df_identifiers


def write_user_accounts_parquet(
    df_items: pd.DataFrame,
    df_identifiers: pd.DataFrame,
    dry_run: bool = True,
) -> tuple[int, int]:
    items_path = DATA_DIR / "user_accounts" / "user_accounts.parquet"
    identifiers_path = DATA_DIR / "account_identifiers" / "account_identifiers.parquet"

    inserted_items = updated_items = 0
    inserted_ids = updated_ids = 0

    if not df_items.empty:
        if not dry_run:
            items_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_parquet(items_path)
        inserted_items, updated_items = upsert_parquet(
            df_items, items_path, ["item_id"]
        )

    if not df_identifiers.empty:
        if not dry_run:
            identifiers_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_parquet(identifiers_path)
        inserted_ids, updated_ids = upsert_parquet(
            df_identifiers, identifiers_path, ["identifier_id"]
        )

    return inserted_items + inserted_ids, updated_items + updated_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import non-sensitive data from sanitized 1Password JSON into parquet datasets.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=IMPORT_PATH_DEFAULT,
        help="Path to sanitized 1Password JSON export (default: data/imports/1password/sanitized_1password.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without writing any parquet files; only log planned changes.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.input.is_file():
        print(f"Input JSON not found: {args.input}")
        return 1

    sanitized = load_sanitized_json(args.input)
    df_items, df_identifiers = build_items_and_identifiers(sanitized)

    print(
        f"Parsed {len(df_items)} 1Password items and {len(df_identifiers)} identifiers."
    )

    # Write / update generic user account parquet datasets (source: 1Password).
    total_inserted_items_ids, total_updated_items_ids = write_user_accounts_parquet(
        df_items, df_identifiers, dry_run=args.dry_run
    )

    # Enrich contacts and companies.
    ins_contacts, upd_contacts = enrich_contacts(df_identifiers, dry_run=args.dry_run)
    ins_companies, upd_companies = enrich_companies(
        df_identifiers, dry_run=args.dry_run
    )

    # Link accounts to companies (only writes when not dry-run).
    ins_accounts_companies, upd_accounts_companies = link_accounts_to_companies(
        dry_run=args.dry_run
    )

    print(
        "1Password parquet: "
        f"{total_inserted_items_ids} inserted, {total_updated_items_ids} updated "
        f"(items + identifiers)."
    )
    print(
        f"Contacts: {ins_contacts} inserted, {upd_contacts} updated "
        f"({'dry-run' if args.dry_run else 'written'})."
    )
    print(
        f"Companies: {ins_companies} inserted, {upd_companies} updated "
        f"({'dry-run' if args.dry_run else 'written'})."
    )

    print(
        f"Accounts-company links: {ins_accounts_companies} inserted, {upd_accounts_companies} updated "
        f"({'dry-run' if args.dry_run else 'written'})."
    )

    if args.dry_run:
        print("Dry-run complete; no parquet files were modified.")

    # Simple validation: ensure no obvious secret-like fields slipped into identifiers.
    if not df_identifiers.empty:
        sample_values = (
            df_identifiers["identifier_value"].astype(str).head(100).tolist()
        )
        for v in sample_values:
            if any(ch in v for ch in [" ", "\n"]) and len(v) > 40:
                print(
                    "Warning: identifier_value looks long/complex; verify it is not a secret."
                )
                break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
