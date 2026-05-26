---
name: create-release
description: "Create a new software release with planning, manifest, and execution schedule. Use when the user mentions creating a release, planning a release, \\"new release\\", version numbers like \\"v1.0.0\\", or release-related planning. Can be invoked via /create-release."
triggers:
  - new release
  - create release
  - plan release
  - v1.0.0
  - v2.0.0
  - split out features
  - release scope
  - /create-release
user_invocable: true
entity_id: ent_dfd053b6614b858e61519d89
---

# Create Release

Plan and create a new software release following the release workflow.

## When to Use

Use this skill when:
- User mentions "new release", "create release", "plan release"
- User references version numbers in release context (e.g., "v1.0.0", "v2.0.0")
- User asks to "split out features" into a release
- Planning discussions about release scope and timeline
- User explicitly invokes `/create-release`

## Workflow

### Step 1: Detect and Confirm

1. **Identify release intent** from user context:
   - Extract release ID/version if mentioned (e.g., "v1.0.0")
   - Note release type hints (internal/external, marketed/not_marketed)
   - Capture scope hints (what features or functionality)

2. **Load configuration:**
   - Read `foundation-config.yaml` for release settings
   - Check if `orchestration.release.enabled` is true
   - Determine `orchestration.release.directory` path

3. **Ask for confirmation:**
   ```
   I see you're requesting a new release. Should I create a release plan in 
   `{configured_releases_directory}/in_progress/{release_id}/` following the release workflow? (yes/no)
   ```
   
   If no release ID detected, ask: "What release ID/version should I use?"

4. **Handle exceptions:**
   - If user says "just a spec document" → create spec only, not full release
   - If user says "no" → do not proceed

### Step 2: Execute Release Workflow

If user confirms (yes):

1. **Load required foundation documents:**
   - `foundation/development/release_workflow.md` — primary workflow
   - `foundation/agent_instructions/cursor_commands/create_release.md` — command implementation
   - `foundation/development/feature_unit_workflow.md` — if feature units enabled
   - Repository-specific execution instructions (if they exist)

2. **Create release structure** in `{configured_directory}/in_progress/{release_id}/`:
   - `release_plan.md` — release goals, scope, out of scope
   - `manifest.yaml` — batches, checkpoints, dependencies
   - `execution_schedule.md` — ordered execution plan
   - `status.md` — current status, batch tracking
   - `integration_tests.md` — if configured
   - `discovery_plan.yaml` — if discovery enabled in config
   - `participant_recruitment_log.md` — if discovery enabled

3. **Follow release workflow checkpoints:**
   - Start at Checkpoint 0: Release Planning
   - Validate dependencies before generating execution schedule (if feature units enabled)
   - Get user approval at configured checkpoints
   - Do NOT create standalone spec documents for releases

## Constraints

- **MUST** ask for user confirmation before creating release plans
- **MUST** follow `foundation/development/release_workflow.md` process
- **MUST** validate dependencies if feature units are enabled
- **MUST** get user approval at configured checkpoints
- **MUST NOT** skip the release workflow
- **MUST NOT** create releases outside configured releases directory

## Integration

- This skill mirrors the `/create-release` command
- Both automatic detection (via this skill) and explicit invocation (`/create-release`) lead to the same workflow
- References foundation release workflow documents for complete process
