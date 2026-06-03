# Set proper terminal type to fix character rendering issues (tilde showing as 'f')
# Force override if TERM is still 'dumb' (some terminals set it after .zprofile)
[[ "$TERM" == "dumb" ]] && export TERM=xterm-256color

# Force UTF-8 locale (Terminal may set C.UTF-8 which can cause character rendering issues)
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export LC_CTYPE=en_US.UTF-8

# pnpm
export PNPM_HOME="/Users/markmhendrickson/Library/pnpm"
case ":$PATH:" in
  *":$PNPM_HOME:"*) ;;
  *) export PATH="$PNPM_HOME:$PATH" ;;
esac
# pnpm end
alias icloud='cd ~/Library/Mobile\ Documents/com~apple~CloudDocs'

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"  # This loads nvm
[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"  # This loads nvm bash_completion
# Silently activate virtual environment if it exists in current directory
# This runs automatically when changing directories
autoload -U add-zsh-hook
activate_venv() {
  local current_dir=$(pwd)
  local venv_activate="$current_dir/.venv/bin/activate"
  
  # If we're in a directory with .venv, activate it (or reactivate if different)
  if [[ -f "$venv_activate" ]]; then
    if [[ "$VIRTUAL_ENV" != "$current_dir/.venv" ]]; then
      # Deactivate current venv if different
      [[ -n "$VIRTUAL_ENV" ]] && deactivate >/dev/null 2>&1
      # Activate the new venv silently
      source "$venv_activate" >/dev/null 2>&1
    fi
  elif [[ -n "$VIRTUAL_ENV" ]]; then
    # No .venv in current directory, deactivate if one is active
    deactivate >/dev/null 2>&1
  fi
}
add-zsh-hook chpwd activate_venv
# Activate on shell startup if in a directory with .venv
activate_venv

# Set prompt: show hostname only if not on local system
# Show username only if not the default system user (markmhendrickson)
# Using explicit prompt format to avoid Terminal.app rendering issues
set_prompt() {
  local host=$(hostname -s)
  local user=$(whoami)
  
  # Clear any existing prompt sequences
  unset RPROMPT
  unset RPROMPT2
  
  if [[ "$host" == "mini" || "$host" == "Marks-Mac-mini" ]]; then
    # Local system: no hostname
    if [[ "$user" != "markmhendrickson" ]]; then
      # Non-default user: show username
      PS1=$'%n@ %1~ %# '
    else
      # Default user: no username, no hostname
      PS1=$'%1~ %# '
    fi
  else
    # Remote system: show hostname
    if [[ "$user" != "markmhendrickson" ]]; then
      # Non-default user: show username and hostname
      PS1=$'%n@%m %1~ %# '
    else
      # Default user: show hostname only
      PS1=$'%m %1~ %# '
    fi
  fi
}
set_prompt
