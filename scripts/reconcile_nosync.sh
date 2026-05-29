#!/bin/bash
# Reconcile .nosync directories by removing suffix and merging with existing directories

set -e

DATA_DIR="$HOME/Documents/data"

echo "=== Finding .nosync directories ==="
cd "$DATA_DIR"

# Find all .nosync directories
NOSYNC_DIRS=$(find . -maxdepth 1 -type d -name "*.nosync" | sed 's|^\./||')

if [ -z "$NOSYNC_DIRS" ]; then
    echo "No .nosync directories found."
    exit 0
fi

echo "Found .nosync directories:"
for dir in $NOSYNC_DIRS; do
    echo "  - $dir"
done

echo ""
echo "=== Processing each directory ==="

for nosync_dir in $NOSYNC_DIRS; do
    # Get target name (remove .nosync suffix)
    target_dir="${nosync_dir%.nosync}"
    
    echo ""
    echo "=== Processing: $nosync_dir -> $target_dir ==="
    
    if [ -d "$target_dir" ]; then
        echo "Target directory exists - merging contents..."
        
        # Merge: copy items from .nosync to target
        # Use rsync to merge directories (preserves structure, skips existing files)
        echo "Merging contents (skipping existing files)..."
        rsync -av --ignore-existing "$nosync_dir/" "$target_dir/"
        
        echo "Merge complete. Removing .nosync directory..."
        rm -rf "$nosync_dir"
        echo "✓ Removed $nosync_dir"
    else
        echo "Target directory doesn't exist - renaming..."
        mv "$nosync_dir" "$target_dir"
        echo "✓ Renamed to $target_dir"
    fi
done

echo ""
echo "=== Reconciliation complete ==="
