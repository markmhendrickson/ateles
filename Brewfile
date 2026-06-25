# Ateles external CLI prerequisites (W0 — installability epic, issue #18).
#
# Brew-installable tools needed to run the swarm locally on macOS. Python
# dependencies are declared in pyproject.toml (`pip install -e .`); this Brewfile
# covers only the external binaries the daemons and agents shell out to.
#
#   brew bundle --file=Brewfile

brew "python@3.13"   # daemon runtime (.venv)
brew "node"          # Node v22+ for the `claude` CLI and github_harness MCP
brew "gh"            # GitHub CLI
brew "1password-cli" # `op` — secret materialization fallback

# Not available via Homebrew — install separately:
#   claude CLI : the official Claude Code installer (or `npm i -g` per Anthropic docs)
#   gws CLI    : operator Google Workspace bridge (Calendar / Gmail)
