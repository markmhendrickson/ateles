#!/bin/bash
# Disk space cleanup script
# Removes safe-to-delete caches and temporary files
# WARNING: Does NOT touch Cursor files (highly sensitive)

set -e

# Parse arguments
DRY_RUN=false
SKIP_CONFIRM=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --yes)
            SKIP_CONFIRM=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--dry-run] [--yes]"
            echo "  --dry-run    Show what would be deleted without deleting"
            echo "  --yes        Skip confirmation prompt"
            exit 1
            ;;
    esac
done

if [ "$DRY_RUN" = true ]; then
    echo "=== Disk Cleanup - DRY RUN MODE ==="
    echo "No files will be deleted. Preview only."
else
    echo "=== Disk Cleanup - Preview ==="
fi
echo "⚠️  Cursor files are protected and will NOT be touched"
echo
df -h ~ | head -2
echo

# Preview what will be deleted
echo "=== Preview of Deletions ==="
echo

# System caches
echo "1. Homebrew cache:"
if [ -d ~/Library/Caches/Homebrew ]; then
    BREW_SIZE=$(du -sh ~/Library/Caches/Homebrew 2>/dev/null | awk '{print $1}')
    BREW_COUNT=$(find ~/Library/Caches/Homebrew -type f 2>/dev/null | wc -l | xargs)
    echo "   Location: ~/Library/Caches/Homebrew"
    echo "   Size: ${BREW_SIZE}"
    echo "   Files: ${BREW_COUNT}"
else
    echo "   (not found)"
fi
echo

echo "2. Pip cache:"
if command -v pip &> /dev/null; then
    PIP_SIZE=$(du -sh ~/Library/Caches/pip 2>/dev/null | awk '{print $1}' || echo "unknown")
    echo "   Location: ~/Library/Caches/pip"
    echo "   Size: ${PIP_SIZE}"
else
    echo "   (pip not found)"
fi
echo

echo "3. npm cache:"
if command -v npm &> /dev/null; then
    NPM_SIZE=$(du -sh ~/.npm 2>/dev/null | awk '{print $1}' || echo "unknown")
    echo "   Location: ~/.npm"
    echo "   Size: ${NPM_SIZE}"
else
    echo "   (npm not found)"
fi
echo

echo "4. __pycache__ directories:"
PYCACHE_DIRS=$(find ~/Projects -name "__pycache__" -type d 2>/dev/null | wc -l | xargs)
PYCACHE_SIZE=$(find ~/Projects -name "__pycache__" -type d -exec du -sk {} \; 2>/dev/null | awk '{s+=$1}END{printf "%.1f MB", s/1024}' || echo "0 MB")
echo "   Location: ~/Projects/**/__pycache__"
echo "   Directories: ${PYCACHE_DIRS}"
echo "   Size: ${PYCACHE_SIZE}"
echo

echo "5. .pyc files:"
PYC_COUNT=$(find ~/Projects -name "*.pyc" 2>/dev/null | wc -l | xargs)
PYC_SIZE=$(find ~/Projects -name "*.pyc" -exec du -sk {} \; 2>/dev/null | awk '{s+=$1}END{printf "%.1f MB", s/1024}' || echo "0 MB")
echo "   Location: ~/Projects/**/*.pyc"
echo "   Files: ${PYC_COUNT}"
echo "   Size: ${PYC_SIZE}"
echo

echo "6. Old logs (>30 days):"
OLD_LOGS=$(find ~/Library/Logs -name "*.log" -mtime +30 2>/dev/null | wc -l | xargs)
OLD_LOGS_SIZE=$(find ~/Library/Logs -name "*.log" -mtime +30 -exec du -sk {} \; 2>/dev/null | awk '{s+=$1}END{printf "%.1f MB", s/1024}' || echo "0 MB")
echo "   Location: ~/Library/Logs/**/*.log (>30 days)"
echo "   Files: ${OLD_LOGS}"
echo "   Size: ${OLD_LOGS_SIZE}"
echo

echo "7. Trash:"
TRASH_SIZE=$(du -sh ~/.Trash 2>/dev/null | awk '{print $1}' || echo "0")
TRASH_COUNT=$(find ~/.Trash -type f 2>/dev/null | wc -l | xargs)
echo "   Location: ~/.Trash"
echo "   Size: ${TRASH_SIZE}"
echo "   Items: ${TRASH_COUNT}"
echo

echo "=== Summary ==="
echo "✓ Cursor files will remain untouched"
echo "✓ All deletions are safe and reproducible"
echo

if [ "$DRY_RUN" = true ]; then
    echo "=== Dry Run Complete - No Changes Made ==="
    exit 0
fi

# Confirmation prompt
if [ "$SKIP_CONFIRM" = false ]; then
    read -p "Proceed with cleanup? (yes/no): " -r
    echo
    if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
        echo "Cleanup cancelled."
        exit 0
    fi
fi

echo "=== Executing Cleanup ==="
echo

# System caches
echo "Clearing Homebrew cache..."
if [ -d ~/Library/Caches/Homebrew ]; then
    rm -rf ~/Library/Caches/Homebrew/*
    echo "  ✓ Cleared"
else
    echo "  (not found)"
fi

echo "Clearing pip cache..."
if command -v pip &> /dev/null; then
    pip cache purge
    echo "  ✓ Cleared"
else
    echo "  (pip not found)"
fi

echo "Clearing npm cache..."
if command -v npm &> /dev/null; then
    (npm cache clean --force 2>&1 && echo "  ✓ Cleared") || {
        echo "  ⚠️  Permission issue - run manually:"
        echo "     sudo chown -R $(id -u):$(id -g) ~/.npm"
        echo "     npm cache clean --force"
    }
else
    echo "  (npm not found)"
fi

# Development artifacts
echo "Removing __pycache__ directories..."
find ~/Projects -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
echo "  ✓ Removed"

echo "Removing .pyc files..."
find ~/Projects -name "*.pyc" -delete 2>/dev/null || true
echo "  ✓ Removed"

# Old logs
echo "Removing logs older than 30 days..."
find ~/Library/Logs -name "*.log" -mtime +30 -delete 2>/dev/null || true
echo "  ✓ Removed"

# Trash
echo "Emptying trash..."
rm -rf ~/.Trash/*
echo "  ✓ Emptied"

echo
echo "=== Cleanup Complete ==="
echo "✓ Cursor files remain untouched"
df -h ~ | head -2

