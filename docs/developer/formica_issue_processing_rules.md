# Formica issue processing rules

<context>
Formica should reuse the Neotoma `/process-issues` workflow when the selected repository already exposes that skill. This keeps issue handling aligned with Neotoma's canonical issue-processing flow instead of relying on an ad hoc prompt.
</context>

## Scope

Applies to Formica agents spawned to handle Neotoma-tracked issues in any workspace that exposes the `process-issues` skill.

## Purpose

Define how Formica should shape spawned-agent prompts for issue work when the target workspace includes Neotoma's `process-issues` skill.

## Trigger patterns

<requirements>
When Formica prepares an agent prompt for an issue and the selected workspace contains either `.cursor/skills/process-issues/SKILL.md` or `.claude/skills/process_issues/SKILL.md`, Formica MUST instruct the spawned agent to use `/process-issues`.
</requirements>

## Agent actions

### Step 1: Detect the skill

1. Check the selected workspace for the Cursor or Claude `process-issues` skill path.
2. Treat either path as evidence that the repository exposes the Neotoma issue-processing workflow.

### Step 2: Shape the prompt

1. Tell the spawned agent to use `/process-issues` as the primary workflow for the issue.
2. Scope the run to the current issue entity only.
3. Preserve the issue title, body, and classifier context in the prompt.

### Step 3: Fall back safely

1. If the skill is absent, use the generic Formica issue-processing prompt.
2. Do not block issue handling when the skill is unavailable.

## Constraints

<constraints>
- Formica MUST prefer `/process-issues` when the selected workspace exposes that skill.
- Formica MUST scope the workflow to the current issue instead of triaging unrelated queue items.
- Formica MUST fall back to the generic prompt only when the skill is unavailable.
</constraints>

## Quick reference

- Domain: Formica issue-processing workflow
- Sequence: Detect skill -> shape prompt -> scope to current issue -> fall back if missing
- Required: Target workspace path, current issue entity id, issue content, classifier result
- Outputs: Spawned-agent prompt aligned with Neotoma's `/process-issues` workflow
- Constraints: Prefer the skill when available; never widen scope to unrelated issues
