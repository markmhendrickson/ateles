---
name: create-feature-unit
description: "Create a new feature unit with spec, manifest, and test structure. Use when user mentions creating features, new features, implementing features, or feature unit IDs. Can be invoked via /create-feature-unit."
triggers:
  - create feature
  - new feature
  - add feature
  - implement feature
  - feature unit
  - feature unit ID
  - create-feature-unit
user_invocable: true
entity_id: ent_a26ffa85303847a29ee4e3d3
---

# Create Feature Unit

Create a structured feature unit with specification, manifest, and test scaffolds.

## When to Use

Use this skill when:
- User mentions "create feature", "new feature", "add feature", "implement feature"
- User references "feature unit" or configured abbreviation
- Feature Unit IDs mentioned (matching configured pattern in `foundation-config.yaml`)
- Feature descriptions implying new work (e.g., "we need to add X", "implement Y")
- Planning discussions about new functionality
- User explicitly invokes `/create-feature-unit`

## Workflow

### Step 1: Detection and Confirmation

1. **Load configuration:**
   - Read `foundation-config.yaml` for feature unit settings
   - Extract `feature_units.directory` and `feature_units.id_pattern`
   - Check if `feature_units.enabled` is true

2. **Extract feature information** from context:
   - Feature Unit ID (if mentioned, validate against configured pattern)
   - Feature name or description
   - Priority hints (P0/P1/P2/P3)
   - Scope hints (what functionality is mentioned)

3. **Check if feature unit already exists:**
   - Look in `{configured_directory}/completed/{feature_id}/`
   - Look in `{configured_directory}/in_progress/{feature_id}/`
   - Check repository-specific FU inventory (if configured)
   
   If exists → inform user and ask: "Do you want to modify it or create a new one? (modify/new)"

4. **If feature unit does NOT exist:**
   - Ask for confirmation:
     ```
     I see you're requesting a new Feature Unit. Should I create a Feature Unit spec 
     in `{configured_directory}/in_progress/{feature_id}/` following the Feature Unit 
     creation workflow? (yes/no)
     ```

5. **If feature ID not detected:**
   - Show configured ID pattern from config
   - Ask: "What Feature Unit ID should I use? (format: {configured_pattern}, e.g., FU-2025-01-001)"
   - Wait for user input before proceeding

6. **Handle exceptions:**
   - If user says "no" → do not proceed
   - If user says "just a spec document" → create spec only

### Step 2: Execute Feature Unit Creation Workflow

If user confirms (yes) and feature_id is provided:

1. **Load required documents:**
   - `foundation/development/feature_unit_workflow.md` — primary workflow
   - `foundation/agent_instructions/cursor_commands/create_feature_unit.md` — command
   - `foundation/development/templates/feature_unit_spec_template.md` — spec template
   - `foundation/development/templates/manifest_template_simple.yaml` or `manifest_template_extended.yaml` — manifest template
   - Repository-specific FU inventory (if configured)

2. **Follow Feature Unit creation workflow:**
   - Start at Checkpoint 0: Spec Creation
   - Check if spec exists (completed or in_progress)
   - If spec exists: validate completeness, proceed if complete
   - If spec does NOT exist: prompt user interactively for all required spec details
   - Generate complete spec and manifest from user input
   - **Validate dependencies**: REJECT if dependencies not implemented (if validation enabled)

3. **Create file structure:**
   ```
   {configured_directory}/in_progress/{feature_id}/
   ├── {feature_id}_spec.md
   └── manifest.yaml
   
   tests/unit/features/{feature_id}/
   tests/integration/features/{feature_id}/
   tests/e2e/features/{feature_id}/  (if e2e tests required)
   tests/regression/features/{feature_id}/
   ```
   
   Do NOT implement code yet — only scaffolds.

4. **Alignment check (spec vs mental model):**
   - Produce concise summary:
     - Problem it solves and why it exists
     - What is in scope and out of scope
     - Which modules/subsystems it touches
     - Critical invariants or constraints
   - Ask user:
     - "Does this accurately capture what you want this Feature Unit to do? (yes/no)"
     - "What feels off, missing, or over-scoped compared to your intent?"
   - Incorporate corrections and re-summarize if substantial
   - Do NOT proceed until user confirms spec matches their mental model

## Constraints

- **MUST** ask for user confirmation before creating feature unit
- **MUST** require explicit feature_id before proceeding
- **MUST** validate dependencies if validation enabled in config
- **MUST** get user approval on spec alignment before proceeding
- **MUST** create only spec and manifest at Checkpoint 0 (no code implementation yet)
- **MUST NOT** skip the feature unit workflow
- **MUST NOT** create incomplete specs

## Integration

- This skill mirrors the `/create-feature-unit` command
- Both automatic detection (via this skill) and explicit invocation (`/create-feature-unit`) lead to the same workflow
- References foundation feature unit workflow documents for complete process
