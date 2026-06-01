#!/bin/bash
# Disk space analysis script
# Identifies large directories and potential cleanup targets

set -e

echo "=== Disk Space Analysis ==="
echo "Note: Cursor files will only be analyzed, never deleted"
echo

# Check available tools
HAS_DUST=$(command -v dust &> /dev/null && echo "yes" || echo "no")
HAS_NCDU=$(command -v ncdu &> /dev/null && echo "yes" || echo "no")

if [ "$HAS_DUST" = "no" ]; then
    echo "⚠️  'dust' not installed. Install with: brew install dust"
    echo "   (falling back to 'du')"
    echo
fi

# Current disk usage
echo "Current Disk Usage:"
df -h ~ | head -2
echo

# Top-level directories
echo "=== Top-Level Directories ==="
if [ "$HAS_DUST" = "yes" ]; then
    dust -n 20 -d 2 ~
else
    echo "Using 'du' (slower than dust):"
    du -sh ~/* 2>/dev/null | sort -h | tail -20
fi
echo

# Cache directories
echo "=== Cache Directories ==="
echo "Homebrew cache:"
du -sh ~/Library/Caches/Homebrew 2>/dev/null || echo "  (not found)"

echo "Pip cache:"
du -sh ~/Library/Caches/pip 2>/dev/null || echo "  (not found)"

echo "npm cache:"
du -sh ~/.npm 2>/dev/null || echo "  (not found)"

echo "Cursor cache (ANALYSIS ONLY - DO NOT DELETE):"
du -sh ~/Library/Application\ Support/Cursor 2>/dev/null || echo "  (not found)"

echo "Docker:"
du -sh ~/Library/Containers/com.docker.docker/Data 2>/dev/null || echo "  (not found)"
echo

# Development artifacts
echo "=== Development Artifacts ==="
echo "Finding node_modules directories..."
find ~/Projects -name "node_modules" -type d -prune -exec du -sh {} \; 2>/dev/null | head -10 || echo "  (none found)"

echo "Finding Python venvs..."
find ~/Projects -name "venv" -o -name ".venv" -type d -prune -exec du -sh {} \; 2>/dev/null | head -10 || echo "  (none found)"

echo "Finding __pycache__ directories..."
PYCACHE_SIZE=$(find ~/Projects -name "__pycache__" -type d -exec du -sh {} \; 2>/dev/null | awk '{s+=$1}END{print s}' || echo "0")
echo "  Total: ${PYCACHE_SIZE}"
echo

# Project-specific
echo "=== Data Directory ==="
if [ -z "$DATA_DIR" ]; then
    echo "⚠️  DATA_DIR not set - skipping data directory analysis"
    echo "   Set DATA_DIR to analyze data directory usage"
else
    if [ -d "$DATA_DIR" ]; then
        echo "Snapshots:"
        du -sh "$DATA_DIR/snapshots" 2>/dev/null || echo "  (not found)"
        
        echo "Snapshots older than 90 days:"
        find "$DATA_DIR/snapshots" -name "*.parquet" -mtime +90 -exec du -sh {} \; 2>/dev/null | wc -l | xargs echo "  Count:"
        
        echo "Import archives:"
        du -sh "$DATA_DIR/imports" 2>/dev/null || echo "  (not found)"
        
        echo "Data directory total:"
        du -sh "$DATA_DIR" 2>/dev/null || echo "  (not found)"
    else
        echo "⚠️  DATA_DIR directory not found: $DATA_DIR"
    fi
fi
echo

# Recommendations
echo "=== Cleanup Recommendations ==="
echo "Run './scripts/disk_cleanup.sh' to safely remove:"
echo "  - System caches (Homebrew, pip, npm)"
echo "  - Python build artifacts (__pycache__, .pyc)"
echo "  - Old logs (>30 days)"
echo "  - Trash"
echo
echo "Manual review recommended for:"
echo "  - node_modules directories (can reinstall)"
echo "  - Python venvs (can recreate)"
echo "  - Old snapshots (>90 days)"
echo "  - Import archives (if fully normalized)"
echo "  - Downloads folder"
echo

echo "Next steps:"
echo "  1. Review this analysis"
echo "  2. Run: ./scripts/preview_deletions.sh (detailed deletion preview)"
echo "  3. Run: ./scripts/disk_cleanup.sh --dry-run (preview cleanup)"
echo "  4. Run: ./scripts/disk_cleanup.sh (execute with confirmation)"
echo
echo "For interactive analysis, install and use:"
if [ "$HAS_DUST" = "yes" ]; then
    echo "  dust -n 50 ~"
else
    echo "  brew install dust && dust -n 50 ~"
fi

if [ "$HAS_NCDU" = "yes" ]; then
    echo "  ncdu ~"
else
    echo "  brew install ncdu && ncdu ~"
fi

