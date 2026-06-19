# Swarm HITL Checkpoints — Design

**Status:** design (2026-06-19) · **Scope:** GitHub flows now, architected to extend to all swarm processes.

**Goal:** let the operator configure **human-in-the-loop checkpoints** that pause the swarm for a verdict before it proceeds at a given boundary (e.g. before writing code on a non-trivial issue, before merging a PR, before a release, before any external comms). Trivial work flows freely; oversight is dialed up where it matters, "at least until the swarm is trained to handle it without oversight."

This is **mostly wiring existing primitives**, not a new system. The swarm already has: `execution_policy.checkpoints`, `checkpoint_brief` (blocking + operator notify), the confidence×blast-radius gate in `apis.py` (`write_checkpoint_brief` / `handle_checkpoint_brief`), operator-gated merge by default, and the `issue_comment` command path (`/confirm-gates-clear`, `/swarm-run`). This design generalizes them into operator-configurable, per-boundary checkpoints with a uniform confirm command.

---

## What already exists (build on, don't replace)

- **`execution_policy.checkpoints`** — already a list of `{name, trigger, brief_template, blocking}` (verified on live policies, e.g. the Influencer-research + #174 batch policies). This is 80% of the model.
- **`checkpoint_brief`** — a blocking artifact the gate files + notifies the operator on; `apis.py` `handle_checkpoint_brief` reacts when resolved.
- **confidence × blast-radius gate** (`apis.py`) — `_infer_action_type` + `high/low_blast_action_types` already decide auto-execute vs. file-a-blocking-checkpoint. `APIS_AUTO_EXECUTE`, `APIS_AUTONOMY_AUTO_MERGE` are the coarse on/off switches.
- **operator-gated merge** — Vanellus aggregates but never merges unless `APIS_AUTONOMY_AUTO_MERGE=1`; a `checkpoint_brief` is filed at the merge boundary today.
- **`issue_comment` command path** — `/confirm-gates-clear`, `/swarm-run`, operator-login-guarded, bot-author-guarded (#112/#122/#123).

The gap: checkpoints are **hardcoded at specific boundaries** (mainly merge) and **not operator-configurable per boundary/condition**, and there's no uniform approve/reject confirm command.

---

## The model: a checkpoint is `{boundary, condition, action}`

Extend `execution_policy.checkpoints[]` entries to:
```
{
  "name": "pre_merge",
  "boundary": "pre_merge",          # WHERE in the flow (new: a controlled vocabulary)
  "condition": "blast>=high | diff>200 | labels:checkpoint:strict",  # WHEN it fires (new: expression; absent = always)
  "action": "block_until_approve",  # WHAT it does: block_until_approve | notify_and_proceed | auto
  "brief_template": "...",          # existing
  "blocking": true                  # existing (kept; redundant-but-compatible with action)
}
```

### Boundary vocabulary (the controlled set of pause points)
GitHub flow:
- `issue.triaged` — after Lanius triages, before expectations/Pavo.
- `pre_impl` — before the swarm writes ANY code/docs on an issue. **(the core ask for non-trivial issues)**
- `pr.opened` — after a PR opens, before the review panel.
- `pre_merge` — before merge. **(already operator-gated; formalize as a configurable checkpoint)**
- `pre_release` — before any release/publish/tag. **(high blast; default block)**

Non-GitHub (future, same vocabulary):
- `pre_comms` — before any external-facing post (Corvus/Hirundo → Typefully/Substack).
- `pre_payment` — before Monedula executes a payment.
- `pre_skill_change` — before `review_learning` applies a proposed skill/rule update.
- `pre_irreversible` — generic catch-all for `block:irreversible:*` permission scopes.

### Condition expression (so trivial work isn't gated)
A small DSL evaluated against the trigger context:
- `always` (default if omitted)
- `blast>=high` / `blast>=medium` — reuses `_infer_action_type` blast radius.
- `confidence<high` — reuses `execution_policy.swarm_confidence`.
- `diff>N` — PR changed-lines threshold.
- `labels:<label>` — issue/PR carries a label (e.g. `checkpoint:strict`).
- `touches:<glob>` — diff touches a path (e.g. `touches:src/migrations/**`).
- boolean `&` / `|` of the above.

### Action
- `block_until_approve` — hard gate: file a `checkpoint_brief`, notify operator, STOP until `/approve`. (your core ask)
- `notify_and_proceed` — soft: file the brief + notify, but continue (FYI checkpoints).
- `auto` — no checkpoint (explicit "this boundary is autonomous now" — the training-graduation end state).

---

## Operator confirm mechanism (uniform across all boundaries)

A blocking checkpoint files a `checkpoint_brief` (summary + what's proposed + diff/context links + the Neotoma anchor) and notifies the operator (Telegram + a GitHub comment when there's an issue/PR). The operator releases it with a **GitHub comment command** on the issue/PR (the `issue_comment` path, already wired + bot/operator-guarded):

- **`/approve`** — proceed past this checkpoint.
- **`/reject <reason>`** — stop; record the reason (becomes swarm-learning signal).
- **`/hold`** — park (no auto-timeout; stays blocked).
- (existing `/confirm-gates-clear` becomes an alias/special-case of `/approve` at the gate boundary; `/swarm-run` unchanged.)

For non-GitHub flows with no issue/PR thread, the same verdicts arrive via the Telegram bot (Onychomys) or by resolving the `checkpoint_brief` entity directly (`handle_checkpoint_brief` already does this).

Every command is **operator-login-guarded** and **bot-author-guarded** (reuse `_OPERATOR_LOGIN` + `_is_bot_author` from #122/#123).

---

## Configuration: per-repo default + per-item override

1. **Repo-level default** — one `execution_policy` per repo (linked to the repo, not a plan) sets the default checkpoints. Sensible starting defaults:
   - `pre_impl`: `block_until_approve` if `blast>=high | diff>200 | labels:checkpoint:strict` (trivial issues flow; non-trivial pause). 
   - `pre_merge`: `block_until_approve` `always` (matches today's operator-gated-merge default).
   - `pre_release`: `block_until_approve` `always`.
   - `issue.triaged` / `pr.opened`: `auto` (no pause — triage is cheap + reversible).
2. **Per-item override** — an issue/PR label (`checkpoint:strict` → block everything; `checkpoint:auto` → trust the swarm for this one) or an `execution_policy` linked to the specific issue. This is the "dial oversight per non-trivial item" control.
3. **Global kill-switch** — `APIS_AUTO_EXECUTE=0` (already exists) forces everything to checkpoint; the per-boundary config refines from there.

---

## Build phases

- **Phase H1 — `pre_merge` formalized.** Convert today's hardcoded operator-gated-merge into a configured `pre_merge` checkpoint + the `/approve` `/reject` `/hold` commands (generalizing `/confirm-gates-clear`). Lowest-risk: it's already the default behavior; this just makes it explicit + adds the verdict verbs. Test on a real PR.
- **Phase H2 — `pre_impl` checkpoint.** The core ask: pause before the swarm writes code on a non-trivial issue (condition: `blast>=high | diff>200 | labels:checkpoint:strict`). File the brief, block, release on `/approve`. (Note: today the swarm doesn't auto-*implement* from an issue — it reviews; so `pre_impl` becomes load-bearing when/if auto-implementation is wired. Until then it gates the issue→expectations→scoping handoff for flagged issues.)
- **Phase H3 — `pre_release`.** Block before any release/publish (already high-blast in the policy vocabulary).
- **Phase H4 — condition DSL + per-repo execution_policy + label overrides.** The configurability layer.
- **Phase H5 (later) — extend to non-GitHub boundaries** (`pre_comms`, `pre_payment`, `pre_skill_change`) using the same model.

Each phase shippable + verifiable on a live issue/PR (the operator-steered one-at-a-time rhythm).

---

## Why this is the right shape
- **Reuses the governance schema you already designed** (`execution_policy.checkpoints` + `checkpoint_brief`) — no parallel system, no schema churn beyond extending checkpoint entries.
- **One confirm grammar** (`/approve` `/reject` `/hold`) across every boundary — operator learns it once.
- **Condition-gated** so trivial work isn't slowed — oversight scales with risk, exactly your "non-trivial at least until trained" intent.
- **Graduation path built in** — flip a boundary's action from `block_until_approve` → `notify_and_proceed` → `auto` as the swarm earns trust, per repo or per condition.

## Risks / notes
- **Don't deadlock** (we hit this with `/confirm-gates-clear`): a `block_until_approve` with no working release command is a dead-end. Every blocking checkpoint MUST have a wired `/approve` and an operator notification; add a self-check that no boundary blocks without a release path.
- **`/reject` should feed learning** — a rejected checkpoint is a training signal (`review_learning`), not just a stop.
- **Operator-identity dependence** — commands rely on the HMAC-verified `comment.user.login` == operator (per #114). Per-agent accounts (#109) don't change this.
- **Condition DSL scope creep** — keep the expression grammar small (the listed operators); resist a full rules engine.
