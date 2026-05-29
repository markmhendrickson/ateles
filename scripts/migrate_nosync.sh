#!/bin/bash
set -euo pipefail

DATA_DIR_PATH="/Users/markmhendrickson/Documents/data"

if [ ! -d "$DATA_DIR_PATH" ]; then
    echo "ERROR: DATA_DIR does not exist: $DATA_DIR_PATH" >&2
    exit 1
fi

for name in imports snapshots attachments; do
    src="$DATA_DIR_PATH/$name"
    dst="$DATA_DIR_PATH/${name}.nosync"

    echo "--- Processing $name ---"
    
    # Skip if already a symlink
    if [ -L "$src" ]; then
        echo "OK: $src is already a symlink -> $(readlink "$src")"
        continue
    fi

    # Skip if destination already exists (manual migration case)
    if [ -d "$dst" ]; then
        echo "OK: $dst already exists"
        if [ -e "$src" ] && [ ! -L "$src" ]; then
            echo "WARNING: $src exists (not a symlink) alongside $dst; skipping to avoid overwrite." >&2
            continue
        fi
        if [ ! -e "$src" ]; then
            echo "Creating symlink: $src -> $dst"
            ln -s "$dst" "$src"
        fi
        continue
    fi

    # Rename directory and create symlink
    if [ -d "$src" ]; then
        echo "Renaming: $src -> $dst"
        mv "$src" "$dst"
        echo "Creating symlink: $src -> $dst"
        ln -s "$dst" "$src"
        echo "✓ Completed: $name"
    else
        echo "SKIP: $src does not exist"
    fi
    echo
done

echo "=== Verification ==="
for name in imports snapshots attachments; do
    src="$DATA_DIR_PATH/$name"
    if [ -L "$src" ]; then
        target="$(readlink "$src")"
        if [ -d "$target" ]; then
            echo "✓ $src -> $target (valid)"
        else
            echo "✗ $src -> $target (target missing!)" >&2
        fi
    else
        echo "✗ $src is not a symlink" >&2
    fi
done
