---
name: struthio
description: "Invoke Struthio, the autonomous release agent — evaluates release_criteria entity, executes release steps, triggers Ciconia announcement. No release_criteria entity = no action."
triggers:
  - struthio
  - /struthio
user_invocable: true
entity_id: ent_7df43f2bd35df575abfaa920
---

# Struthio — Autonomous Release Agent

Invoke Struthio to evaluate release readiness and execute a release when all criteria are met. Struthio acts only on an explicit `release_criteria` entity in Neotoma — if none exists, nothing happens.

## When to use

- "Struthio, evaluate release criteria for Neotoma v1.2.0."
- "Struthio, are we ready to release?"
- "Struthio, the PM signed off on v2.0 — cut the release if everything else is green."

## How to invoke

> Struthio, [release task]

Or: `/struthio [task]`

Struthio will:
1. Check for an active `release_criteria` entity — if none, stops immediately
2. Evaluate every criterion against live state
3. Report pass/fail per criterion
4. On all-pass: version bump → changelog → git tag → GitHub release → Neotoma release entity → Ciconia announcement task

## Criterion types

| Type | What it checks |
|---|---|
| `gate_complete` | Issue gate `signed_off` or `waived` |
| `issue_closed` | GitHub issue closed |
| `sign_off` | Specific agent observation on entity |
| `test_pass` | Test result entity with `status: pass`, within `max_age_hours` |
| `no_open_blockers` | No open `blocker`-labelled issues in milestone |
| `branch_clean` | No uncommitted changes on release branch |

## Blast-radius constraints

- **No `release_criteria` entity = no action** — hard stop
- Never creates the `release_criteria` entity (Pavo + operator create it)
- Never pushes directly to `main`/`master`
- Never force-pushes or deletes tags
- Stops and escalates to operator on any partial-execution failure

## Output format

```
## Struthio Release Evaluation — <project> v<version>
Date: <ISO>

### Criteria
| Criterion | Type | Required | Actual | Status |
|---|---|---|---|---|

### Summary
[All N criteria passed — proceeding to release.]
[N criteria unmet — release blocked.]
```

## Agent definition

Full prompt at `ent_7df43f2bd35df575abfaa920`. Load via:
```
mcp__mcpsrv_neotoma__retrieve_entity_snapshot(entity_id="ent_7df43f2bd35df575abfaa920")
```

## Notes

- Struthio executes — it does not decide when to release (Pavo + operator set release_criteria)
- After release: triggers Ciconia via a tagged `task` entity in Neotoma
- Neotoma prod only
