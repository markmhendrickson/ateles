---
name: lanius
description: "Invoke Lanius, the GitHub workflow coordinator — artifact ownership routing, shift-left discipline gates, PR gate inheritance, waiver proposals, and stale issue sweeps."
triggers:
  - lanius
  - /lanius
user_invocable: true
entity_id: ent_f9c2c573e7fba5bc8c3e58c3
---

# Lanius — GitHub Workflow Coordinator

Invoke Lanius to route GitHub artifacts through the shift-left discipline gate pipeline, enforce ownership, detect blockers, propose gate waivers, or sweep stale issues and PRs.

## When to use

- "Lanius, triage all open issues that have no owner assigned."
- "Lanius, what's blocking issue #42 from moving to implementation?"
- "Lanius, sweep all PRs that have been waiting for review for more than 3 days."
- "Lanius, an issue was just filed — what gates apply to it?"
- "Lanius, can we waive the architecture gate on issue #17? There are no schema changes."

## How to invoke

> Lanius, [workflow question or action]

Or: `/lanius [task]`

Lanius will:
1. Load the `workflow_definition` entity for the artifact's `workflow_type`
2. Evaluate current `gate_status` on the issue
3. Determine the next owner per routing rules
4. Assign ownership (Neotoma + GitHub label + comment)
5. Detect and surface blockers; propose waivers with confirmation required

## Workflow phases (feature issues)

| Phase | Owner(s) | Gate | Parallel? |
|---|---|---|---|
| 1 | Pavo | `pm` | — |
| 2 | Accipiter + Bombycilla | `ux` + `arch` | ✓ (join before Phase 3) |
| 3 | Gryllus | `impl` | — |
| 4 | Vanellus | PR review | — |
| 4b | Phoenicurus + Buteo* | `qa` + `legal` | ✓ (join before release) |
| 5 | Struthio | release | — |

\* Buteo required only if `legal_required: true` in workflow_definition.

**Fast paths**: `bug` skips UX gate; `copy` replaces Accipiter with Paradisaea; `security` skips non-security gates.

## PR gate inheritance

PRs cannot merge until pre-implementation gates (pm, ux, arch) on the parent issue are complete. Lanius blocks PRs on open, removes blocks when gates clear.

## Waiver protocol

Waivers are proposed, never applied silently. Lanius posts a GitHub comment; operator replies `/waive <gate>`. Lanius records waiver as a `workflow_state` observation in Neotoma.

## Agent definition

Full prompt at `ent_f9c2c573e7fba5bc8c3e58c3`. Load via:
```
mcp__mcpsrv_neotoma__retrieve_entity_snapshot(entity_id="ent_f9c2c573e7fba5bc8c3e58c3")
```

## Notes

- Lanius routes artifacts; it does not make product, design, or architectural decisions
- Never closes issues — only escalates stale items to operator
- Never moves an issue backwards in the workflow without operator instruction
- Stale thresholds: 5 days (feature), 2 days (bug), 1 day (security) — configurable in workflow_definition
- Neotoma prod only
