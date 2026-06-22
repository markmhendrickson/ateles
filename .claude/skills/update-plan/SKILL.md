---
name: update-plan
description: "Update a Neotoma plan entity to reflect current session work — todos, decisions, next_steps, body."
triggers:
  - update-plan
  - /update-plan
user_invocable: true
entity_id: ent_5d7f84290f290383e53d1a42
---

# update-plan

Update a Neotoma plan entity to accurately reflect what was built and decided this session.

**Always run this at the end of any session that makes progress against a plan.** Also invoke mid-session if a significant decision is settled that should not be lost to context exhaustion.

## Inputs

- `plan_entity_id`: the Neotoma entity ID of the plan **matching this session's workstream**. There is no global default — resolve it by `retrieve_entities(entity_type=plan, search=<workstream>)` and pick the closest match, or create a new plan if none fits. Only use `ent_99ace4dd6673aa36ed08b1fe` ("Ateles Agent Swarm Architecture") when the session's work is swarm-architecture itself; never funnel unrelated workstreams (tax, release engineering, website, cloud hosting) into it.
- Session context: what was built, what was decided, what is now blocked or unblocked

## Steps

### Step 1: Retrieve current plan snapshot

```
mcp__mcpsrv_neotoma__retrieve_entity_snapshot(entity_id=<plan_entity_id>, format="markdown")
```

### Step 2: Diff todos against reality

For each item in the `todos` array:
- Mark `"status": "done"` if completed this session or in prior sessions
- Mark `"status": "in_progress"` if actively being worked
- Leave `"status": "pending"` if not yet started
- Add a `"notes"` field on any item where implementation details matter (entity IDs, file paths, caveats, PR numbers)
- Add new todos for work identified this session that isn't already listed

### Step 3: Update decisions map

Add or correct entries for any decisions settled this session. Each entry is a key (snake_case decision name) → single-sentence value capturing the *what* and *why*. `correct` replaces the *entire* `decisions` map, so RE-READ the current map from Step 1, then write back the full map with only your keys added/updated and every pre-existing key preserved. Never delete existing entries, and never rebuild the map from a stale in-memory copy — that silently drops other sessions' decisions. The same re-read-and-merge rule applies to the `todos` array.

Key decisions to always capture if settled:
- Tool/library choices and why alternatives were rejected
- Renamed entities/daemons (with old → new and rationale)
- Deferred items (what was deferred and to which phase)
- "GHA-first" or "T4-first" evaluations for new agents

### Step 4: Correct next_steps

Replace with the current phase's specific blockers and the immediate next action. Be concrete — name files, entity IDs, PR numbers, or account names involved. Format as a short numbered list.

### Step 5: Fix body if stale

Check the body for:
- Daemon names that were renamed (e.g. Castor → neotoma-agent)
- Phase descriptions that no longer match reality
- Entity IDs or URLs that changed
- Outdated status indicators

Only correct fields that actually changed — skip fields that are already accurate.

### Step 6: Apply corrections

One `mcp__mcpsrv_neotoma__correct` call per changed field:

```
mcp__mcpsrv_neotoma__correct(
  entity_id=<plan_entity_id>,
  entity_type="plan",
  field=<field_name>,
  idempotency_key="update-plan-<field>-<YYYY-MM-DD>",
  value=<corrected_value>
)
```

Fields to consider: `todos`, `decisions`, `next_steps`, `body`, `summary`, `overview`.

## Output

Confirm which fields were corrected and summarise the key changes in a short bullet list.

## Notes

- This skill writes to Neotoma prod (`mcp__mcpsrv_neotoma__*`), never the dev instance.
- The `todos` field is an array of objects — always send the full updated array, not a partial patch.
- The `decisions` field is a JSON object (key → string) — send the full updated object.
- After running this skill, run `/update-tasks` to ensure task entities reflect the updated todos.
