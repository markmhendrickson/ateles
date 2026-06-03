# Dotfiles Sync via Git Repo

This directory contains shell configuration files that are synced across all your devices via git (this personal repo).

## Current Files

- `.zshrc` - Zsh shell configuration (prompt, aliases, functions, etc.)
- `setup_symlinks.sh` - Setup script for new devices
- `README.md` - This file

## Setup on New Device

1. Clone the personal repo (or pull latest changes)
2. Run the setup script from the repo root:
   ```bash
   cd ~/repos/ateles  # or wherever you cloned the repo
   ./dotfiles/setup_symlinks.sh
   ```
3. The script will:
   - Create symlinks from your home directory to files in the repo
   - Backup any existing files (as `.backup`)
   - Skip if symlinks already exist

## Manual Setup

If you prefer to set up manually:

```bash
# Backup existing file (if any)
mv ~/.zshrc ~/.zshrc.backup

# Create symlink (adjust path to your repo location)
ln -s ~/repos/ateles/dotfiles/.zshrc ~/.zshrc
```

## Adding More Dotfiles

To sync additional dotfiles:

1. Copy the file to this directory:
   ```bash
   cp ~/.somefile ~/repos/ateles/dotfiles/
   ```

2. Create a symlink:
   ```bash
   ln -s ~/repos/ateles/dotfiles/.somefile ~/.somefile
   ```

3. Update `setup_symlinks.sh` to include the new file

4. Commit to the repo:
   ```bash
   git add dotfiles/.somefile
   git commit -m "Add .somefile to dotfiles"
   git push
   ```

## Notes

- Changes to files in this directory should be committed to git to sync across devices
- After making changes, reload your shell: `source ~/.zshrc`
- The symlink approach ensures files are always in sync with the repo
- Keep device-specific settings in separate files if needed
- The setup script automatically detects the repo location, so it works regardless of where you cloned it

## Benefits of Repo-Based Approach

- Version control: Track changes with git history
- Branching: Test changes in branches before applying
- Pull requests: Review changes before merging
- Backup: Git provides natural backup via remote
- Collaboration: Easy to share with others if needed