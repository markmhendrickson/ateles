#!/bin/bash
# Preview potential deletions with detailed breakdown
# Shows what could be safely deleted without actually deleting

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== Disk Space Deletion Preview ==="
echo "Note: This is a read-only analysis. No files will be deleted."
echo "⚠️  Cursor files are never included in deletion candidates"
echo
df -h ~ | head -2
echo

# Calculate total recoverable space
TOTAL_RECOVERABLE=0

echo "=== Safe to Delete (Reproducible) ==="
echo

# System caches
echo "1. SYSTEM CACHES"
echo "   Risk: None - All reproducible"
echo

if [ -d ~/Library/Caches/Homebrew ]; then
    BREW_SIZE=$(du -sk ~/Library/Caches/Homebrew 2>/dev/null | awk '{print $1}')
    BREW_SIZE_MB=$(echo "$BREW_SIZE / 1024" | bc)
    TOTAL_RECOVERABLE=$((TOTAL_RECOVERABLE + BREW_SIZE))
    echo "   Homebrew cache: ${BREW_SIZE_MB} MB"
    echo "   ~/Library/Caches/Homebrew"
fi

if [ -d ~/Library/Caches/pip ]; then
    PIP_SIZE=$(du -sk ~/Library/Caches/pip 2>/dev/null | awk '{print $1}')
    PIP_SIZE_MB=$(echo "$PIP_SIZE / 1024" | bc)
    TOTAL_RECOVERABLE=$((TOTAL_RECOVERABLE + PIP_SIZE))
    echo "   Pip cache: ${PIP_SIZE_MB} MB"
    echo "   ~/Library/Caches/pip"
fi

if [ -d ~/.npm ]; then
    NPM_SIZE=$(du -sk ~/.npm 2>/dev/null | awk '{print $1}')
    NPM_SIZE_MB=$(echo "$NPM_SIZE / 1024" | bc)
    TOTAL_RECOVERABLE=$((TOTAL_RECOVERABLE + NPM_SIZE))
    echo "   npm cache: ${NPM_SIZE_MB} MB"
    echo "   ~/.npm"
fi
echo

# Development dependencies
echo "2. DEVELOPMENT DEPENDENCIES"
echo "   Risk: None - Can reinstall/recreate"
echo

NODE_MODULES=$(find ~/Projects -name "node_modules" -type d -prune 2>/dev/null)
if [ -n "$NODE_MODULES" ]; then
    echo "   node_modules directories:"
    echo "$NODE_MODULES" | while read dir; do
        SIZE=$(du -sk "$dir" 2>/dev/null | awk '{print $1}')
        SIZE_MB=$(echo "$SIZE / 1024" | bc)
        TOTAL_RECOVERABLE=$((TOTAL_RECOVERABLE + SIZE))
        echo "   - ${SIZE_MB} MB: $dir"
    done | head -10
    TOTAL_NODE_MODULES=$(echo "$NODE_MODULES" | wc -l | xargs)
    if [ "$TOTAL_NODE_MODULES" -gt 10 ]; then
        echo "   ... and $((TOTAL_NODE_MODULES - 10)) more"
    fi
fi

VENVS=$(find ~/Projects -name "venv" -o -name ".venv" -type d -prune 2>/dev/null)
if [ -n "$VENVS" ]; then
    echo
    echo "   Python venvs:"
    echo "$VENVS" | while read dir; do
        SIZE=$(du -sk "$dir" 2>/dev/null | awk '{print $1}')
        SIZE_MB=$(echo "$SIZE / 1024" | bc)
        echo "   - ${SIZE_MB} MB: $dir"
    done | head -10
fi
echo

# Build artifacts
echo "3. BUILD ARTIFACTS"
echo "   Risk: None - Regenerated on build"
echo

PYCACHE_SIZE=$(find ~/Projects -name "__pycache__" -type d -exec du -sk {} \; 2>/dev/null | awk '{s+=$1}END{print int(s/1024)" MB"}')
PYCACHE_COUNT=$(find ~/Projects -name "__pycache__" -type d 2>/dev/null | wc -l | xargs)
echo "   __pycache__: ${PYCACHE_SIZE} (${PYCACHE_COUNT} directories)"

PYC_SIZE=$(find ~/Projects -name "*.pyc" -exec du -sk {} \; 2>/dev/null | awk '{s+=$1}END{print int(s/1024)" MB"}')
PYC_COUNT=$(find ~/Projects -name "*.pyc" 2>/dev/null | wc -l | xargs)
echo "   .pyc files: ${PYC_SIZE} (${PYC_COUNT} files)"
echo

# Old logs
echo "4. OLD LOGS"
echo "   Risk: Low - Logs older than 30 days"
echo

OLD_LOGS=$(find ~/Library/Logs -name "*.log" -mtime +30 2>/dev/null)
if [ -n "$OLD_LOGS" ]; then
    OLD_LOGS_SIZE=$(echo "$OLD_LOGS" | xargs du -sk 2>/dev/null | awk '{s+=$1}END{print int(s/1024)" MB"}')
    OLD_LOGS_COUNT=$(echo "$OLD_LOGS" | wc -l | xargs)
    echo "   Logs >30 days: ${OLD_LOGS_SIZE} (${OLD_LOGS_COUNT} files)"
fi
echo

# Trash
echo "5. TRASH"
echo "   Risk: Medium - Review before emptying"
echo

TRASH_SIZE=$(du -sh ~/.Trash 2>/dev/null | awk '{print $1}' || echo "0")
TRASH_COUNT=$(find ~/.Trash -type f 2>/dev/null | wc -l | xargs)
echo "   Trash: ${TRASH_SIZE} (${TRASH_COUNT} items)"
echo

echo "=== Review Before Deleting ==="
echo

# Old snapshots
echo "6. OLD SNAPSHOTS (>90 days)"
echo "   Risk: Low if recent snapshots exist"
echo

if [ -z "$DATA_DIR" ]; then
    echo "   ⚠️  DATA_DIR not set - skipping snapshot analysis"
else
    if [ -d "$DATA_DIR/snapshots" ]; then
        OLD_SNAPSHOTS=$(find "$DATA_DIR/snapshots" -name "*.parquet" -mtime +90 2>/dev/null)
        if [ -n "$OLD_SNAPSHOTS" ]; then
            OLD_SNAP_SIZE=$(echo "$OLD_SNAPSHOTS" | xargs du -sk 2>/dev/null | awk '{s+=$1}END{print int(s/1024)" MB"}')
            OLD_SNAP_COUNT=$(echo "$OLD_SNAPSHOTS" | wc -l | xargs)
            echo "   Snapshots >90 days: ${OLD_SNAP_SIZE} (${OLD_SNAP_COUNT} files)"
            echo "   ⚠️  Verify recent snapshots exist first"
        else
            echo "   (none found)"
        fi
    else
        echo "   ⚠️  DATA_DIR/snapshots not found: $DATA_DIR/snapshots"
    fi
fi
echo

# Import archives
echo "7. IMPORT ARCHIVES"
echo "   Risk: Medium - Verify normalization complete"
echo

if [ -z "$DATA_DIR" ]; then
    echo "   ⚠️  DATA_DIR not set - skipping import archive analysis"
else
    if [ -d "$DATA_DIR/imports" ]; then
        IMPORTED_SIZE=$(du -sh "$DATA_DIR/imports" 2>/dev/null | awk '{print $1}' || echo "0")
        echo "   Import archives: ${IMPORTED_SIZE}"
        echo "   $DATA_DIR/imports"
        echo "   ⚠️  Verify data is normalized before deleting"
    else
        echo "   ⚠️  DATA_DIR/imports not found: $DATA_DIR/imports"
    fi
fi
echo

# Old downloads
echo "8. OLD DOWNLOADS (>90 days)"
echo "   Risk: Medium - May contain needed files"
echo

OLD_DOWNLOADS=$(find ~/Downloads -type f -mtime +90 2>/dev/null)
if [ -n "$OLD_DOWNLOADS" ]; then
    OLD_DL_SIZE=$(echo "$OLD_DOWNLOADS" | xargs du -sk 2>/dev/null | awk '{s+=$1}END{print int(s/1024)" MB"}')
    OLD_DL_COUNT=$(echo "$OLD_DOWNLOADS" | wc -l | xargs)
    echo "   Downloads >90 days: ${OLD_DL_SIZE} (${OLD_DL_COUNT} files)"
    echo "   ⚠️  Review individual files first"
fi
echo

echo "=== Summary ==="
TOTAL_RECOVERABLE_GB=$(echo "scale=2; $TOTAL_RECOVERABLE / 1024 / 1024" | bc)
echo "Estimated safe recoverable space: ${TOTAL_RECOVERABLE_GB} GB"
echo "✓ Cursor files excluded from all recommendations"
echo
echo "Next steps:"
echo "  1. Review this report"
echo "  2. Run: ./scripts/disk_cleanup.sh --dry-run (preview automated cleanup)"
echo "  3. Run: ./scripts/disk_cleanup.sh (execute with confirmation)"
echo "  4. Manually review old snapshots, imports, downloads"









