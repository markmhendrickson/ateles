---
name: update-tasks
description: "Create or update Neotoma task entities linked PART_OF a plan, reflecting current pending work."
triggers:
  - update-tasks
  - /update-tasks
user_invocable: true
entity_id: ent_c21f9fb84691f43f45e6cd55
---

# update-tasks

Create or update Neotoma task entities that represent actionable work items, linked `PART_OF` a plan. Keeps the task graph in sync with the plan's todos so Apis can eventually consume them as `task.created` events.

**Run after `/update-plan`** whenever todos change significantly, or when task statuses need updating.

## Inputs

- `plan_entity_id`: the Neotoma entity ID of the plan **matching this session's workstream** (the same plan `/update-plan` bound to). There is no global default — resolve it by workstream; only use `ent_99ace4dd6673aa36ed08b1fe` for swarm-architecture work itself.
- Updated todos list (from `/update-plan` output or plan snapshot)

## Steps

### Step 1: Retrieve existing task relationships

```
mcp__mcpsrv_neotoma__list_relationships(
  target_entity_id=<plan_entity_id>,
  relationship_type="PART_OF"
)
```

Then for each related entity, check `entity_type=task` to get the existing task IDs and names.

### Step 2: Identify gaps

Compare the plan's `todos` (status=pending or in_progress) against existing task entities. Note which todos have no corresponding task entity.

Focus on **actionable, near-term items** — typically the current phase and one phase ahead. Don't create task entities for Phase 7+ items until Phase 5 is in progress.

### Step 3: Create missing task entities

```
mcp__mcpsrv_neotoma__store(
  idempotency_key="task-<slug>-<YYYY-MM-DD>",
  entities=[{
    "entity_type": "task",
    "canonical_name": "<concise task name>",
    "title": "<same as canonical_name>",
    "description": "<expand on the todo — include file paths, entity IDs, acceptance criteria, implementation notes>",
    "status": "pending",  // or "in_progress" if actively being worked
    "priority": "<see priority mapping below>",
    "phase": "<plan phase number as string>",
    "tags": ["ateles", "phase-N", "<daemon-name-if-relevant>"],
    "action_type": "<see action_type vocabulary below>",
    "confidence": <0.0–1.0, see below>,
    "confidence_drivers": "<one or two sentences: what you retrieved, what is still unverified>"
  }],
  relationships=[{
    "relationship_type": "PART_OF",
    "source_index": 0,
    "target_entity_id": "<plan_entity_id>"
  }]
)
```

**`action_type` — what the task DOES** (feeds the execution gate's blast-radius
classification; ateles#682). Pick the most consequential action the task will
actually take, not the mildest one it starts with. A task that analyses
something and then opens a PR is `open_or_merge_pr`.

| Low blast (may auto-execute at high confidence) | High blast (always checkpoints) |
|---|---|
| `local_edit` — edits confined to a working tree | `git_push` — pushes to a remote |
| `draft` — stages a draft that a human sends | `open_or_merge_pr` — opens or merges a PR |
| `neotoma_read` — retrieval only | `payment` — moves money |
| `neotoma_internal_entity_update` — bookkeeping writes | `send_external_comms` — sends to someone outside the swarm |
| `compute_only_analysis` — produces a report, writes nothing | `delete_entity_or_data` — destroys data |
| | `external_api_write` — writes to a third-party API |
| | `publish` — makes something public |

Omitting `action_type` is safe but lossy: the gate falls back to inferring it
from your title and description, and failing that to a per-agent default. Say
what you mean and the classification stops being a guess.

**`confidence` — how sure you are, 0.0–1.0.** This is a self-score about *this
task's specification*, not about your general competence. Score honestly; the
gate's whole value is that a low score stops work that should stop.

- **0.85+** (the default auto-execute threshold): you retrieved the relevant
  entities, every required input is present, and the acceptance criteria are
  unambiguous.
- **0.5–0.85**: the work is understood but something material is unverified —
  a value you inferred, a file you have not read, a decision not yet made.
- **below 0.5**: you are proposing work, not specifying it.

Never inflate a score to get past the gate. If you cannot honestly assert
0.85, the correct outcome is a checkpoint, and `confidence_drivers` is where
you tell the operator exactly what would raise it.

**Priority mapping:**
- Phase 2 blocker items → `"blocker"`
- Phase 2 non-blocker / Phase 3 items → `"p1"` or `"p2"`
- Phase 4+ items → `"p2"` or `"p3"`

### Step 4: Update completed tasks

For todos just marked done, update the corresponding task entity:

```
mcp__mcpsrv_neotoma__correct(
  entity_id=<task_entity_id>,
  entity_type="task",
  field="status",
  idempotency_key="task-done-<slug>-<YYYY-MM-DD>",
  value="done"
)
```

### Step 5: Confirm relationships

All new task entities should have a `PART_OF` relationship to the plan. Verify via the `relationships` block in the `store` response, or create explicitly:

```
mcp__mcpsrv_neotoma__create_relationships(
  relationships=[{
    "relationship_type": "PART_OF",
    "source_entity_id": <task_entity_id>,
    "target_entity_id": <plan_entity_id>
  }]
)
```

## Output

List the task entities created or updated with their entity IDs, and confirm all are linked `PART_OF` the plan.

## Notes

- This skill writes to Neotoma prod (`mcp__mcpsrv_neotoma__*`), never the dev instance.
- Use `strict: false` in store calls — the task schema uses `title` as the canonical name field; duplicate prevention is handled by idempotency keys.
- When Apis is live (Phase 3+), these task entities become the primary dispatch signal via `task.created` SSE events — keep descriptions actionable and self-contained.
- Current Phase 2 task entities already created:
  - `ent_55649174d7dd2951fb90bf6d` — Configure 4 Neotoma mirror profiles
  - `ent_1765472ab3536042959f63df` — Create ateles-agent GitHub machine account
  - `ent_1273cd3a4d06a34364fb517d` — Create neotoma-agent GitHub account
  - `ent_5d6202c08d794ac1b7798b20` — OpenClaw workspace-neotoma.ts PR
  - `ent_8a18a26009bb932f0feb411e` — Anthus daemon skeleton
  - `ent_2482ad3ee9ec3c69d911c4fa` — Tyto daemon skeleton
