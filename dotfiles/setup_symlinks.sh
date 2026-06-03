#!/bin/bash
# Setup script to create symlinks for dotfiles from the personal repo
# Run this on new devices to sync your shell configuration
# 
# Usage: ./dotfiles/setup_symlinks.sh
# Or from repo root: ./dotfiles/setup_symlinks.sh

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DOTFILES="$SCRIPT_DIR"
HOME_DIR="$HOME"

echo "Setting up dotfiles symlinks from personal repo..."

# Check if dotfiles directory exists
if [[ ! -d "$REPO_DOTFILES" ]]; then
    echo "Error: Dotfiles directory not found at: $REPO_DOTFILES"
    echo "Make sure you're running this from the personal repo."
    exit 1
fi

# Function to create symlink if file doesn't exist or is not already a symlink
create_symlink() {
    local source="$REPO_DOTFILES/$1"
    local target="$HOME_DIR/$1"
    
    if [[ ! -f "$source" ]]; then
        echo "Warning: Source file $source does not exist, skipping..."
        return
    fi
    
    if [[ -L "$target" ]]; then
        echo "$1 is already a symlink, skipping..."
        return
    fi
    
    if [[ -f "$target" ]]; then
        echo "Backing up existing $1 to ${1}.backup"
        mv "$target" "${target}.backup"
    fi
    
    ln -s "$source" "$target"
    echo "Created symlink: $target -> $source"
}

# Create symlinks for dotfiles
create_symlink ".zshrc"
create_symlink ".zprofile"

echo ""
echo "Setup complete! Your dotfiles are now symlinked from the personal repo."
echo "To apply changes, run: source ~/.zshrc"
echo ""
echo "Note: Changes to dotfiles should be committed to the repo to sync across devices."

