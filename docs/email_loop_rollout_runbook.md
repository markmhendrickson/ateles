# Email Execution Loop — Staged Live Rollout Runbook

**Status:** runbook (2026-06-24) · **Plan:** Task-spine loop (`ent_aff87747b49e338790568af6`) · **Spec:** [task_execution_loop.md](task_execution_loop.md)

The email-driven execution loop (E1–E6) is built, tested (122), and live-validated in a contained run. This runbook turns it on **safely** on the live Apis daemon. The core risk: the three flags are **global** (every dispatched task is affected, no per-task selector), and the readiness gate can **park real tasks** in `awaiting_input`. So enable additive behavior first, the gating behavior second, and only widen after repeated clean cycles.

All flags live in `~/.config/neotoma/.env` (both Apis and Riparia load it at start; the three transport vars are already staged there). Operator-supervised throughout — the assistant will not flip these or restart the live daemon unattended.

---

## Stage 0 — Pre-flight (all DONE except the daemon actions)

- [x] Env staged: `ATELES_SWARM_EMAIL`, `OPERATOR_EMAIL`, `ATELES_GMAIL_SEND_CMD` in `~/.config/neotoma/.env`.
- [x] Confirmed send command (raw RFC822) + `gws` auth (prod token).
- [x] Gmail `Swarm` label + `to:+swarm` filter (label-only, no archive).
- [ ] Riparia plist installed: `cp execution/daemons/riparia/com.ateles.riparia.plist ~/Library/LaunchAgents/`
- [ ] Two log tails ready: `tail -f ~/Library/Logs/ateles/apis.log` and `~/Library/Logs/ateles/riparia.log`

---

## Stage 1 — Conversations + email only (NO readiness gate)

The additive, no-parking step: a run gets a conversation + kickoff/outcome emails; nothing is gated out.

```sh
# add to ~/.config/neotoma/.env
APIS_RUN_CONVERSATIONS=1
APIS_RUN_EMAIL=1
# (optional) ATELES_NOTIFY_EMAIL=1   # system notifications via email too

# start Riparia (inbound) + restart Apis to pick up the env
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ateles.riparia.plist
launchctl kickstart -k gui/$(id -u)/com.ateles.apis
```

**Watch one real task flow through and verify:**
- apis.log: `run conversation <id> opened for task <id>` then a `sent kickoff email` line.
- Inbox: a `[#ent_<task>]` thread arrives (labeled `Swarm`), and an outcome email on completion.
- **Reply to that email** → riparia.log: `routed operator reply … → conversation <id>`.
- Neotoma (MCP `retrieve_related_entities` on the conversation): kickoff + operator-reply (`sender_kind=operator`) + outcome turns, `PART_OF` the task.

**Rollback (any stage):** set the flags back to `0` in `.env`, `launchctl kickstart -k gui/$(id -u)/com.ateles.apis`. To stop Riparia: `launchctl bootout gui/$(id -u)/com.ateles.riparia`.

**Pass criteria:** ≥1 task shows the full conversation+email round-trip with no errors and no unexpected email volume.

---

## Stage 2 — Add the readiness gate (the one that can park tasks)

```sh
# add to ~/.config/neotoma/.env, restart Apis
APIS_READINESS_GATE=1
# optional, if the default 0.75 parks too much real work:
# APIS_READINESS_THRESHOLD=0.6
```

**Watch specifically for false-negatives** — the live risk is real tasks getting parked:
- apis.log: `readiness: task=<id> ready (score/threshold)` for well-specified tasks (they should proceed).
- For under-specified tasks: `parked awaiting_input (readiness=…, missing=…)` + a targeted gap email. Confirm these were *genuinely* under-specified, not good tasks the rubric misjudged.
- If good tasks are being parked: lower `APIS_READINESS_THRESHOLD`, or improve the task fields (`acceptance_criteria`, `constraints`), then re-dispatch (an `awaiting_input` task re-routes when the operator supplies input).

**Pass criteria:** well-specified tasks execute; only genuinely thin tasks park; no surprise stalls in real work.

---

## Stage 3 — Soak

Run several real tasks across a day or two with all three flags on. Monitor: email volume sane, no stalled/blocked pile-ups, Riparia routing replies reliably, no daemon crashes (fail-open should prevent any, but confirm). This is the "passes repeatedly" gate from the `canary_one_at_a_time` decision.

---

## Stage 4 — Widen + retire Telegram

Only after Stage 3 is clean:
- **Persist** the flags (they're already in `.env`; just leave them on).
- **Per-domain graduation:** keep high-blast domains gated. Tune per-agent `execution_policy` (`confidence_threshold`, `blast_radius_default`, `auto_execute_after_n_successful_recurrences`) and the checkpoint `block_until_approve → notify_and_proceed → auto` ladder. Start low-blast (docs/ops), expand as each domain earns trust.
- **Flip `ATELES_NOTIFY_EMAIL=1`** so system notifications go via email (Telegram becomes break-glass).
- **Stand down Cyphorhinus** (Telegram inbound is now Riparia's job): `launchctl bootout gui/$(id -u)/com.ateles.cyphorhinus`.

---

## Safety notes

- **Quiet window for first enable.** With global flags, enabling during a burst means many emails at once. Enable when the dispatch stream is calm, or create one controlled low-blast task and dispatch it right after enabling.
- **Everything is fail-open.** A missing address / gws error / Neotoma blip logs and no-ops; it never crashes dispatch. Worst case of a bad enable is noise + some parked tasks, both reversible.
- **The readiness gate is the only behavior-changing flag.** If in doubt, run Stages 1 and 3 with just conversations+email for a while before adding Stage 2.
