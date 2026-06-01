#!/usr/bin/env python3
"""
Deduplicate user_accounts parquet file while preserving all data and tracking
1Password source details for reconciliation.

Strategy:
- Groups duplicates by domain+account_identifier (email or username)
  * If domain exists: group by domain + account_email (or username from account_identifiers)
  * If domain missing: group by title only
- Detects different accounts: Accounts with same domain but different emails/usernames are kept separate
- Merges duplicates intelligently:
  * Prefer active over archived
  * Prefer newer updated_at over older
  * Prefer records with more complete data (non-empty fields)
  * Preserve all unique data from duplicates
- Stores all merged item_ids in merged_item_ids field (JSON array)
- Stores all vault_ids and vault_names in merged_vaults field (JSON array)
- Preserves earliest created_at and latest updated_at
- Tracks which 1Password items were merged for reconciliation

Usage:
    python execution/scripts/deduplicate_user_accounts.py              # Dry run (preview changes)
    python execution/scripts/deduplicate_user_accounts.py --apply       # Apply deduplication
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(REPO_ROOT))
from scripts.config import get_data_dir

DATA_DIR = get_data_dir()
USER_ACCOUNTS_FILE = DATA_DIR / "user_accounts" / "user_accounts.parquet"
ACCOUNT_IDENTIFIERS_FILE = (
    DATA_DIR / "account_identifiers" / "account_identifiers.parquet"
)
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
SNAPSHOTS_DIR.mkdir(exist_ok=True)


def load_account_identifiers() -> pd.DataFrame:
    """Load account_identifiers to map item_ids to emails/usernames."""
    if ACCOUNT_IDENTIFIERS_FILE.exists():
        return pd.read_parquet(ACCOUNT_IDENTIFIERS_FILE)
    return pd.DataFrame()


def get_account_identifier_for_item(
    item_id: str, account_email: str, identifiers_df: pd.DataFrame
) -> str:
    """
    Get the account identifier (email or username) for an item_id.
    Priority: email from identifiers > username from identifiers > account_email from user_accounts

    Note: account_identifiers is more accurate as it's extracted from the actual login item,
    while account_email in user_accounts is the vault owner's email.
    """
    # First try to find in account_identifiers (more accurate)
    item_identifiers = identifiers_df[identifiers_df["item_id"] == item_id]
    if not item_identifiers.empty:
        # Prefer email over username
        emails = item_identifiers[item_identifiers["identifier_type"] == "email"]
        if not emails.empty:
            email_val = emails.iloc[0]["identifier_value"]
            if pd.notna(email_val) and str(email_val).strip():
                return str(email_val).strip().lower()

        # Fall back to username
        usernames = item_identifiers[item_identifiers["identifier_type"] == "username"]
        if not usernames.empty:
            username_val = usernames.iloc[0]["identifier_value"]
            if pd.notna(username_val) and str(username_val).strip():
                return str(username_val).strip().lower()

    # Fall back to account_email from user_accounts
    if pd.notna(account_email) and str(account_email).strip():
        return str(account_email).strip().lower()

    return ""


def get_match_key(row: pd.Series, identifiers_df: pd.DataFrame) -> str:
    """
    Generate a match key for duplicate detection.
    Uses domain + account_identifier (email/username) to detect same account.
    Falls back to title+domain if domain missing or identifier unavailable.
    """
    domain = str(row.get("domain", "")).strip()
    title = str(row.get("title", "")).strip()
    account_email = row.get("account_email", "")
    item_id = row.get("item_id", "")

    # If domain exists, use domain + account identifier
    if domain and domain != "nan" and domain:
        account_id = get_account_identifier_for_item(
            item_id, account_email, identifiers_df
        )
        if account_id:
            return f"{domain}|||{account_id}"
        # If no identifier found, use domain + title (less reliable but better than nothing)
        return f"{domain}|||{title}"

    # If no domain, use title only
    return f"NO_DOMAIN|||{title}"


def merge_duplicates(group: pd.DataFrame) -> pd.Series:
    """
    Merge a group of duplicate records into a single record.
    Preserves all data and tracks merged item_ids.
    """
    if len(group) == 1:
        row = group.iloc[0].copy()
        # Initialize merged fields even for non-duplicates
        row["merged_item_ids"] = json.dumps([row["item_id"]])
        row["merged_vaults"] = json.dumps(
            [
                {
                    "vault_id": row.get("vault_id", ""),
                    "vault_name": row.get("vault_name", ""),
                }
            ]
        )
        return row

    # Sort by priority: active > archived, newer > older, more complete > less complete
    def priority_score(row: pd.Series) -> tuple[int, int, int]:
        state_priority = 1 if row.get("state") == "active" else 0
        updated_at_priority = row.get("updated_at", pd.Timestamp.min)
        if pd.isna(updated_at_priority):
            updated_at_priority = pd.Timestamp.min

        # Count non-empty fields
        completeness = sum(
            [
                (
                    1
                    if pd.notna(row.get("domain"))
                    and str(row.get("domain", "")).strip()
                    else 0
                ),
                (
                    1
                    if pd.notna(row.get("primary_url"))
                    and str(row.get("primary_url", "")).strip()
                    else 0
                ),
                (
                    1
                    if pd.notna(row.get("tags")) and str(row.get("tags", "")).strip()
                    else 0
                ),
                (
                    1
                    if pd.notna(row.get("account_email"))
                    and str(row.get("account_email", "")).strip()
                    else 0
                ),
            ]
        )

        return (state_priority, updated_at_priority, completeness)

    group_sorted = group.copy()
    group_sorted["_priority"] = group_sorted.apply(priority_score, axis=1)
    group_sorted = group_sorted.sort_values("_priority", ascending=False)

    # Use the highest priority record as the base
    base = group_sorted.iloc[0].copy()

    # Collect all item_ids and vault info
    all_item_ids = group_sorted["item_id"].tolist()
    all_vaults = []
    seen_vaults = set()

    for _, row in group_sorted.iterrows():
        vault_id = str(row.get("vault_id", ""))
        vault_name = str(row.get("vault_name", ""))
        vault_key = f"{vault_id}|||{vault_name}"

        if vault_key not in seen_vaults:
            all_vaults.append({"vault_id": vault_id, "vault_name": vault_name})
            seen_vaults.add(vault_key)

    # Merge data from other records (fill in missing fields)
    for _, row in group_sorted.iloc[1:].iterrows():
        # Fill in domain if base is missing
        if (
            (pd.isna(base.get("domain")) or not str(base.get("domain", "")).strip())
            and pd.notna(row.get("domain"))
            and str(row.get("domain", "")).strip()
        ):
            base["domain"] = row["domain"]

        # Fill in primary_url if base is missing
        if (
            (
                pd.isna(base.get("primary_url"))
                or not str(base.get("primary_url", "")).strip()
            )
            and pd.notna(row.get("primary_url"))
            and str(row.get("primary_url", "")).strip()
        ):
            base["primary_url"] = row["primary_url"]

        # Merge account_email (prefer non-empty, keep both if different)
        if (
            pd.notna(row.get("account_email"))
            and str(row.get("account_email", "")).strip()
        ):
            base_email = (
                str(base.get("account_email", "")).strip()
                if pd.notna(base.get("account_email"))
                else ""
            )
            row_email = str(row.get("account_email", "")).strip()
            if not base_email:
                base["account_email"] = row_email
            elif base_email.lower() != row_email.lower():
                # Different emails - this shouldn't happen if grouping is correct, but log it
                pass

        # Merge tags (combine unique tags)
        if pd.notna(row.get("tags")) and str(row.get("tags", "")).strip():
            base_tags = str(base.get("tags", "")).strip()
            row_tags = str(row.get("tags", "")).strip()
            if base_tags and row_tags and base_tags != row_tags:
                # Combine tags (simple approach - could be improved)
                base["tags"] = f"{base_tags}, {row_tags}"
            elif not base_tags:
                base["tags"] = row_tags

        # Prefer active state if any record is active
        if row.get("state") == "active":
            base["state"] = "active"

        # Update has_username/has_password if True in any record
        if row.get("has_username"):
            base["has_username"] = True
        if row.get("has_password"):
            base["has_password"] = True

        # Use earliest created_at
        if pd.notna(row.get("created_at")):
            if (
                pd.isna(base.get("created_at"))
                or row["created_at"] < base["created_at"]
            ):
                base["created_at"] = row["created_at"]

        # Use latest updated_at
        if pd.notna(row.get("updated_at")):
            if (
                pd.isna(base.get("updated_at"))
                or row["updated_at"] > base["updated_at"]
            ):
                base["updated_at"] = row["updated_at"]

    # Store merged item_ids and vaults
    # Keep primary item_id first, then sort the rest
    primary_id = all_item_ids[0]
    other_ids = sorted([id for id in all_item_ids[1:]])
    base["merged_item_ids"] = json.dumps([primary_id] + other_ids)
    base["merged_vaults"] = json.dumps(all_vaults)

    # Keep the primary item_id (highest priority)
    base["item_id"] = primary_id

    return base


def deduplicate_user_accounts(dry_run: bool = True) -> dict:
    """Deduplicate user_accounts parquet file."""
    print(f"Loading user_accounts from {USER_ACCOUNTS_FILE}")
    df = pd.read_parquet(USER_ACCOUNTS_FILE)

    print(f"Total records: {len(df)}")

    # Load account identifiers for email/username lookup
    print("Loading account_identifiers for email/username lookup...")
    identifiers_df = load_account_identifiers()
    if not identifiers_df.empty:
        print(f"  Loaded {len(identifiers_df)} identifier records")
    else:
        print(
            "  No account_identifiers found, using account_email from user_accounts only"
        )

    # Create match keys using domain + account identifier
    print("Creating match keys (domain + account identifier)...")
    df["match_key"] = df.apply(lambda row: get_match_key(row, identifiers_df), axis=1)

    # Find duplicates
    dupe_groups = df.groupby("match_key").size()
    dupes = dupe_groups[dupe_groups > 1]

    print(f"Unique match keys: {df['match_key'].nunique()}")
    print(f"Duplicate groups: {len(dupes)}")
    print(f"Total records in duplicate groups: {dupes.sum()}")

    # Show examples of accounts that will be kept separate (same domain, different identifiers)
    print(
        "\nChecking for accounts with same domain but different identifiers (will be kept separate)..."
    )
    domain_groups = df[df["domain"].notna() & (df["domain"] != "")].groupby("domain")
    same_domain_different_accounts = []
    for domain, group in domain_groups:
        if len(group) > 1:
            # Get unique account identifiers for this domain
            account_ids = set()
            for _, row in group.iterrows():
                account_id = get_account_identifier_for_item(
                    row["item_id"], row.get("account_email", ""), identifiers_df
                )
                if account_id:
                    account_ids.add(account_id)

            if len(account_ids) > 1:
                same_domain_different_accounts.append(
                    {
                        "domain": domain,
                        "count": len(group),
                        "unique_accounts": len(account_ids),
                        "account_ids": sorted(list(account_ids))[:3],  # Show first 3
                    }
                )

    if same_domain_different_accounts:
        print(
            f"  Found {len(same_domain_different_accounts)} domains with multiple distinct accounts:"
        )
        for item in sorted(
            same_domain_different_accounts, key=lambda x: x["count"], reverse=True
        )[:10]:
            print(
                f"    {item['domain']}: {item['count']} records, {item['unique_accounts']} distinct accounts"
            )
            print(
                f"      Account identifiers: {', '.join(item['account_ids'])}{'...' if len(item['account_ids']) > 3 else ''}"
            )
    else:
        print("  No domains with multiple distinct accounts found")

    if len(dupes) == 0:
        print("\nNo duplicates found!")
        return {
            "deduplicated": 0,
            "removed": 0,
            "total": len(df),
            "separate_accounts": len(same_domain_different_accounts),
        }

    # Group by match_key and merge duplicates
    print("\nMerging duplicates...")
    merged_records = []
    kept_records = []

    for match_key, group in df.groupby("match_key"):
        if len(group) > 1:
            merged = merge_duplicates(group)
            merged_records.append(merged)
        else:
            # Non-duplicate - add merged fields for consistency
            row = group.iloc[0].copy()
            row["merged_item_ids"] = json.dumps([row["item_id"]])
            row["merged_vaults"] = json.dumps(
                [
                    {
                        "vault_id": row.get("vault_id", ""),
                        "vault_name": row.get("vault_name", ""),
                    }
                ]
            )
            kept_records.append(row)

    # Combine merged and kept records
    merged_df = pd.DataFrame(merged_records)
    kept_df = pd.DataFrame(kept_records)

    if not merged_df.empty and not kept_df.empty:
        deduplicated_df = pd.concat([merged_df, kept_df], ignore_index=True)
    elif not merged_df.empty:
        deduplicated_df = merged_df
    else:
        deduplicated_df = kept_df

    # Remove temporary columns
    deduplicated_df = deduplicated_df.drop(
        columns=["match_key", "_priority"], errors="ignore"
    )

    print("\nAfter deduplication:")
    print(f"  Total records: {len(deduplicated_df)}")
    print(f"  Records removed: {len(df) - len(deduplicated_df)}")
    print(
        f"  Reduction: {len(df) - len(deduplicated_df)} records ({100 * (len(df) - len(deduplicated_df)) / len(df):.1f}%)"
    )

    # Show sample merged records
    print("\nSample merged records:")
    merged_samples = deduplicated_df[
        deduplicated_df["merged_item_ids"].apply(lambda x: len(json.loads(x)) > 1)
    ].head(10)

    for _, row in merged_samples.iterrows():
        merged_ids = json.loads(row["merged_item_ids"])
        merged_vaults = json.loads(row["merged_vaults"])
        print(f"\n  {row['title']} ({row.get('domain', 'NO_DOMAIN')}):")
        print(f"    Account: {row.get('account_email', 'N/A')}")
        print(f"    Primary item_id: {row['item_id']}")
        print(
            f"    Merged item_ids ({len(merged_ids)}): {', '.join(merged_ids[:3])}{'...' if len(merged_ids) > 3 else ''}"
        )
        print(
            f"    Vaults ({len(merged_vaults)}): {', '.join([v['vault_name'] for v in merged_vaults])}"
        )
        print(f"    State: {row.get('state')}")

    if dry_run:
        print("\n*** DRY RUN - No changes applied ***")
        print("Run with --apply to apply deduplication")
        return {
            "deduplicated": len(merged_records),
            "removed": len(df) - len(deduplicated_df),
            "total": len(deduplicated_df),
            "separate_accounts": len(same_domain_different_accounts),
            "dry_run": True,
        }

    # Create snapshot
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    snapshot_file = SNAPSHOTS_DIR / f"user_accounts-{timestamp}.parquet"
    print(f"\nCreating snapshot: {snapshot_file}")
    df.to_parquet(snapshot_file, index=False)

    # Save deduplicated data
    print(f"Saving deduplicated data to {USER_ACCOUNTS_FILE}")
    deduplicated_df.to_parquet(USER_ACCOUNTS_FILE, index=False)

    print("\n✓ Deduplication complete!")
    return {
        "deduplicated": len(merged_records),
        "removed": len(df) - len(deduplicated_df),
        "total": len(deduplicated_df),
        "separate_accounts": len(same_domain_different_accounts),
        "snapshot": str(snapshot_file),
        "dry_run": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Deduplicate user_accounts parquet file"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Apply deduplication (default is dry run)"
    )
    args = parser.parse_args()

    result = deduplicate_user_accounts(dry_run=not args.apply)
    print(f"\nResult: {result}")
