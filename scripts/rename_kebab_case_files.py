#!/usr/bin/env python3
"""
Rename kebab-case Python files to snake_case.

This script renames all Python files with kebab-case names to snake_case
to comply with Python naming conventions (PEP 8).
"""

import re
from pathlib import Path

# Directory containing scripts
SCRIPTS_DIR = Path(__file__).parent.parent / "execution" / "scripts"


def kebab_to_snake(name: str) -> str:
    """Convert kebab-case to snake_case."""
    return name.replace("-", "_")


def rename_file(old_path: Path) -> Path:
    """Rename a file from kebab-case to snake_case."""
    old_name = old_path.name
    if "-" not in old_name:
        return old_path  # Already snake_case or other format

    # Convert kebab-case to snake_case
    new_name = kebab_to_snake(old_name)
    new_path = old_path.parent / new_name

    if new_path.exists():
        print(f"  WARNING: Target already exists: {new_path}")
        return old_path

    print(f"  Renaming: {old_name} -> {new_name}")
    old_path.rename(new_path)
    return new_path


def find_kebab_case_files(directory: Path) -> list[Path]:
    """Find all Python files with kebab-case names."""
    kebab_files = []
    for file_path in directory.glob("*.py"):
        if "-" in file_path.name:
            kebab_files.append(file_path)
    return sorted(kebab_files)


def update_imports_in_file(file_path: Path, old_name: str, new_name: str):
    """Update import statements that reference the renamed file."""
    try:
        content = file_path.read_text(encoding="utf-8")
        original_content = content

        # Pattern to match imports of the old module name
        # Matches: from old_name import ... or import old_name
        old_module = old_name.replace(".py", "")
        new_module = new_name.replace(".py", "")

        # Update various import patterns
        patterns = [
            (rf"from\s+{re.escape(old_module)}\s+import", f"from {new_module} import"),
            (rf"import\s+{re.escape(old_module)}\b", f"import {new_module}"),
            (rf"'{re.escape(old_module)}'", f"'{new_module}'"),
            (rf'"{re.escape(old_module)}"', f'"{new_module}"'),
        ]

        for pattern, replacement in patterns:
            content = re.sub(pattern, replacement, content)

        if content != original_content:
            file_path.write_text(content, encoding="utf-8")
            print(f"    Updated imports in: {file_path.name}")
    except Exception as e:
        print(f"    ERROR updating {file_path.name}: {e}")


def main():
    """Main function to rename kebab-case files."""
    print("Finding kebab-case Python files...")
    kebab_files = find_kebab_case_files(SCRIPTS_DIR)

    if not kebab_files:
        print("No kebab-case files found.")
        return

    print(f"\nFound {len(kebab_files)} kebab-case file(s):")
    for f in kebab_files:
        print(f"  - {f.name}")

    print("\nRenaming files...")
    renamed = []
    for old_path in kebab_files:
        new_path = rename_file(old_path)
        if new_path != old_path:
            renamed.append((old_path, new_path))

    if renamed:
        print(f"\nRenamed {len(renamed)} file(s).")
        print("\nUpdating import references...")

        # Update imports in all Python files
        for old_path, new_path in renamed:
            old_name = old_path.name
            new_name = new_path.name

            # Update imports in all Python files in the directory
            for py_file in SCRIPTS_DIR.glob("*.py"):
                if py_file != new_path:  # Don't update the renamed file itself
                    update_imports_in_file(py_file, old_name, new_name)

        print("\nDone!")
    else:
        print("No files were renamed.")


if __name__ == "__main__":
    main()
