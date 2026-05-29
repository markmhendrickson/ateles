# Skills and Hooks Quick Reference

This guide shows how workflow rules have been converted to Agent Skills (on-demand) and enforcement hooks (automated).

## Agent Skills

Skills are loaded when relevant or invoked via `/skill-name`.

### Available Skills

| Skill | Invocation | When to Use |
|-------|------------|-------------|
| `create-release` | `/create-release` | Creating software releases, planning releases, version numbering |
| `fix-feature-bug` | `/fix-feature-bug` | Fixing bugs, errors, broken features, test failures |
| `create-feature-unit` | `/create-feature-unit` | Creating new features, implementing functionality, feature planning |
| `email-triage` | Auto-applied | Processing inbox, email triage workflow, handling email queues |

**Location:** `.cursor/skills/`

**How to view:**
1. Open Cursor Settings (Cmd+Shift+J / Ctrl+Shift+J)
2. Navigate to **Rules**
3. Skills appear in **Agent Decides** section

### Benefits
- **Reduced context:** Skills load only when needed (~700 lines moved to on-demand)
- **Focused workflows:** Each skill provides specific task guidance
- **Command integration:** Skills work with existing `/` commands

---

## Pre-commit Hooks

Hooks enforce rules automatically at commit time.

### New Hooks (Added in 2026-01-26)

| Hook | Script | Purpose | Config |
|------|--------|---------|--------|
| `security-audit` | `scripts/linters/security_audit.py` | Protected paths, .env files, data/ directory | `foundation-config.yaml` `security.pre_commit_audit` |
| `cursor-rules-sync` | `scripts/linters/cursor_rules_sync_pre_commit.sh` | Auto-sync .cursor/ when rule sources change | Runs on `docs/**/*.mdc` and foundation rule sources |
| `release-status-readme` | `scripts/update_readme_release_status.py` | Update README when release status.md changes | Runs on `docs/releases/*/status.md` |

### Existing Hooks (Already Configured)

| Hook | Purpose |
|------|---------|
| `black` | Python code formatting |
| `ruff` | Python linting |
| `yamllint` | YAML linting |
| `shellcheck` | Shell script linting |
| `gitleaks` | Secrets detection |
| `check-parquet-access` | Enforce MCP-only parquet access |
| `check-file-naming` | File naming conventions |
| `check-documentation` | Documentation structure |
| `check-workflow-compliance` | Workflow file compliance |

**Configuration:** `.pre-commit-config.yaml`

### Usage

**Automatic (on commit):**
```bash
git commit -m "message"
# All hooks run automatically
```

**Manual execution:**
```bash
# Run all hooks
pre-commit run --all-files

# Run specific hook
pre-commit run security-audit --all-files
pre-commit run cursor-rules-sync --all-files
```

**Install:**
```bash
pip install pre-commit
pre-commit install
```

### Benefits
- **Automated enforcement:** Rules enforced mechanically at commit time
- **Config-driven:** Single source of truth in `foundation-config.yaml`
- **Fail-fast:** Violations caught before commit, not in review

---

## Rules Updates

Rules now reference hooks/skills instead of containing full implementation:

| Rule | Change |
|------|--------|
| `security.mdc` | Shortened to reference `scripts/linters/security_audit.py` |
| `release_status_readme_update.mdc` | Shortened to reference `scripts/update_readme_release_status.py` |
| `cursor_rules_sync.mdc` | New rule explaining pre-commit sync automation |
| `email_triage_protocol_rules.mdc` | Added note referencing `.cursor/skills/email-triage/` |

**Result:** Rules stay as policy/constraints; implementation lives in hooks/skills.

---

## Configuration

All hooks read from `foundation-config.yaml`:

**Security:**
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
```

**Release:**
```yaml
orchestration:
  release:
    enabled: true
    directory: "docs/releases/"
    status_file: "status.md"
```

---

## Testing

**Test security audit:**
```bash
python3 scripts/linters/security_audit.py
# Should pass if no protected files are staged
```

**Test cursor rules sync:**
```bash
# Edit a rule source (e.g., .cursor/rules/communication.mdc)
# Then:
bash scripts/linters/cursor_rules_sync_pre_commit.sh
# Should run setup_cursor_copies and stage .cursor/ changes
```

**Test release status update:**
```bash
python3 scripts/update_readme_release_status.py
# Reports no releases if docs/releases/ doesn't exist (expected)
```

**Test skills:**
- Type `/create-release` in Cursor chat
- Mention "triage inbox" and see if email-triage skill is applied
- Check Cursor Settings → Rules for skills list

---

## Troubleshooting

**Hook fails with "command not found: pre-commit"**
- Install: `pip install pre-commit && pre-commit install`

**security_audit.py fails with "PyYAML not installed"**
- Install: `pip install PyYAML`

**cursor_rules_sync hook can't find setup script**
- Verify: `ls foundation/scripts/setup_cursor_copies.sh`
- If missing, initialize submodule: `git submodule update --init`

**Skills not showing in Cursor**
- Restart Cursor to pick up new skills in `.cursor/skills/`
- Check Settings → Rules → Agent Decides section

---

## Related Documentation

- `reports/rules-to-skills-and-hooks-implementation-2026-01-26.md` — Full implementation report
- `.pre-commit-config.yaml` — Hook configuration
- `foundation-config.yaml` — Security and release configuration
- `.cursor/skills/` — Skills directory
- `foundation/agent_instructions/README.md` — Foundation documentation
