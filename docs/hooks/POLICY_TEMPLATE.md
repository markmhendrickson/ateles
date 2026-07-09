# `hook_policy` — minimal entity shape

A Neotoma `hook_policy` entity is the canonical source of *why a hook exists*
and *how it is wired*. `execution/scripts/render_hooks.py` refuses to
generate or update a hook's adapter until one exists (fail closed) — see
`docs/hooks/README.md`'s error-states table.

Field names below are the DECLARED schema fields (registered via
`register_schema`, entity_type `hook_policy`) — they must match what
`render_hooks.py`'s error/warn messages reference verbatim. A mismatch
between this doc, the schema, and the error strings is the #1 way this
abstraction rots; `scripts/tests/test_docs_schema_field_consistency.py`
enforces the three stay identical.

## Fields

| Field | Required | Description |
| --- | --- | --- |
| `hook_name` | yes | Stable identifier matching `hooks/logic/<hook_name>.py` and the adapter file name, e.g. `spawn_task_neotoma_pairing`. |
| `harness` | yes | Target harness this policy governs an adapter for, e.g. `claude`. Scopes lookup so same-named hooks in different harnesses never collide. |
| `repository` | yes | Repo slug this policy governs, e.g. `markmhendrickson/ateles`. Combined with `hook_name` + `harness` forms the stable lookup key (`canonical_name_fields`). |
| `event` | yes | Harness event this hook binds to, e.g. `PostToolUse`, `SessionStart`, `UserPromptSubmit`, `Stop`. |
| `matcher` | no | Harness matcher string gating which tool/event instances trigger the hook, e.g. `^mcp__ccd_session__spawn_task$`. Empty/absent means unconditional within the event. |
| `intent` | yes | Why this hook exists — the governance intent in plain language. Redact PII/real identifiers; this doc and its worked examples ship in the public repo. |
| `enforces` | yes | What behavior/contract this hook enforces or reminds toward, in plain language. |
| `status` | yes | `active` \| `archived`. An `archived` policy with a deployed adapter means the adapter is running ungoverned — `render_hooks.py --check` WARNs. |
| `owner_agent` | no | Swarm agent or team that owns/authored this policy. |

`hook_name` + `harness` + `repository` together are the entity's
`canonical_name_fields` — storing a second `hook_policy` with the same three
values updates (does not duplicate) the existing entity.

## Minimal example (placeholder values — no real entity IDs)

```json
{
  "entity_type": "hook_policy",
  "hook_name": "example_hook",
  "harness": "claude",
  "repository": "your-org/your-repo",
  "event": "PostToolUse",
  "matcher": "^mcp__some_tool__name$",
  "intent": "One or two sentences: why does this hook need to exist at all?",
  "enforces": "One or two sentences: what behavior/contract does it enforce or remind toward?",
  "status": "active",
  "owner_agent": "your-agent-name"
}
```

## Authoring one

Via the Neotoma MCP `store` tool (or `neotoma store --entity-type hook_policy
...` from the CLI), with a stable `idempotency_key`:

```python
store(
    idempotency_key="hook-policy-<hook_name>-<harness>-<repo-slug>-v1",
    observation_source="workflow_state",
    entities=[{
        "entity_type": "hook_policy",
        "hook_name": "example_hook",
        "harness": "claude",
        "repository": "your-org/your-repo",
        "event": "PostToolUse",
        "matcher": "",
        "intent": "...",
        "enforces": "...",
        "status": "active",
    }],
)
```

After authoring the policy, write `hooks/logic/<hook_name>.py` (a pure
`handle(event: dict) -> str | None` function, no harness imports) and run
`python3 execution/scripts/render_hooks.py --write` to generate the adapter.
