#!/usr/bin/env python3
"""
Reconcile .nosync directories by removing the suffix and merging with existing directories.
"""

import os
import shutil
import sys
from pathlib import Path


def find_nosync_dirs(root_path):
    """Find all directories ending with .nosync"""
    nosync_dirs = []
    for root, dirs, files in os.walk(root_path):
        for d in dirs:
            if d.endswith(".nosync"):
                nosync_dirs.append(os.path.join(root, d))
    return nosync_dirs


def reconcile_directories(nosync_path):
    """Reconcile a .nosync directory with its non-suffixed counterpart"""
    nosync_path = Path(nosync_path)
    target_path = nosync_path.parent / nosync_path.name.replace(".nosync", "")

    print(f"\n=== Processing: {nosync_path.name} ===")
    print(f"  .nosync path: {nosync_path}")
    print(f"  Target path: {target_path}")

    # Check if target already exists
    if target_path.exists():
        print("  ✓ Target directory exists, will merge contents")

        # Get contents of both directories
        nosync_items = set(os.listdir(nosync_path))
        target_items = set(os.listdir(target_path))

        # Find items only in .nosync
        nosync_only = nosync_items - target_items
        # Find items in both
        in_both = nosync_items & target_items

        print(f"  Items in .nosync only: {len(nosync_only)}")
        print(f"  Items in both: {len(in_both)}")

        # Check for conflicts (items that exist in both but are different types)
        conflicts = []
        for item in in_both:
            nosync_item = nosync_path / item
            target_item = target_path / item

            nosync_is_dir = nosync_item.is_dir()
            target_is_dir = target_item.is_dir()

            if nosync_is_dir != target_is_dir:
                conflicts.append(item)
                print(
                    f"  ⚠️  CONFLICT: {item} is {'directory' if nosync_is_dir else 'file'} in .nosync but {'directory' if target_is_dir else 'file'} in target"
                )

        if conflicts:
            print(
                "  ❌ Cannot reconcile due to conflicts. Manual intervention required."
            )
            return False

        # Copy items from .nosync to target
        print("  Copying items from .nosync to target...")
        for item in nosync_items:
            nosync_item = nosync_path / item
            target_item = target_path / item

            if target_item.exists():
                # If both are directories, recursively merge
                if nosync_item.is_dir() and target_item.is_dir():
                    print(f"    Merging directory: {item}")

                    # Recursively merge directory contents
                    def merge_dir(src_dir, dst_dir):
                        """Recursively merge src_dir into dst_dir"""
                        src_dir = Path(src_dir)
                        dst_dir = Path(dst_dir)
                        for src_path in src_dir.rglob("*"):
                            rel_path = src_path.relative_to(src_dir)
                            dst_path = dst_dir / rel_path

                            if src_path.is_dir():
                                dst_path.mkdir(parents=True, exist_ok=True)
                            elif src_path.is_file():
                                if not dst_path.exists():
                                    dst_path.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.copy2(src_path, dst_path)

                    merge_dir(nosync_item, target_item)
                # If both are files, skip (keep existing)
                elif nosync_item.is_file() and target_item.is_file():
                    print(f"    Skipping existing file: {item}")
            else:
                # Item doesn't exist in target, copy it
                print(f"    Copying new item: {item}")
                if nosync_item.is_dir():
                    shutil.copytree(nosync_item, target_item)
                else:
                    shutil.copy2(nosync_item, target_item)

        # After successful merge, remove .nosync directory
        print("  ✓ Merge complete, removing .nosync directory...")
        shutil.rmtree(nosync_path)
        print(f"  ✓ Removed {nosync_path}")

    else:
        # Target doesn't exist, just rename
        print("  Target doesn't exist, renaming...")
        nosync_path.rename(target_path)
        print(f"  ✓ Renamed to {target_path}")

    return True


def main():
    # Start from Documents/data (where we know .nosync dirs exist)
    # But also check the entire home directory
    search_paths = [Path.home() / "Documents" / "data", Path.home()]

    all_nosync_dirs = []
    for search_path in search_paths:
        if search_path.exists():
            nosync_dirs = find_nosync_dirs(str(search_path))
            all_nosync_dirs.extend(nosync_dirs)

    # Remove duplicates and sort
    all_nosync_dirs = sorted(set(all_nosync_dirs))

    if not all_nosync_dirs:
        print("No .nosync directories found.")
        return 0

    print(f"Found {len(all_nosync_dirs)} .nosync directories:")
    for d in all_nosync_dirs:
        print(f"  - {d}")

    print("\n" + "=" * 60)
    print("RECONCILIATION PLAN")
    print("=" * 60)

    # Process each directory
    success_count = 0
    for nosync_dir in all_nosync_dirs:
        try:
            if reconcile_directories(nosync_dir):
                success_count += 1
        except Exception as e:
            print(f"  ❌ Error processing {nosync_dir}: {e}")
            import traceback

            traceback.print_exc()

    print("\n" + "=" * 60)
    print(
        f"SUMMARY: Processed {success_count}/{len(all_nosync_dirs)} directories successfully"
    )
    print("=" * 60)

    return 0 if success_count == len(all_nosync_dirs) else 1


if __name__ == "__main__":
    sys.exit(main())
