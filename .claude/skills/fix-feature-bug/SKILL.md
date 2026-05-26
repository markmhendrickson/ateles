---
name: fix-feature-bug
description: "Fix bugs using structured workflow with error classification and regression tests. Use when user reports bugs, errors, broken features, test failures, or mentions \"fix\", \"broken\", \"not working\", \"failing\". Can be invoked via /fix-feature-bug."
triggers:
  - bug
  - error
  - fix
  - broken
  - not working
  - failing
  - issue
  - problem
  - regression
  - broken feature
  - /fix-feature-bug
user_invocable: true
entity_id: ent_060e97e4b2d18d37526591f1
---

# Fix Feature Bug

Systematically fix bugs using error classification and regression testing.

## When to Use

Use this skill when:
- User mentions "bug", "error", "fix", "broken", "not working", "failing"
- User reports "issue" or "problem" in context of code/functionality
- Error messages or stack traces are present
- Test failures mentioned
- User mentions "regression" or "broken feature"
- User explicitly invokes `/fix-feature-bug`

## Workflow

### Step 1: Detection and Confirmation

1. **Extract bug information** from context:
   - Error messages or stack traces (if provided)
   - Feature/module ID (if mentioned or inferred from paths)
   - Description of the problem
   - Steps to reproduce (if mentioned)

2. **Identify affected feature/module:**
   - Check error messages for file paths
   - Check context for feature mentions
   - Check open files for feature references
   - If cannot identify → ask: "Which feature/module is affected? (identifier or 'unknown')"

3. **Ask for confirmation:**
   ```
   I see you're reporting a bug. Should I fix this using the bug fix workflow? 
   Feature/Module: {identifier} (or 'unknown' if not identified)
   (yes/no)
   ```

4. **Handle exceptions:**
   - If user says "no" → do not proceed
   - If user says "just document it" → document the bug instead of fixing

### Step 2: Execute Bug Fix Workflow

If user confirms (yes):

1. **Load configuration:**
   - Read `foundation-config.yaml` for bug fix workflow settings
   - Check if `development.bug_fix.error_classification.enabled` is true
   - Determine feature unit directory (if feature units enabled)

2. **Load required documents:**
   - `foundation/agent_instructions/cursor_commands/fix_feature_bug.md` — workflow
   - Repository navigation guide (if configured)
   - Feature spec and manifest (if feature units enabled and identifier provided)
   - Error classification documentation (if configured)
   - Relevant subsystem/module docs (if configured)

3. **Classify bug** (if error classification configured):
   - **Class 1:** Implementation bug (spec correct, code wrong) → Patch code only
   - **Class 2:** Spec bug (code correct, spec wrong) → Update spec first, then align code
   - **Class 3:** Architectural bug (architecture docs wrong) → Update architecture docs first
   
   If classification not configured, skip to fixing.

4. **Apply correction:**
   - **Class 1:** Fix code to match spec
   - **Class 2:** Update spec/manifest, then align code and tests
   - **Class 3:** Update architecture docs, then rebuild implementation
   
5. **Add regression test** (MANDATORY):
   - Always add a test that would have caught this bug
   - Test must fail with the bug present and pass with the fix

6. **Run tests:**
   - Execute test suite to verify fix
   - Ensure regression test passes

7. **Output:**
   - Error class (if classification used)
   - Reason for classification
   - Files corrected
   - Tests added/updated
   - Test results

## Constraints

- **MUST** ask for user confirmation before fixing
- **MUST** add a regression test for every fix
- **MUST** run tests after fix
- **MUST** classify bug if error classification is configured
- **MUST** update spec/manifest for Class 2 bugs before changing code
- **MUST** update architecture docs for Class 3 bugs before changing code
- **MUST NOT** skip the bug fix workflow
- **MUST NOT** fix without adding tests

## Integration

- This skill mirrors the `/fix-feature-bug` command
- Both automatic detection (via this skill) and explicit invocation (`/fix-feature-bug`) lead to the same workflow
- References foundation bug fix workflow documents for complete process
