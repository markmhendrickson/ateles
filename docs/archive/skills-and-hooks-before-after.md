# Skills and Hooks: Before and After

## Before (All Rules Always-On)

**Context per agent session:** ~45 rules always loaded (~15,000+ lines of instructions)

**Manual processes:**
- Run `/setup_cursor_copies` after editing rule sources
- Update README manually when release status changes
- Remember to apply workflows for releases, bugs, features

**Enforcement:**
- Some checks automated (file naming, parquet access, protected paths via hardcoded list)
- Security checks partially implemented
- No automatic rule sync

---

## After (Skills + Hooks)

**Context per agent session:**
- **Base rules:** ~40 rules always loaded (~12,000 lines)
- **Skills:** 4 skills loaded on-demand (~3,000 lines when needed)
- **Net reduction:** ~700 lines moved to on-demand

**Automated processes:**
- Pre-commit hooks automatically:
  - Sync `.cursor/` when rule sources change
  - Update README when release status changes
  - Enforce security checks from config
- Agent skills automatically load when relevant

**Enforcement:**
- All security checks config-driven (`foundation-config.yaml`)
- Cursor rules sync automated (no manual `/setup_cursor_copies` needed)
- Release status → README automated

---

## Comparison Table

| Area | Before | After | Benefit |
|------|--------|-------|---------|
| **Release workflow** | Always-on rule (150 lines) | On-demand skill | Context only when needed |
| **Bug fix workflow** | Always-on rule (120 lines) | On-demand skill | Context only when needed |
| **Feature creation** | Always-on rule (130 lines) | On-demand skill | Context only when needed |
| **Email triage** | Always-on rule (300 lines) | On-demand skill | Context only when needed |
| **Security audit** | Hardcoded paths | Config-driven hook | Single source of truth |
| **Cursor sync** | Manual `/setup_cursor_copies` | Pre-commit hook | Automatic |
| **Release README** | Manual agent update | Pre-commit hook | Automatic |

---

## Usage Examples

### Using Skills

**Automatic application:**
```
User: "create a release for v1.0"
Agent: [loads create-release skill] "I see you're requesting a new release..."
```

**Manual invocation:**
```
User: /create-release
Agent: [loads create-release skill] "I'll help you create a release..."
```

### Using Hooks

**Security audit (automatic):**
```bash
# Edit a file in protected path
git add docs/private/sensitive.md
git commit -m "test"
# Hook blocks: "❌ SECURITY VIOLATION: Files in docs/private/ detected!"
```

**Cursor sync (automatic):**
```bash
# Edit a rule source
vim .cursor/rules/communication.mdc
git add .cursor/rules/communication.mdc
git commit -m "update communication rules"
# Hook runs: "✓ Rule/command source changes detected, running setup_cursor_copies..."
# Hook stages: .cursor/rules/communication_rules.mdc automatically
```

**Release status (automatic):**
```bash
# Update release status
vim docs/releases/v1.0.0/status.md
# Change: planning → in_progress
git add docs/releases/v1.0.0/status.md
git commit -m "start v1.0.0 development"
# Hook updates README.md Releases section automatically
```

---

## Configuration

All hooks read from `foundation-config.yaml`:

```yaml
security:
  pre_commit_audit:
    enabled: true
    protected_paths:
      - "$DATA_DIR/imports/"
      - "$DATA_DIR/attachments/"
      - "docs/private/"
      - ".env*"
    check_env_files: true
    check_data_directory: true

orchestration:
  release:
    enabled: true
    directory: "docs/releases/"
    status_file: "status.md"
```

---

## Benefits Summary

### For Agents
- **Smaller base context:** 700 lines moved to on-demand
- **Focused workflows:** Skills provide task-specific guidance
- **Less repetition:** Rules reference hooks/skills instead of duplicating implementation

### For Developers
- **Automated enforcement:** Hooks catch violations before commit
- **Faster feedback:** Violations detected immediately, not in review
- **Single source:** Config in `foundation-config.yaml` drives both rules and hooks

### For Repository
- **Consistency:** Rules, hooks, and skills all reference same config and workflows
- **Maintainability:** Update config once, hooks and rules follow
- **Discoverability:** Skills visible in Cursor Settings; hooks in `.pre-commit-config.yaml`

---

## Testing Checklist

- [ ] Install pre-commit: `pip install pre-commit && pre-commit install`
- [ ] Test security audit: Edit file in `data/` and try to commit
- [ ] Test cursor sync: Edit `.cursor/rules/communication.mdc` and commit
- [ ] Test release status: Create `docs/releases/v1.0.0/status.md` and commit
- [ ] Test skills: Type `/create-release` in Cursor chat
- [ ] Verify in Settings: Cursor Settings → Rules → Agent Decides (skills should appear)

---

## Troubleshooting

**"PyYAML not installed"**
```bash
pip install PyYAML
```

**"pre-commit command not found"**
```bash
pip install pre-commit
pre-commit install
```

**Skills not appearing in Cursor**
- Restart Cursor to pick up new `.cursor/skills/`
- Check Settings → Rules → Agent Decides

**Hook fails on commit**
- Read error message
- Fix violations or exclude files
- Retry commit

---

## Related Files

- `docs/skills-and-hooks-guide.md` — Quick reference guide
- `reports/rules-to-skills-and-hooks-implementation-2026-01-26.md` — Implementation report
- `SKILLS_AND_HOOKS_CHANGELOG.md` — Changelog
- `.pre-commit-config.yaml` — Hook configuration
- `foundation-config.yaml` — Config source of truth
