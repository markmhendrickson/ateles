#!/bin/bash
# Setup script to verify/install 1Password CLI
# Part of repository initialization

set -e

echo "Checking for 1Password CLI..."

# Check if op command exists
if command -v op &> /dev/null; then
    echo "✓ 1Password CLI is installed"
    op --version
else
    echo "✗ 1Password CLI not found"
    echo ""
    echo "Installing 1Password CLI..."
    
    # Detect OS
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        if command -v brew &> /dev/null; then
            echo "Installing via Homebrew..."
            brew install --cask 1password-cli
        else
            echo "ERROR: Homebrew not found. Please install Homebrew first:"
            echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
            exit 1
        fi
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        # Linux
        echo "Please install 1Password CLI manually:"
        echo "  See: https://developer.1password.com/docs/cli/get-started#install"
        exit 1
    else
        echo "ERROR: Unsupported OS. Please install 1Password CLI manually:"
        echo "  See: https://developer.1password.com/docs/cli/get-started#install"
        exit 1
    fi
fi

echo ""
echo "Verifying 1Password CLI authentication..."

# Check if signed in
if op account list &> /dev/null; then
    echo "✓ 1Password CLI is authenticated"
    op account list
else
    echo "⚠ 1Password CLI is not signed in"
    echo ""
    echo "To sign in, run:"
    echo "  eval \$(op signin)"
    echo ""
    echo "Or for a specific account:"
    echo "  eval \$(op signin <account-shorthand>)"
    echo ""
    echo "See: https://developer.1password.com/docs/cli/get-started#sign-in"
fi

echo ""
echo "1Password CLI setup complete!"

