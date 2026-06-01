#!/usr/bin/env python3
"""
Sync sibling repositories (../) into parquet so /analyze can compare against all repos.

Creates or updates $DATA_DIR/schemas/repositories_schema.json and $DATA_DIR/repositories/repositories.parquet.
Requires DATA_DIR environment variable (e.g. from .env).

Usage:
  DATA_DIR=/path/to/data python execution/scripts/sync_repos_to_parquet.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

# Repo root and DATA_DIR (required from env)
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
_data_dir = os.environ.get("DATA_DIR")
if not _data_dir:
    print("ERROR: DATA_DIR environment variable is not set.", file=sys.stderr)
    print("Set DATA_DIR to your data directory, e.g.:", file=sys.stderr)
    print('  export DATA_DIR="/path/to/data"', file=sys.stderr)
    print("  # or ensure .env is loaded with DATA_DIR=...", file=sys.stderr)
    sys.exit(1)
DATA_DIR = Path(_data_dir)
SCHEMAS_DIR = DATA_DIR / "schemas"
REPOSITORIES_DIR = DATA_DIR / "repositories"
REPOSITORIES_PARQUET = REPOSITORIES_DIR / "repositories.parquet"
# Parent of repo (sibling repos live here)
REPOS_PARENT = REPO_ROOT.parent

REPOS_SCHEMA = {
    "schema": {
        "repo_id": "string",
        "name": "string",
        "path": "string",
        "parent_dir": "string",
        "description": "string",
        "foundation_docs_path": "string",
        "core_identity_path": "string",
        "product_positioning_path": "string",
        "problem_statement_path": "string",
        "philosophy_path": "string",
        "import_date": "date",
        "import_source_file": "string",
        "notes": "string",
    },
    "description": "Repositories in the parent directory (../). Used by /analyze for comparative analysis.",
    "version": "1.0.0",
}


def ensure_schema() -> None:
    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
    schema_path = SCHEMAS_DIR / "repositories_schema.json"
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(REPOS_SCHEMA, f, indent=2)
    print(f"Schema: {schema_path}")


def list_sibling_repos() -> list[dict]:
    """List direct children of REPOS_PARENT that look like repos (have .git or README)."""
    rows = []
    parent_str = str(REPOS_PARENT)
    for entry in sorted(REPOS_PARENT.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        # Treat as repo if it has .git or README
        if (entry / ".git").exists() or (entry / "README.md").exists():
            abs_path = str(entry)
            docs = entry / "docs" / "foundation"
            rows.append(
                {
                    "repo_id": entry.name,
                    "name": entry.name,
                    "path": abs_path,
                    "parent_dir": parent_str,
                    "description": None,
                    "foundation_docs_path": str(docs) if docs.exists() else None,
                    "core_identity_path": (
                        str(docs / "core_identity.md")
                        if (docs / "core_identity.md").exists()
                        else None
                    ),
                    "product_positioning_path": (
                        str(docs / "product_positioning.md")
                        if (docs / "product_positioning.md").exists()
                        else None
                    ),
                    "problem_statement_path": (
                        str(docs / "problem_statement.md")
                        if (docs / "problem_statement.md").exists()
                        else None
                    ),
                    "philosophy_path": (
                        str(docs / "philosophy.md")
                        if (docs / "philosophy.md").exists()
                        else None
                    ),
                    "import_date": date.today().isoformat(),
                    "import_source_file": "sync_repos_to_parquet",
                    "notes": None,
                }
            )
    return rows


def main() -> int:
    import pandas as pd

    if not DATA_DIR.exists():
        print(f"DATA_DIR does not exist: {DATA_DIR}", file=sys.stderr)
        print("Create it or set DATA_DIR to an existing path.", file=sys.stderr)
        return 1

    ensure_schema()
    REPOSITORIES_DIR.mkdir(parents=True, exist_ok=True)

    rows = list_sibling_repos()
    if not rows:
        print("No sibling repos found under", REPOS_PARENT, file=sys.stderr)
        return 0

    df_new = pd.DataFrame(rows)
    if REPOSITORIES_PARQUET.exists():
        df_old = pd.read_parquet(REPOSITORIES_PARQUET)
        # Upsert by repo_id (name)
        for _, row in df_new.iterrows():
            mask = df_old["repo_id"] == row["repo_id"]
            if mask.any():
                df_old.loc[mask] = row.values
            else:
                df_old = pd.concat([df_old, pd.DataFrame([row])], ignore_index=True)
        df = df_old
    else:
        df = df_new

    df.to_parquet(REPOSITORIES_PARQUET, index=False)
    print(f"Wrote {len(df)} repositories to {REPOSITORIES_PARQUET}")
    for name in df["name"].tolist():
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
