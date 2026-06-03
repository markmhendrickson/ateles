# Agent auto-invocation

Topic-based auto-invocation surfaces the right Ateles **T4 panel agent** inside
an interactive Claude Code session — without the operator having to remember
which bird name owns which domain.

## What it does

On every prompt, a `UserPromptSubmit` hook scores the prompt against a routing
manifest. When a panel agent's domain is clearly implicated (e.g. "implement
this issue and open a PR" → Gryllus), the hook injects a concise note into the
session context recommending that the session adopt the agent's operating
guidance, or invoke it explicitly via its slash command (`/gryllus`).

This is **suggestion + soft context injection**, not autonomous spawning:

- It nudges the *active* session toward the correct agent's guidance.
- It does **not** fork a nested `claude` process. Autonomous, event-driven
  spawning is already handled by the T3 daemons (Anthus/Formica) off Neotoma
  SSE events — see `docs/swarm_orchestration.md`. This hook complements that by
  covering the interactive, human-in-the-loop path the daemons never see.

## Why a hook (and not SKILL.md metadata)

`.claude/skills/<name>/SKILL.md` files are **mirror-canonical** — Apus
regenerates them from Neotoma `agent_definition` entities, so any routing
keywords added there would be reverted on the next mirror pass. The routing
manifest therefore lives in a hand-authored file outside the mirror path.

## Components

| File | Role |
|---|---|
| `.claude/agent-routing.json` | Hand-authored manifest: per-agent role, slash command, and match signals. Durable (not mirrored). |
| `.claude/hooks/agent_auto_invocation.py` | Stdlib-only `UserPromptSubmit` hook. Scores the prompt, emits `additionalContext`. Fails open. |
| `scripts/install-agent-auto-invocation.sh` | Installs the hook + panel-agent skills **device-wide** (user-level), pointing at this checkout. Idempotent; `--uninstall` reverses it. |

The hook is registered at the **user level** (`~/.claude/settings.json`), not in
the project's `.claude/settings.json` — see *Scope* below.

## Matching model

```
score(agent) = signal_weight       * (# signal substrings present)
             + strong_signal_weight * (# strong_signal substrings present)
```

Agents scoring `>= config.min_score` are surfaced, top `config.max_agents` by
score. An agent is skipped if the operator already typed its slash command.
All knobs (`enabled`, `min_score`, `max_agents`, weights) live in the manifest's
`config` block, so behavior is tuned by editing JSON — no code change required.

Defaults: `min_score: 2`, `max_agents: 3`. The threshold of 2 means either one
strong signal or two ordinary signals must be present, which keeps generic
single-word prompts from firing.

## Tuning

- **Add an agent**: append an entry to `agents[]` with `name`, `command`,
  `role`, `signals`, and `strong_signals`. Keep `role` in sync with the agent's
  SKILL.md description.
- **Reduce false positives**: raise `min_score`, or move a noisy term from
  `signals` to a more specific multi-word phrase.
- **Disable entirely**: set `config.enabled` to `false` (the hook then emits
  nothing).

## Testing

```bash
# Built-in assertions across representative prompts:
python3 .claude/hooks/agent_auto_invocation.py --selftest

# Ad-hoc: see which agents a given prompt would surface:
python3 .claude/hooks/agent_auto_invocation.py --prompt "review the pr and squash merge it"

# Exercise the real stdin/stdout hook contract:
echo '{"prompt":"implement issue #42 and open a pr"}' \
  | python3 .claude/hooks/agent_auto_invocation.py
```

## Safety

The hook is **fail-open**: a missing manifest, malformed JSON, or unreadable
stdin results in exit 0 with no output. It never blocks or delays a prompt, and
it never modifies state. `additionalContext` is injected for the model only; it
is not shown to the user as a separate message.

## Scope: device-wide vs project-only

A *project*-level `.claude/settings.json` hook only fires in sessions rooted in
the ateles repo. Two things are needed for auto-invocation to work in **any**
repo on the machine:

1. The hook must be registered at the **user level** (`~/.claude/settings.json`),
   so it runs regardless of which repo the session is in.
2. The agent skills must be available at the **user level**
   (`~/.claude/skills/`), or the recommended slash command (`/gryllus`, …) won't
   resolve outside ateles — panel agents are otherwise *project* skills.

`scripts/install-agent-auto-invocation.sh` does both, by reference rather than
by copy: user settings point at this checkout's hook script (absolute path), and
each panel skill is **symlinked** from `~/.claude/skills/<name>` into
`<ateles>/.claude/skills/<name>`. The ateles repo stays the single source of
truth, so `git pull` and Apus SKILL.md re-mirrors are picked up automatically.

```bash
# from the ateles checkout, on the target machine:
scripts/install-agent-auto-invocation.sh            # install device-wide
scripts/install-agent-auto-invocation.sh --uninstall
```

The hook script itself is cwd-independent: its manifest lookup falls back to a
path relative to the script, so it finds `agent-routing.json` in the ateles
checkout no matter which repo invoked it. (A different repo may still override
by shipping its own `.claude/agent-routing.json`.)

> **Do not** also register the hook in the project `.claude/settings.json` once
> it's installed user-level — both would fire inside ateles and the context
> would be injected twice. The installer prints this reminder.

The installed skill set is read from `agent-routing.json` at install time, so it
always matches what the hook can recommend: the 13 core T4 panel agents (Pavo,
Accipiter, Bombycilla, Gryllus, Vanellus, Lanius, Phoenicurus, Struthio, Buteo,
Hirundo, Paradisaea, Corvus, Regulus). Domain-utility skills (e.g.
`email-triage`, `deploy-website`) and T3 daemons are intentionally excluded.

## Future: Neotoma-canonical routing

A later phase can move match signals into a `routing` field on each
`agent_definition` entity and have Apus generate `agent-routing.json` during the
mirror pass — making Neotoma the single source of truth, consistent with the
rest of the agent definitions. An LLM-based classifier could also replace the
deterministic substring matcher if precision demands it; the hook's
`score_agents()` is the only function that would change.
