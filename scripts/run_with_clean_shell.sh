#!/bin/bash
# Wrapper script to run commands with a clean shell environment
# Bypasses Cursor's problematic shell initialization

# Use a minimal shell environment
env -i \
    HOME="$HOME" \
    USER="$USER" \
    PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    SHELL="/bin/bash" \
    /bin/bash -c "$@"
