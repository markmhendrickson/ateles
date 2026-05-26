---
name: learn
description: "Convert issues raised in chat into durable automation by creating or updating rules, skills, and hooks in the correct repository."
triggers:
  - learn
  - /learn
  - why didn't you
  - not following instructions
  - strengthen behavior
user_invocable: true
entity_id: ent_8577b3807b0247c01628666d
---

# Learn

Turn a behavior miss raised in chat into a permanent process improvement.

Use this skill when the user points out an omission, asks why something was not done, or requests stronger automatic agentic behavior.

## Goals

1. Fix the immediate omission in the current turn.
2. Prevent recurrence by updating the smallest durable artifact (rule, skill, or hook).
3. **Apply the new guidance immediately in the given chat context**—perform the newly required behavior in this conversation, not only document it.

## Inputs

- `issue_statement` (required): What behavior was missed.
- `scope_hint` (optional): `repo`, `local_mcp`, or specific path.
- `target_behavior` (optional): Desired default behavior going forward.

## Workflow

1. **Capture the failure clearly**
   - Restate: expected behavior, actual behavior, and impact.
   - Confirm whether the missed action was not explicitly requested earlier in the turn flow (retrospective improvement case).

2. **Select the right remediation artifact**
   - **Rule update** when behavior should be globally or repeatedly enforced.
   - **Skill update/new skill** when behavior is a multi-step workflow.
   - **Hook/script update** when behavior must be automatically enforced by tooling (for example, pre-commit checks or setup hooks).
   - **MCP source-repo instruction update** when omission is due to MCP instruction quality or ordering.

3. **Choose the repository to change**
   - **Current repo (`ateles`)** for local agent workflow defaults, repo rules, and repo skills.
   - **Relevant local MCP source repo** when the issue is MCP-instruction behavior (for example Neotoma instruction text in sibling repo).
   - **Priority rule:** If the miss concerns Neotoma interactions, default to improving Neotoma's own instruction source first; use `ateles` only for non-MCP workflow behavior.
   - Prefer the smallest repository scope that fixes the issue for future runs.
   - **Never add or modify ateles rules (or skills/hooks) to compensate for behavior that should be enforced by MCP server instructions.** If the root cause is MCP instruction quality or ordering, fix the MCP source repo only; do not duplicate or compensate in this repo.

4. **Implement minimal durable changes**
   - Add explicit trigger language (what should cause the behavior).
   - Add explicit ordering language for required steps (for example, "first action every turn").
   - Add forbidden-pattern language for known failure mode (for example, "responding before storage is complete is forbidden").
   - Keep MCP instruction updates generalized and reusable; avoid adding data-domain-specific logic (for example rules tied to one roster, one account list, or one dataset).
   - Keep changes tight; avoid broad rewrites unless needed.

5. **Validate the fix**
   - Re-run the same scenario path that failed (or nearest reproducible path).
   - If this is MCP-related, retry the MCP usage path after the fix.
   - Verify the updated rule/skill/hook can be discovered by the agent.

6. **Apply learnings immediately in this chat (required)**
   - **MANDATORY:** In the same turn (or the next user-visible action), perform the behavior that the new guidance requires. Examples: if you added store-first, you must store this turn first; if you added a rule to do X after Y, do X when Y applies in this conversation.
   - Consider /learn incomplete until you have both (a) updated the durable artifact and (b) applied the new behavior in the given chat context.
   - Do not finish /learn by only reporting what was changed; you must also demonstrate or perform the new behavior in this chat.

7. **Report outcome**
   - What was changed.
   - Why this artifact was chosen.
   - What behavior is now enforced.
   - Any follow-up needed.

## Decision Guide

- If the issue is **single-product, process-wide, recurring** -> create/update a **rule** (in ateles only when the behavior is not MCP-instruction-driven).
- If the issue is **workflow logic with branching steps** -> create/update a **skill**.
- If the issue needs **tooling-level enforcement** -> create/update a **hook/script**.
- If the issue is **MCP instruction quality/order** -> update **only the MCP source-repo instructions**. Do not add or modify rules in this repo to compensate.
- If the issue is **Neotoma interaction behavior** -> prioritize updating `neotoma` instructions with generalized guidance, not case-specific data modeling.

## Neotoma-Specific Pattern

When the issue is "agent did not follow Neotoma store-first turn behavior" (or any behavior defined by MCP server use instructions):

1. Store the current turn first before other actions.
2. Improve durable guidance **in the Neotoma (MCP) source repo only**: update the MCP instruction block via the dedicated Neotoma learn process. Do not add or modify ateles rules to compensate for weak or missing MCP instructions.
3. Re-test by executing the same turn pattern and confirm the behavior is followed.

4. **Hybrid (user files on disk only):** When the miss is “user-provided files or blobs exist only on the host (moved/copied locally) but were **not** ingested into Neotoma’s unstructured path,” add or tighten a **type-agnostic** Neotoma MCP instruction (same-turn `file_path` / `file_content` + Attachment recipe; structured inferred entities are additive). For **host-only** workflow details (e.g. finance `registry_id` tables, copy scripts), update **ateles** rules or pipeline docs—**do not** use ateles to replace MCP store ordering.

5. **Session-derived entities without graph links:** When any entity stored from the current chat (separate `store` from the user-phase batch) was left without `REFERS_TO` the conversation or the originating `agent_message`, fix the graph in the same turn (`create_relationship`) and add generalized wording to `../neotoma/docs/developer/mcp/instructions.md` (first fenced block) via the neotoma-learn path; do not duplicate the rule in ateles.

## Constraints

- **Always apply learnings immediately to the given chat context.** Completing /learn by only updating an artifact and reporting, without performing the new behavior in this conversation, is forbidden.
- **Do not add or modify rules (or skills/hooks) in this repo to compensate for behavior that should be enforced by MCP server instructions.** When the root cause is MCP instruction quality or ordering, change only the MCP source repo.
- Do not create duplicate guidance in multiple places unless each location has a distinct enforcement role.
- Do not edit generated copies when a source-of-truth document/workflow exists.
- Do not perform destructive operations without explicit user approval.
- Do not commit changes unless user explicitly asks.
