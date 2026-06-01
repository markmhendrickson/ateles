#!/bin/bash
# Check for protected path violations in staged files

# Protected paths from foundation-config.yaml
PROTECTED_PATHS="imports/|attachments/|docs/private/|\.env"

# Get staged files
STAGED_FILES=$(git diff --cached --name-only)

if [ -z "$STAGED_FILES" ]; then
    exit 0
fi

# Check for protected paths
VIOLATIONS=$(echo "$STAGED_FILES" | grep -E "$PROTECTED_PATHS")

if [ -n "$VIOLATIONS" ]; then
    echo "ERROR: Attempting to commit protected paths:" >&2
    echo "$VIOLATIONS" | while read -r file; do
        echo "  - $file" >&2
    done
    echo "" >&2
    echo "Protected paths:" >&2
    echo "  - \$DATA_DIR/imports/ (read-only archive)" >&2
    echo "  - \$DATA_DIR/attachments/ (large files)" >&2
    echo "  - docs/private/ (private submodule)" >&2
    echo "  - .env* files" >&2
    echo "" >&2
    echo "See foundation-config.yaml security section for details." >&2
    exit 1
fi

exit 0

