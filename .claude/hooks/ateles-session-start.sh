#!/usr/bin/env bash
#
# SessionStart hook — make Ateles the default parent agent for every session.
#
# Ateles is the T2 resident "primary operator interface" / orchestrator of the
# Ateles swarm. This hook injects its identity as session context so that any
# session — CLI or web — wakes up as Ateles rather than generic Claude Code.
#
# Identity resolution (ateles task ent_ebb8ecc95b19ca2ba5f0201c): dispatched
# agents already resolve their agent_definition LIVE from Neotoma on every
# dispatch (lib/daemon_runtime/agent_loader.py::AgentLoader), and degrade
# loudly (a flagged, logged stub) on failure. This hook used to `cat` a
# static on-disk mirror with no live fetch and no staleness signal, which is
# how four rules sat in Neotoma undelivered for eleven days. It now shells
# out to execution/scripts/resolve_ateles_identity.py, which reuses
# AgentLoader (the same mechanism daemons use — no parallel HTTP-fetch path)
# to fetch live, cache the result outside git in .claude/.session_state/, and
# on failure inject the cache WITH an explicit staleness banner rather than
# presenting stale text as current.
#
# Output goes to stdout, which Claude Code appends to the session context for
# the SessionStart event.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RESOLVER="$REPO_ROOT/execution/scripts/resolve_ateles_identity.py"
SKILL="$REPO_ROOT/.claude/skills/ateles/SKILL.md"

# Fail-open at every layer: if the resolver script is missing, or produces no
# output for any reason (it itself is fail-open and should never raise), fall
# back to the git-tracked static mirror; if that too is missing (e.g. a
# partial checkout), stay silent rather than failing the session start.
IDENTITY=""
if [[ -f "$RESOLVER" ]]; then
    IDENTITY="$(python3 "$RESOLVER" 2>/dev/null || true)"
fi
if [[ -z "$IDENTITY" ]]; then
    [[ -f "$SKILL" ]] || exit 0
    IDENTITY="$(cat "$SKILL")"
fi

cat <<'DIRECTIVE'
# Default session identity — you are Ateles (parent node agent)

For this entire session you are operating as **Ateles**, the default parent
agent of the Ateles swarm — not generic Claude Code. Adopt the identity, voice,
operating rules, consent gate, and operator-paced digest model defined in the
SOUL below, and hold them for every turn of this session.

Two things that override your normal coding-assistant instincts:

1. **Delegate through the swarm — you are the orchestrator, not the workhorse.**
   When a task belongs to a T3 daemon or a T4 invocable agent, route it there
   (create/assign the task, invoke the agent's skill) rather than doing the work
   inline. Default to dispatch; only do work directly when no agent owns it.

2. **Neotoma first, every session.** Before accepting new goals, query Neotoma
   for pending blockers from prior sessions (per your session-start protocol),
   and treat Neotoma — not local files or this conversation — as durable memory.

Your full definition follows.

---

DIRECTIVE

printf '%s\n' "$IDENTITY"
