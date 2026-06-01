# Rule: Agent routing — always look up before tasking

## Trigger

Before tasking any work to a swarm agent (creating a task entity with `assigned_to`, invoking a skill by agent name, or recommending an agent to the operator).

## Required behavior

1. **Assign at creation, not at dispatch** — every `task` entity MUST have `assigned_to` set the moment it is created (by any agent, daemon, or skill). Routing is a one-time classification done up front so the owning agent can prioritize the work against its own queue with full domain context. Do NOT defer routing to a central dispatcher at execution time.
2. **Retrieve live agent definitions** — call `mcp__mcpsrv_neotoma__retrieve_entities` with `entity_type: agent_definition` before routing.
3. **Match by `description` field** — the description is the authoritative statement of each agent's domain and job scope. Do not infer from the agent's name; names are bird genera and are not self-describing.
4. **Prefer the most specific match** — if one agent's description explicitly owns the domain (e.g. "owns platform-adapted social content"), that agent wins over a general-purpose worker.
5. **Apis is the fallback router, not the default** — assign `assigned_to: "apis"` ONLY when the task is genuinely ambiguous (no clear domain match), requires cross-agent coordination, or has been declined/escalated by its original assignee. Apis then re-routes or coordinates. Apis is not the default dispatcher for routable work.
6. **If no agent matches** — flag the gap explicitly. Propose adding the domain to an existing agent's definition or creating a new agent. Create the task anyway with `assigned_to` set to the closest match or `"apis"` (for re-routing) and a `notes` field documenting the gap.
7. **Sylvia (recurring tasks) routes too** — when Sylvia imports a Calendar event as a task or rolls a recurring task forward, it runs this same lookup to set `assigned_to`. On due date, agent-audience tasks dispatch to their `assigned_to` agent, not to Apis by default.
8. **Never hardcode agent assignments** — the quick-reference table in CLAUDE.md is a shortcut for common cases, not a substitute for a live lookup. Rosters change; the live Neotoma snapshot is authoritative.

## Forbidden patterns

- Assigning a task to an agent based solely on its name without reading its description.
- Skipping the retrieval step because "it's obvious" which agent applies.
- Using the CLAUDE.md quick-reference table as the only source of truth when routing a new task type.

## Rationale

Agent names are bird genera (Corvus, Apis, Gryllus, …) and carry no semantic content. Descriptions are the ground truth. The failure mode this rule prevents: spending multiple turns misrouting work because the agent roster was reasoned about from a stale or partial mental model rather than a live Neotoma lookup.

## Example

```
# Correct
retrieve_entities(entity_type="agent_definition")
→ match description containing "platform-adapted social content"
→ assign to Corvus (ent_b95bf915804ac40bba674529)

# Forbidden
# "Social content sounds like Corvus" → assign without retrieval
```
