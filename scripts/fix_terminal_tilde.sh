#!/bin/bash
# Fix Terminal.app tilde rendering issue
# This script sets proper locale and encoding for Terminal

# Set locale to en_US.UTF-8 if not already set
if [[ -z "$LANG" || "$LANG" == "C" || "$LANG" == "C.UTF-8" ]]; then
    export LANG=en_US.UTF-8
    export LC_ALL=en_US.UTF-8
fi

# Ensure UTF-8 encoding
export LC_CTYPE=en_US.UTF-8

# Set proper terminal type
export TERM=xterm-256color

# Test tilde rendering
echo "Testing tilde character: ~"
echo "If you see 'f' above, there's still an encoding issue"
echo ""
echo "Current locale settings:"
locale | grep -E "LANG|LC_CTYPE|LC_ALL"

echo ""
echo "To make these changes permanent, add to ~/.zprofile:"
echo "export LANG=en_US.UTF-8"
echo "export LC_ALL=en_US.UTF-8"
echo "export LC_CTYPE=en_US.UTF-8"

