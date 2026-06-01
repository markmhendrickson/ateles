"""
Sync environment variables from env.example to .env using 1Password CLI.

Design:
- Read ../neotoma/env.example to identify needed environment variables
- Map each variable to a 1Password op:// reference
- Use the `op` CLI to resolve each secret
- Create or update ../neotoma/.env with the resolved values

Safety:
- This script NEVER prints secret values
- It only prints which keys were updated
- Run this locally, not in CI, and never commit .env to git

Requirements:
- 1Password CLI (`op`) installed and signed in (`op signin`)
- Python 3.9+ recommended

Usage:
    python execution/scripts/neotoma_sync_env_from_1password.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Mapping from ENV var names to 1Password op:// references.
# Format: op://<vault>/<item>/<section>/<field-id>
#
# RECOMMENDED: Use DEV_SUPABASE_PROJECT_ID instead of DEV_SUPABASE_URL
# The script will automatically construct the URL from the project ID.
ENV_TO_OP_REF: dict[str, str] = {
    # Supabase Development Environment
    "DEV_SUPABASE_PROJECT_ID": "op://Private/Supabase/add more/kcxd34yc5l62cpbw7erntlvgde",
    "DEV_SUPABASE_SERVICE_KEY": "op://Private/Supabase/add more/j6aegoxuwjx4pd2qlvldlaruxy",
    # Supabase Production Environment (if needed)
    # "PROD_SUPABASE_PROJECT_ID": "op://Private/Supabase/add more/4mjiphwyrexwjh7qvdi3lygo3a",
    # "PROD_SUPABASE_SERVICE_KEY": "op://Private/Supabase/add more/3mgyiaeyy3b4nufmllmryzftgu",
    # Add more mappings as needed:
    # "OPENAI_API_KEY": "op://Private/ItemName/field-id",
    # "PLAID_CLIENT_ID": "op://Private/ItemName/field-id",
    # "PLAID_SECRET": "op://Private/ItemName/field-id",
    # "CONNECTOR_SECRET_KEY": "op://Private/ItemName/field-id",
    # "ACTIONS_BEARER_TOKEN": "op://Private/ItemName/field-id",
}


def op_read(ref: str) -> str:
    """
    Read a secret value from 1Password using `op read <ref>`.
    """
    try:
        result = subprocess.run(
            ["op", "read", ref],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:  # noqa: BLE001
        raise RuntimeError(
            f"1Password CLI error for {ref}: {e.stderr or e.stdout or e}"
        ) from e

    value = result.stdout.rstrip("\n")
    if not value:
        raise RuntimeError(f"Empty value returned for 1Password ref: {ref}")
    return value


def parse_env_example(env_example_path: Path) -> list[tuple[str, str | None]]:
    """
    Parse env.example file to extract variable names and optional default values.

    Returns list of tuples: (var_name, default_value_or_None)
    Ignores comments and empty lines.
    """
    if not env_example_path.exists():
        raise FileNotFoundError(f"env.example not found at {env_example_path}")

    text = env_example_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    variables = []
    for line in lines:
        # Strip whitespace
        line = line.strip()

        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue

        # Parse VAR_NAME=value or VAR_NAME= (empty value)
        if "=" not in line:
            continue

        var_name, value = line.split("=", 1)
        var_name = var_name.strip()

        # Skip if var_name is empty
        if not var_name:
            continue

        # Remove quotes from value if present
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]

        # If value is a placeholder (like "your-api-key-here"), treat as None
        if value and not any(
            placeholder in value.lower()
            for placeholder in ["your-", "your_", "here", "placeholder"]
        ):
            default_value = value
        else:
            default_value = None

        variables.append((var_name, default_value))

    return variables


def construct_supabase_url(project_id: str) -> str:
    """Construct Supabase URL from project ID."""
    return f"https://{project_id}.supabase.co"


def sync_env(env_example_path: Path, target_path: Path) -> None:
    """
    Parse env.example, resolve secrets from 1Password, and create/update .env file.

    Special handling:
    - If DEV_SUPABASE_PROJECT_ID is provided, automatically constructs DEV_SUPABASE_URL
    """
    print(f"Reading variables from: {env_example_path}")
    print(f"Target .env file: {target_path}")

    # Parse env.example to get all variables
    variables = parse_env_example(env_example_path)
    print(f"\nFound {len(variables)} environment variables in env.example")

    # Load existing .env if it exists
    existing_lines = []
    existing_keys = {}
    if target_path.exists():
        existing_text = target_path.read_text(encoding="utf-8")
        existing_lines = existing_text.splitlines()
        for idx, line in enumerate(existing_lines):
            if not line or line.lstrip().startswith("#"):
                continue
            if "=" not in line:
                continue
            k = line.split("=", 1)[0].strip()
            if k:
                existing_keys[k] = idx

    # Build new .env content
    new_lines = []
    updated = []
    skipped = []
    resolved_values = {}  # Track resolved values for derived variables

    for var_name, default_value in variables:
        # Check if we have a 1Password mapping
        if var_name in ENV_TO_OP_REF:
            op_ref = ENV_TO_OP_REF[var_name]
            print(f"- Resolving {var_name} from {op_ref}")
            try:
                value = op_read(op_ref)
                resolved_values[var_name] = value
                new_line = f'{var_name}="{value}"'

                # Update existing line or append
                if var_name in existing_keys:
                    existing_lines[existing_keys[var_name]] = new_line
                    updated.append(var_name)
                else:
                    new_lines.append(new_line)
                    updated.append(var_name)
            except RuntimeError as e:
                print(f"  ERROR: {e}")
                skipped.append(var_name)
                # Keep existing value if present, otherwise skip
                if var_name in existing_keys:
                    new_lines.append(existing_lines[existing_keys[var_name]])
        else:
            # No 1Password mapping - use default value or keep existing
            if var_name in existing_keys:
                # Keep existing value
                new_lines.append(existing_lines[existing_keys[var_name]])
            elif default_value:
                # Use default from env.example
                new_lines.append(f'{var_name}="{default_value}"')
            else:
                # No mapping and no default - skip (or could add empty)
                skipped.append(var_name)
                print(f"- Skipping {var_name} (no 1Password mapping and no default)")

    # Post-process: Construct DEV_SUPABASE_URL from DEV_SUPABASE_PROJECT_ID if needed
    # Only do this if URL wasn't explicitly set from 1Password
    if (
        "DEV_SUPABASE_PROJECT_ID" in resolved_values
        and "DEV_SUPABASE_URL" not in resolved_values
    ):
        project_id = resolved_values["DEV_SUPABASE_PROJECT_ID"]
        url = construct_supabase_url(project_id)
        url_line = f'DEV_SUPABASE_URL="{url}"'

        # Remove any existing DEV_SUPABASE_URL from new_lines (placeholder or default)
        new_lines = [
            line for line in new_lines if not line.startswith("DEV_SUPABASE_URL=")
        ]

        # Update or add the constructed URL
        if "DEV_SUPABASE_URL" in existing_keys:
            existing_lines[existing_keys["DEV_SUPABASE_URL"]] = url_line
            if "DEV_SUPABASE_URL" not in updated:
                updated.append("DEV_SUPABASE_URL")
        else:
            # Find where to insert (after DEV_SUPABASE_PROJECT_ID if it exists)
            inserted = False
            for i, line in enumerate(new_lines):
                if line.startswith("DEV_SUPABASE_PROJECT_ID"):
                    new_lines.insert(i + 1, url_line)
                    inserted = True
                    break
            if not inserted:
                new_lines.append(url_line)
            if "DEV_SUPABASE_URL" not in updated:
                updated.append("DEV_SUPABASE_URL")

        # Remove from skipped if it was skipped
        if "DEV_SUPABASE_URL" in skipped:
            skipped.remove("DEV_SUPABASE_URL")

        print("- Constructed DEV_SUPABASE_URL from DEV_SUPABASE_PROJECT_ID")

    # Write .env file
    # Preserve existing lines that weren't in env.example
    final_lines = []
    written_keys = set()

    # First, add all new/updated lines
    for line in new_lines:
        if "=" in line:
            key = line.split("=", 1)[0].strip()
            written_keys.add(key)
            final_lines.append(line)

    # Then, add updated existing lines
    for idx, line in enumerate(existing_lines):
        if "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in written_keys:
                continue  # Already written
            if key in updated:
                continue  # Already updated
        final_lines.append(line)

    # Write file
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("\n".join(final_lines) + "\n", encoding="utf-8")

    # Print summary
    print("\n" + "=" * 60)
    if updated:
        print(f"Updated {len(updated)} keys in .env (values NOT shown):")
        for k in updated:
            print(f"  - {k}")
    else:
        print("No keys were updated.")

    if skipped:
        print(f"\nSkipped {len(skipped)} keys (no 1Password mapping):")
        for k in skipped:
            print(f"  - {k}")
        print("\nTo add these keys, update ENV_TO_OP_REF mapping in the script.")


def main() -> int:
    # Determine paths relative to script location
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent
    neotoma_dir = repo_root.parent / "neotoma"

    env_example_path = neotoma_dir / "env.example"
    target_path = neotoma_dir / ".env"

    if not env_example_path.exists():
        print(f"ERROR: env.example not found at {env_example_path}")
        print("Make sure the neotoma directory exists at ../neotoma/")
        return 1

    try:
        sync_env(env_example_path, target_path)
        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
