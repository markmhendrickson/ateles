# `.claude/hooks`

Repo-scoped Claude Code hooks, registered in `../settings.json`.

## `ateles-session-start.sh` (SessionStart)

Makes **Ateles** — the T2 resident "primary operator interface" / orchestrator
of the Ateles swarm — the default agent for **every** session (CLI and web), not
just when `/ateles` is invoked manually or reached via Telegram.

On each SessionStart it reads `../skills/ateles/SKILL.md` (the canonical
definition, regenerated from Neotoma entity `ent_706f1432822b4a9d9d71c127`) and
prints it to stdout, which Claude Code appends to the session context. A short
directive ahead of the SOUL reinforces the two behaviors that matter most in an
interactive session:

1. **Delegate through the swarm** — route T3/T4 work to the owning agent rather
   than doing it inline.
2. **Neotoma first** — check for pending blockers before accepting new goals.

Because the hook reads the SKILL.md at runtime, it never drifts from the
Neotoma-sourced definition. If that file is missing the hook exits silently and
the session falls back to generic Claude Code.

To temporarily run a plain (non-Ateles) session, comment out the SessionStart
entry in `../settings.json` or rename this script.
