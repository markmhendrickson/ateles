# Foundation vs Ateles Rules

## Overview

Foundation rules are kept **generic and reusable** across all repos. Ateles-specific automation (hooks) is provided via **repository-specific overrides** in `docs/`.

## Foundation Rules (Generic)

Foundation rules in `foundation/agent_instructions/cursor_rules/` describe **what agents should do manually**:

- **`security.mdc`** — Agents perform security checks manually before commits
- **`release_status_readme_update.mdc`** — Agents update README manually when release status changes

These rules work for any repository using foundation, regardless of whether hooks are configured.

## Ateles Overrides (Hook-Based)

Ateles-specific rules in `docs/` **override foundation rules** to provide automated enforcement:

- **`docs/security_audit_hook_rules.mdc`** — Overrides foundation security rule
  - References: `scripts/linters/security_audit.py` hook
  - Says: "Hook enforces this automatically"

- **`docs/release_status_readme_hook_rules.mdc`** — Overrides foundation release status rule
  - References: `scripts/update_readme_release_status.py` hook
  - Says: "Hook updates README automatically"

- **`docs/cursor_rules_sync_hook_rules.mdc`** — Ateles-specific (not in foundation)
  - References: `scripts/linters/cursor_rules_sync_pre_commit.sh` hook
  - Says: "Hook syncs .cursor/ automatically"

## How It Works

**Cursor rule precedence:**
1. Repository rules in `docs/` (ateles overrides) — highest priority
2. Foundation rules in `foundation/agent_instructions/cursor_rules/` — fallback

**Result:**
- Other repos using foundation get generic rules (agents do X manually)
- Ateles gets hook-based automation (hooks do X automatically)
- Foundation remains reusable and generic

## Adding Hooks to Other Repos

If another repo wants hook-based automation:

1. **Copy ateles hook scripts** to that repo's `scripts/linters/`
2. **Copy ateles override rules** from `docs/*_hook_rules.mdc` to that repo's `docs/`
3. **Configure hooks** in that repo's `.pre-commit-config.yaml`
4. **Update config** in that repo's `foundation-config.yaml`

Foundation rules remain unchanged and generic.

## Benefits

- **Foundation stays generic** — reusable across repos
- **Ateles gets automation** — hooks enforce rules automatically
- **Clear separation** — foundation = policy, ateles = implementation
- **Easy to extend** — other repos can copy ateles hooks/overrides if desired
