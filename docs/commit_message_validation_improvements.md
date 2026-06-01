# Commit Message Validation Improvements

## Problem Analysis

**Issue:** Commit message incorrectly claimed "Add WhatsApp MCP server" when files were actually modified (M), not added (A).

**Root Cause:** The commit workflow analyzed file paths and content but didn't verify git status codes (A/M/D) before making claims about whether features were "added" vs "modified" vs "refactored".

## Recommended Improvements

### 1. Add File Status Validation Step

**Location:** `foundation/agent_instructions/cursor_commands/foundation_commit.md` - "COMPREHENSIVE CHANGE ANALYSIS" section

**Add explicit validation:**

```bash
# Step 1: Get file status codes (A/M/D/R/C)
git diff --cached --name-status > /tmp/staged_status.txt
git diff HEAD --name-status > /tmp/unstaged_status.txt

# Step 2: Categorize by status
ADDED_FILES=$(grep "^A" /tmp/staged_status.txt /tmp/unstaged_status.txt 2>/dev/null | cut -f2)
MODIFIED_FILES=$(grep "^M" /tmp/staged_status.txt /tmp/unstaged_status.txt 2>/dev/null | cut -f2)
DELETED_FILES=$(grep "^D" /tmp/staged_status.txt /tmp/unstaged_status.txt 2>/dev/null | cut -f2)

# Step 3: Verify claims in commit message
# - If commit message says "Add X", verify files are status A
# - If commit message says "Refactor X", verify files are status M
# - If commit message says "Remove X", verify files are status D
```

### 2. Add Pre-Commit Message Validation

**Add validation step before final commit:**

```bash
# Validate commit message accuracy
validate_commit_message() {
  COMMIT_MSG="$1"

  # Extract claimed actions from commit message
  if echo "$COMMIT_MSG" | grep -qi "add.*server\|new.*server\|implement.*server"; then
    # Verify files are actually added (status A)
    if ! echo "$ADDED_FILES" | grep -q "server"; then
      echo "⚠️  WARNING: Commit message claims 'add server' but no files with status A found"
      echo "   Consider: 'Refactor server' or 'Modify server' instead"
      return 1
    fi
  fi

  if echo "$COMMIT_MSG" | grep -qi "refactor\|modify\|update\|improve"; then
    # Verify files are actually modified (status M)
    if ! echo "$MODIFIED_FILES" | grep -q .; then
      echo "⚠️  WARNING: Commit message claims modification but no files with status M found"
      return 1
    fi
  fi
}
```

### 3. Enhance Change Analysis Section

**Update "COMPREHENSIVE CHANGE ANALYSIS" to explicitly check file status:**

```markdown
### Step 1: Categorize Changes by Status (REQUIRED)

**MANDATORY:** Before analyzing content, categorize files by git status:

1. **Run git diff with status codes:**
   ```bash
   git diff --cached --name-status  # Staged changes
   git diff HEAD --name-status      # All changes
   ```

2. **Group files by status:**
   - **Added (A)**: New files - use "Add", "Implement", "Create"
   - **Modified (M)**: Existing files - use "Refactor", "Modify", "Update", "Improve"
   - **Deleted (D)**: Removed files - use "Remove", "Delete"
   - **Renamed (R)**: Moved files - use "Move", "Rename", "Reorganize"

3. **Validate language matches status:**
   - Never say "Add X" for files with status M
   - Never say "Refactor X" for files with status A
   - Match commit message verbs to actual git status
```

### 4. Add Post-Generation Validation

**Add validation after commit message generation:**

```bash
# After generating commit message, validate it
validate_generated_message() {
  MSG_FILE="$1"

  # Check for common misstatements
  if grep -qi "add.*mcp server" "$MSG_FILE"; then
    # Verify WhatsApp files are actually added
    if ! echo "$ADDED_FILES" | grep -q "whatsapp.*server"; then
      echo "❌ ERROR: Commit message claims 'Add WhatsApp MCP server'"
      echo "   But files show status M (modified), not A (added)"
      echo "   Correct to: 'Refactor WhatsApp MCP server'"
      return 1
    fi
  fi
}
```

### 5. Configuration Option

**Add to `foundation-config.yaml`:**

```yaml
development:
  commit:
    validate_message_accuracy: true  # Validate commit message matches git status
    require_status_verification: true  # Require explicit status check before claims
    validation_strictness: "warn"  # "warn" or "error" (block commit if invalid)
```

## Implementation Priority

1. **High Priority:** Add file status categorization step (Step 1 above)
2. **Medium Priority:** Add pre-commit message validation (Step 2)
3. **Low Priority:** Add configuration options (Step 5)

## Example Corrected Workflow

**Before (incorrect):**
```bash
# Saw WhatsApp files → assumed "added"
echo "Add WhatsApp MCP server"
```

**After (correct):**
```bash
# Check status first
git diff --cached --name-status | grep whatsapp
# Output: M  mcp/whatsapp/whatsapp_mcp_server.py

# Status is M (modified) → use correct verb
echo "Refactor WhatsApp MCP server"
```

## Testing

Test cases to validate:
1. New file (A) → message says "Add" ✅
2. Modified file (M) → message says "Add" ❌ (should say "Refactor"/"Modify")
3. Deleted file (D) → message says "Remove" ✅
4. Mixed (A+M) → message accurately describes both ✅

