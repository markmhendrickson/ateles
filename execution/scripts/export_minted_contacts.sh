#!/bin/bash
# Helper script to export Minted contacts
# Usage: ./export_minted_contacts.sh [email] [password]
#   or: export minted_email=... minted_password=... && ./export_minted_contacts.sh

cd "$(dirname "$0")/.."

# Check if credentials provided as arguments
if [ $# -eq 2 ]; then
    export minted_email="$1"
    export minted_password="$2"
fi

# Check if credentials are set
if [ -z "$minted_email" ] || [ -z "$minted_password" ]; then
    echo "Minted credentials required."
    echo ""
    echo "Option 1: Set environment variables:"
    echo "  export minted_email='your@email.com'"
    echo "  export minted_password='yourpassword'"
    echo "  ./scripts/export_minted_contacts.sh"
    echo ""
    echo "Option 2: Provide as arguments:"
    echo "  ./scripts/export_minted_contacts.sh your@email.com yourpassword"
    exit 1
fi

# Run the export script
echo "Exporting contacts from Minted..."
python3 scripts/export_minted_contacts.py "$minted_email" "$minted_password"








