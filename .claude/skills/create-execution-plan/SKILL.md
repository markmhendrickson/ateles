---
name: create-execution-plan
description: "Create a task execution plan with correct schema and task linkage. Use when user says \"create execution plan\", \"add project plan\", or \"create plan for [project]\". Can be invoked via /create-execution-plan."
triggers:
  - create execution plan
  - add project plan
  - create plan for
  - create-execution-plan
user_invocable: true
entity_id: ent_f2207ef042b2f2d75bbb5a5b
---

# Create Execution Plan

Create an execution plan via Parquet MCP (Neotoma first if execution_plans are in Neotoma), avoid duplicates, and link tasks when the work is phased. No markdown files in `truth/operations/execution-plans/`.

## When to Use

Use this skill when:
- User says "create execution plan", "add project plan", "create plan for [project]"
- User describes phased work that should be one task plus one plan (not parent + subtasks)

## Required Documents (load first)

1. **Execution plans rules:** [docs/execution_plans_rules.mdc](docs/execution_plans_rules.mdc) (Create Execution Plans, Task vs Subtasks, Task Creation Workflow, Schema Fields)
2. **Data access:** [docs/neotoma_parquet_migration_rules.mdc](docs/neotoma_parquet_migration_rules.mdc) (query Neotoma first for execution_plans; Parquet as backup)

## Workflow

1. **Query existing plans** by name or project_id (Neotoma MCP first, then Parquet MCP) to avoid duplicates. Use `mcp_parquet_read_parquet(data_type="execution_plans", filters={"name": "..."})` or by project_id.
2. **If duplicate exists:** Inform user and offer to update or link; do not create a second plan.
3. **If no duplicate:** Create plan via Parquet MCP `mcp_parquet_add_record` with `data_type="execution_plans"`. Include: execution_plan_id (e.g. 16-char UUID), name, project_id, project_name, status ("planning"|"planned"|"in_progress"|"completed"|"canceled"), domain, priority, objective, scope, milestones_phases, dependencies, constraints, success_criteria, start_date, created_date, updated_date, import_date, import_source_file ("manual_creation"). See [docs/execution_plans_rules.mdc](docs/execution_plans_rules.mdc) for full schema.
4. **Task linkage:** If the work is one initiative with phased steps, use one task + this execution plan (put phases in milestones_phases). Set plan's related_tasks to task_id and link task to plan. Use parent + subtasks only when each piece is a distinct deliverable with separate due dates/assignees.
5. **Never** create or edit markdown in `truth/operations/execution-plans/`.

## Constraints

- Query Neotoma first for execution_plans; use Parquet MCP when not in Neotoma.
- Always check for existing plans by name or project_id before creating.
- For phased work: one task + one execution plan; put phases in milestones_phases. Do not create parent + subtasks for single phased initiatives.
- FORBIDDEN: Creating or editing markdown files in `truth/operations/execution-plans/`.

## Related Rules

- [docs/execution_plans_rules.mdc](docs/execution_plans_rules.mdc) — Full schema, update pattern, task vs subtasks
- [docs/neotoma_parquet_migration_rules.mdc](docs/neotoma_parquet_migration_rules.mdc) — Read/write order
