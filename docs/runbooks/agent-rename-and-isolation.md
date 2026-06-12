# Runbook — Agent voice-rename + working-tree isolation hardening

Status: **operator-driven, host-side.** Do NOT execute from a Claude Code worktree session.
Authored 2026-06-10 from session `strange-brattain-ea70ff`.

## Why this is a host operation, not a worktree task

Git worktrees isolate **files**. They do nothing for three shared layers that the
agent rename and the daemon fleet both depend on:

1. **The shared `~/repos/ateles` checkout** — it is itself a worktree pinned to whatever
   branch was last checked out. Every running daemon executes Python/venv out of this
   path (`launchctl list | grep ateles` → most point at `/Users/markmhendrickson/repos/ateles/...`).
   Any session that `git checkout`s a branch here changes what the live daemons run.
2. **Running launchd daemons** — bound to those shared paths, not to per-session worktrees.
3. **Global Neotoma (prod)** — single shared store. `agent_definition` identity changes are
   visible to every session and daemon instantly, regardless of git branch.

The agent rename mutates all three. Therefore it must run as a coordinated, single-owner,
host-side pass while the shared checkout is **quiescent** (clean tree, no other active session).

## Preconditions (verify before starting)

```bash
cd ~/repos/ateles
git status --short          # MUST be empty (no other session mid-flight)
git rev-parse --abbrev-ref HEAD   # note the branch; expect main or a clean state
launchctl list | grep ateles      # inventory loaded daemons + pids
```
If the tree is dirty or on a feature branch, STOP — another session owns it. Coordinate first.

---

## Lever 1 — Pin the shared root to `main`, treat it read-only

Goal: nobody does interactive `git checkout <branch>` in `~/repos/ateles`. All interactive
work happens in per-session `.claude/worktrees/<name>` worktrees (already the norm).

```bash
cd ~/repos/ateles
git stash list && git status --short   # confirm clean; resolve any in-flight work first
git checkout main
git pull --ff-only origin main
```
Then socialize the rule: the root is deploy/daemon-source only. (Optional enforcement:
a `pre-checkout`-style guard or a CI/lint note; not scripted here.)

## Lever 2 — Repoint daemons to a dedicated pinned checkout

Goal: no daemon reads code from the flippable root. A pinned checkout already exists:
`~/repos/ateles-daemons-main` on branch `daemons-stable`.

For each `~/Library/LaunchAgents/com.ateles.<daemon>.plist` that currently points at
`/Users/markmhendrickson/repos/ateles/...`, repoint ProgramArguments + WorkingDirectory to
`/Users/markmhendrickson/repos/ateles-daemons-main/...`, then:

```bash
launchctl unload ~/Library/LaunchAgents/com.ateles.<daemon>.plist
launchctl load   ~/Library/LaunchAgents/com.ateles.<daemon>.plist
launchctl list | grep ateles.<daemon>   # confirm it came back with a pid
```
Do this one daemon at a time, verifying each reloads before the next. Keep
`ateles-daemons-main` updated via `git -C ~/repos/ateles-daemons-main pull --ff-only`.

---

## The rename pass (per agent, ATOMIC)

Locked slate (Neotoma decision `agent_voice_rename_slate`):

| Old | New | Notes |
|---|---|---|
| Onychomys | **Ateles** | root; Neotoma side + host-side real-source done (PR #86, commit 10e5077; alias kept). Entity flip done. |
| Monedula / Corvus-monedula | **Jackdaw** | LIVE payment daemon. NOT Pica (Pica is already the disk-cleanup agent). NOT code-only: env-var contract in `ateles-private/.env` (`MONEDULA_PROFILES` etc) + #78 finance routing + AAuth key. DEFER to coordinated host+private pass. |
| Gryllus | **Cicada** | real-source done (PR #86, 1c3bed1). Entity flip pending. |
| Luscinia | **Robin** | off-theme. real-source done (PR #86). Entity flip pending. |
| Phoenicurus | **Finch** | LIVE daemon — DEFER until phoenicurus-release work settles |
| Bombycilla | **Waxwing** | off-theme (= its common name). real-source done (PR #86). Entity flip pending. |
| Paradisaea | **Manucode** | real-source done (PR #86). Entity flip pending. |

The binding chain (why order matters): daemons call `AgentLoader("<name>").load()` against
global Neotoma. If you flip the Neotoma `name` while the running code still calls the old
name, the lookup misses → minimal-default fallback = split brain. So per agent, do the code
and Neotoma changes together, then reload the daemon.

Per-agent atomic sequence:

1. **Code/dir** (in the pinned checkout / repo, on a `rename/voice-robust-agents` branch):
   - rename `execution/daemons/<old>/` → `<new>/` (for daemon agents)
   - rename `.claude/skills/<old>/` → handled by neotoma regen, NOT by hand (SKILL.md is generated; header says "Do not edit directly")
   - update every `AgentLoader("<old>")` call site → `"<new>"`
   - sweep on-disk refs: `grep -rIl <old> . | grep -vE 'node_modules|\.claude/worktrees|\.git/'`
2. **Neotoma**: `correct(agent_definition, field="name", value="<new>")` (drives canonical_name
   via `canonical_name_fields:["name"]`), plus `genus`, `aauth_sub`, `description`,
   `prompt_markdown` self-identity. (Ateles already done.)
3. **launchd** (daemon agents only): rename the plist `com.ateles.<old>` → `<new>`, update its
   `TELEGRAM_TOPIC_<AGENT>` env + label, then unload→load. Verify pid.
4. **Regenerate SKILL.md** via the `neotoma` CLI / Inspector (the documented regenerator).
5. **Verify**: `grep -rIl <old>` returns only intended alias references; daemon back with a pid;
   `retrieve_entity_by_identifier(<new>)` resolves and `<old>` still resolves via alias.

### Extra bindings the Ateles rename must chase (host-side, still pending)

- Commit `b494ed9 #60` "default every session to the Onychomys parent agent" — **Onychomys is
  hardcoded into session-default behavior in code.** Update to `ateles` in coordination with
  that code's owner. This is the main thing keeping the Neotoma-side Ateles rename from being
  fully consistent.
- OpenClaw operator launch config / `AtelesBot` Telegram identity.
- ~60 on-disk refs in README, docs/*, `anthus.py` + `morning-brief.py` cross-refs, and other
  agents' generated SKILL.md routing.

### Mirror reconciliation — plan body + Apus (do NOT skip)

`docs/taxonomy.md` and `docs/architecture.md` are **Apus mirror outputs** of plan entity
`ent_99ace4dd6673aa36ed08b1fe` (the `ateles-architecture-docs` profile mirrors the whole
`docs/` dir; the files carry a "Do not edit manually — update via Neotoma" header). PR #86
hand-edited them to the new names so the branch is self-consistent, but **the next Apus
mirror rebuild will revert those edits unless the plan `body` is updated to match.**

So the host pass MUST also: update the plan `body` (and `architecture`/`summary` if they name
the renamed agents) for every agent whose `agent_definition` entity has been flipped, then let
Apus rebuild `docs/`. Do this in lockstep with the entity flips — updating the body before the
entity flips would assert a false "done" state; updating the entity without the body leaves the
mirror to revert the doc edits. Monedula/Phoenicurus stay old in the body until their deferred
renames land.

## Current live inconsistency to be aware of

As of 2026-06-10, global Neotoma has `agent_definition` flipped Onychomys→Ateles (alias
preserved), while running daemons + session-default code #60 still say "onychomys". Harmless
today (alias resolves; operator reloads per session), but do not add more half-flips: finish
each agent's chain in one coordinated pass.
