#!/usr/bin/env bash
# Link project Cursor skills (`.cursor/skills/<name>/`) into Claude Code's
# `.claude/skills/<name>/` so the same SKILL.md trees are available in both agents.
#
# Preserves real directories already under `.claude/skills/` (e.g. loop-start,
# loop-status, loop-stop) and never replaces them.
#
# Usage: from repo root — ./scripts/setup_claude_skills.sh [--force-symlink]
#
# --force-symlink: replace an existing *symlink* at the destination even if it
#                  pointed elsewhere; still never deletes a real directory.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

CURSOR_SKILLS="${REPO_ROOT}/.cursor/skills"
CLAUDE_SKILLS="${REPO_ROOT}/.claude/skills"

# Names that must stay as whatever is already in .claude (non-symlink trees).
readonly PRESERVE_DIRS=(loop-start loop-status loop-stop)

force_symlink=0
if [[ "${1:-}" == "--force-symlink" ]]; then
  force_symlink=1
fi

if [[ ! -d "$CURSOR_SKILLS" ]]; then
  echo "[ERROR] Missing Cursor skills dir: $CURSOR_SKILLS" >&2
  exit 1
fi

mkdir -p "$CLAUDE_SKILLS"

is_preserved() {
  local n="$1"
  local p
  for p in "${PRESERVE_DIRS[@]}"; do
    [[ "$n" == "$p" ]] && return 0
  done
  return 1
}

linked=0
skipped=0

for skill_dir in "$CURSOR_SKILLS"/*/; do
  [[ -d "$skill_dir" ]] || continue
  name="$(basename "$skill_dir")"
  if [[ ! -f "${skill_dir}SKILL.md" ]]; then
    echo "[WARN] Skip (no SKILL.md): $name"
    ((skipped += 1)) || true
    continue
  fi

  if is_preserved "$name"; then
    echo "[INFO] Preserve Claude-native skill dir: $name"
    ((skipped += 1)) || true
    continue
  fi

  dest="${CLAUDE_SKILLS}/${name}"
  rel_target="../../.cursor/skills/${name}"

  if [[ -e "$dest" || -L "$dest" ]]; then
    if [[ -d "$dest" && ! -L "$dest" ]]; then
      echo "[WARN] Skip — destination is a real directory (not a symlink): $dest"
      ((skipped += 1)) || true
      continue
    fi
    if [[ -L "$dest" ]] && [[ "$force_symlink" -eq 0 ]]; then
      current="$(readlink "$dest" || true)"
      if [[ "$current" == "$rel_target" ]]; then
        echo "[OK] Already linked: $name"
        ((skipped += 1)) || true
        continue
      fi
    fi
  fi

  ln -sfn "$rel_target" "$dest"
  echo "[OK] Linked .claude/skills/$name -> $rel_target"
  ((linked += 1)) || true
done

echo "[INFO] Done. Linked: $linked, skipped/unchanged: $skipped"
