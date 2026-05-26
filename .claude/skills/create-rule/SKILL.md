---
name: create-rule
description: Create Cursor rule for persistent AI guidance.
triggers:
  - create rule
  - /create_rule
  - create-rule
user_invocable: true
entity_id: ent_95ae5bf804f8ab2d7b9f9dbc
---

## Agent Instructions

### When to Load This Document

Load this document when:

- {Trigger condition 1}
- {Trigger condition 2}

### Required Co-Loaded Documents

- {Required document 1}
- {Required document 2}

### Constraints Agents Must Enforce

1. {Constraint to enforce 1}
2. {Constraint to enforce 2}

### Forbidden Patterns

- {Anti-pattern 1}
- {Anti-pattern 2}

### Validation Checklist

- [ ] {Validation item 1}
- [ ] {Validation item 2}
```

**Submodule mode:**

For foundation submodule, create file following foundation cursor-rules pattern (simpler structure without Agent Instructions section since these are loaded automatically).

### Step 4: Run Setup Script

**Repository mode:**

1. Run foundation setup script:
   ```bash
   ./foundation/scripts/setup-cursor-rules.sh
   ```

2. Verify symlink created in `.cursor/rules/`:
   - Expected symlink name: `{location}_{name}_rules.md`
   - Example: `docs/conventions/entity_resolution_rules.md` → `.cursor/rules/conventions_entity_resolution_rules.md`

3. Output success message:
   ```
   ✅ Rule created successfully!
   
   File: docs/{location}/{name}_rules.md
   Symlink: .cursor/rules/{location}_{name}_rules.md
   
   The rule is now available to all Cursor agents in this repository.
   
   Next steps:
   1. Review the generated rule file and fill in any remaining details
   2. Test the rule by triggering its conditions
   3. Update related documentation to reference this rule if needed
   ```

**Submodule mode:**

1. If submodule is `foundation`, run setup script from main repository:
   ```bash
   ./foundation/scripts/setup-cursor-rules.sh
   ```

2. Verify symlink created (for foundation rules, they're prefixed with `foundation-`)

3. Output success message:
   ```
   ✅ Rule created in submodule successfully!
   
   File: {submodule}/{rule_path}/{name}{suffix}.mdc (for foundation submodule, use .mdc extension)
   Symlink: .cursor/rules/{prefix}{name}.mdc (if applicable)
   
   The rule is now available to all repositories using this submodule.
   ```

## Error Handling

- If submodule not found: Exit with error message
- If location directory doesn't exist in repo mode: Offer to create it or exit
- If file already exists: Warn and ask to overwrite or exit
- If setup script fails: Report error but keep the created file

## Configuration

Optional configuration in `foundation-config.yaml`:

```yaml
agent_instructions:
  rules:
    default_location: "docs/conventions"  # Default location for new rules
    template_path: null  # Custom template path (optional)
    auto_run_setup: true  # Automatically run setup script after creation
```

## Examples

### Example 1: Create Repository Rule

```
/create-rule
```

Prompts:
- Rule file name: `entity_resolution`
- Location: `subsystems`
- Purpose: "Ensure consistent entity resolution patterns across codebase"
- Key constraints: "MUST use deterministic merging, MUST NOT introduce randomness"

Creates:
- `docs/subsystems/entity_resolution_rules.md`
- `.cursor/rules/subsystems_entity_resolution_rules.md` (symlink)

### Example 2: Create Foundation Submodule Rule

```
/create-rule foundation
```

Prompts:
- Rule file name: `testing_patterns`
- Purpose: "Enforce consistent testing patterns"
- Key constraints: "MUST include unit tests, SHOULD use fixtures"

Creates:
- `foundation/agent_instructions/cursor_rules/testing_patterns.mdc`
- `.cursor/rules/foundation_testing_patterns.mdc` (symlink)

## Required Documents

Load before starting:

- `foundation/scripts/setup-cursor-rules.sh` (setup script)
- `docs/conventions/documentation_standards_rules.md` (if creating repo rule)
- `foundation-config.yaml` (configuration)
