# Ateles hooks — three-layer source-of-truth model

Mirrors the shape of `docs/agents/README.md` (agent_definition -> canonical
doc -> harness mirror), applied to harness hooks instead of agents.

| Layer | Location | Analogy (agents) |
| --- | --- | --- |
| Policy | Neotoma `hook_policy` entity | `agent_definition` |
| Logic | `hooks/logic/<hook_name>.py` (harness-agnostic, plain function + tests) | `docs/agents/*.md` |
| Adapter | `.claude/hooks/<hook_name>.py` (thin, imports logic, wires to the harness event) | `.claude/skills/<name>/SKILL.md` |
| Generator | `execution/scripts/render_hooks.py` (writes adapters + `.claude/settings.json` wiring from policy+logic), `--check` mode | `execution/scripts/render_agent_docs.py` |

**Neotoma `hook_policy` is canonical for *why* a hook exists and *how* it is
wired** (event, matcher, intent, enforced contract). **`hooks/logic/<name>.py`
is canonical for *what the hook does*** — a pure function, no harness imports,
independently testable. **`.claude/hooks/<name>.py` and the matcher/event
entries in `.claude/settings.json` are GENERATED derived artifacts** — never
hand-edited; regenerate them with `render_hooks.py --write`.

## Flow: modifying the pilot hook (`spawn_task_neotoma_pairing`)

1. **Edit `hooks/logic/spawn_task_neotoma_pairing.py`** — a pure function,
   no Claude imports. Plain file edit, run its tests
   (`python3 -m pytest hooks/logic/tests/`).
2. **Run `python3 execution/scripts/render_hooks.py --check`.**
   - Pass: `OK: 5/5 hook adapters match policy+logic`, exit 0.
   - Drift: names the exact stale file and the exact fix command, exit 1:
     ```
     DRIFT: .claude/hooks/spawn_task_neotoma_pairing.py does not match hooks/logic/spawn_task_neotoma_pairing.py
       run: python render_hooks.py --write
     ```
3. **Run `python3 execution/scripts/render_hooks.py --write`.** Regenerates
   `.claude/hooks/*.py` + the matcher/event wiring in `.claude/settings.json`
   (touching only entries it manages — hand-authored hook entries, e.g.
   `ateles-session-start.sh`, are left alone). Prints a diff-style summary,
   calling out any matcher/event-name change explicitly.
4. **No `hook_policy` entity exists yet** (new hook, or logic added before
   policy is authored): the generator refuses to produce or update the
   adapter and fails closed — see [POLICY_TEMPLATE.md](POLICY_TEMPLATE.md).

## Error / empty states

| Condition | Behavior |
| --- | --- |
| Adapter hand-edited, drifts from logic | `--check` fails loud (exit 1), names the file + fix command |
| Policy archived/deleted in Neotoma but adapter still deployed | `--check` warns (exit 2): `WARN: hooks/logic/<name>.py has no active hook_policy for harness '<harness>' — adapter is running ungoverned` |
| New harness has no adapter for a hook with policy+logic | `render_hooks.py --check --harness <name>` reports `NOT_IMPLEMENTED: <name> (policy exists, no <harness> adapter)` (exit 3) |
| Logic module raises at runtime | Adapter catches it, emits a one-line `hookSpecificOutput.additionalContext` naming the hook and the exception — never a bare stack trace |

## Exit-code contract (`render_hooks.py --check`)

A versioned contract — treat a change to what a code means as a breaking
change to CI, same discipline as `openapi_contract_flow.md` applies to
HTTP/MCP surfaces.

| Code | Meaning |
| --- | --- |
| 0 | OK — disk matches policy+logic, or `--write` succeeded |
| 1 | DRIFT (stale adapter) or missing-policy refusal (no `hook_policy` entity and no adapter deployed yet) |
| 2 | WARN — an adapter is deployed on disk but has no active `hook_policy` (ungoverned) |
| 3 | NOT_IMPLEMENTED — `--harness <name>` targets a harness with no adapter for a governed hook |

## Worked example: `spawn_task_neotoma_pairing`

- **Policy** — Neotoma `hook_policy` entity, canonical name
  `hook_policy:spawn_task_neotoma_pairing|claude|markmhendrickson/ateles`:
  ```json
  {
    "hook_name": "spawn_task_neotoma_pairing",
    "harness": "claude",
    "repository": "markmhendrickson/ateles",
    "event": "PostToolUse",
    "matcher": "^mcp__ccd_session__spawn_task$",
    "intent": "A spawn_task chip is an ephemeral Claude Code UI construct, not a Neotoma entity...",
    "enforces": "After a spawn_task tool call succeeds, remind the assistant to create a paired Neotoma task entity...",
    "status": "active"
  }
  ```
- **Logic** — `hooks/logic/spawn_task_neotoma_pairing.py`: a pure
  `handle(event: dict) -> str | None` function; see the file for the full
  reminder-text generation.
- **Generated adapter** — `.claude/hooks/spawn_task_neotoma_pairing.py`:
  stdlib-only stdin/stdout wiring that imports `handle`, catches any
  exception, and prints the `hookSpecificOutput.additionalContext` JSON
  Claude Code expects. Regenerate with `render_hooks.py --write`; never edit
  directly.

## CLI reference

```
render_hooks.py --check                       # verify disk matches Neotoma+logic; exit 1 on drift
render_hooks.py --write                        # regenerate adapters + settings.json wiring
render_hooks.py --check --harness openclaw      # report NOT_IMPLEMENTED gaps for another harness
render_hooks.py --help                          # full flag list
```

## Discoverability

`render_hooks.py` lives at `execution/scripts/render_hooks.py`, the same
directory as `render_agent_docs.py` — a developer who already knows one finds
the other by pattern-matching the filename. CI runs `render_hooks.py --check`
in the same job/step as `render_agent_docs.py --check`
(`.github/workflows/generated-mirrors.yml`), so a drift failure reads as one
category ("the swarm's source-of-truth checks failed"), not two unrelated
red X's.
