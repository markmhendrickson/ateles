# Restoring harness headroom after a provider quota reset

## Purpose

Give the operator (or a session the operator explicitly directs) the exact, verified steps to restore
Codex/Cursor headroom in `~/.config/ateles/harness-headroom.json` once a provider's quota resets, and the
exact command to confirm the restore took effect before spending a live model call.

## Scope

Covers only the headroom-restore step for `execution/daemons/apis/harness_router.py` and the callers that
read it (`dispatch_role.py`, `execution/scripts/harness_lens_runner.py`). It does not cover provisioning new
provider credentials, changing `APIS_HARNESS_PROVIDERS`, or any other harness configuration.

## What this file governs

`~/.config/ateles/harness-headroom.json` is the single file
`execution/daemons/apis/harness_router.py` reads (via `configured_headroom()`)
to decide whether Claude, Codex, or Cursor is eligible for the NEXT dispatch —
including a lens review run through `execution/scripts/harness_lens_runner.py`
and `execution/daemons/apis/dispatch_role.py`. Its current shape (checked
2026-09-26, **not edited by this change** — see the hard rule below):

```json
{
  "claude": {
    "cooldown_reason": "quota_error_default",
    "cooldown_until": "2026-09-25T08:04:21.916748+00:00",
    "headroom": 0.15
  },
  "codex": 0.0,
  "cursor": 0.0
}
```

A provider's value is either a bare number `0.0`–`1.0`, or an object carrying
`headroom` plus `cooldown_reason`/`cooldown_until` (the shape a cooldown
leaves behind). `configured_headroom()` reads `headroom` out of either shape;
`harness_lens_runner.check_headroom()` refuses a dispatch outright when the
resolved value is exactly `0.0`, which is the operator-reset signal this
runbook is about.

## Hard rule: this PR does not touch the file

Task ent_898998f41372ce24369fb365 and its own hard rules are explicit: **don't
edit harness-headroom.json**. Nothing in this change reads-then-writes it,
and no script this PR adds accepts a flag that would. This file exists so the
restore step has a home once the operator (or a session the operator directs)
is ready to do it — it is instructions, not automation.

## Whose job the edit is

**The operator's**, or a session the operator explicitly directs to do it
after confirming the provider-side reset has actually happened — never a
session acting on its own timing guess. `harness-headroom.json` is a local
operator-owned config file (not synced from any API), so nothing this repo
runs can observe the provider's real quota reset on its own; the operator (or
whoever is watching the Codex/Cursor account) is the source of truth for "has
it actually reset yet."

## Exact command

Once the reset is confirmed (Codex resets ~2026-09-28 per the task's own
notes; re-check the actual account before running this — that date is this
task's estimate, not a guarantee):

```bash
python3 - <<'PY'
import json
from pathlib import Path

path = Path.home() / ".config" / "ateles" / "harness-headroom.json"
data = json.loads(path.read_text())

# Restore ONLY the providers that actually reset. Leave claude's entry
# untouched unless its own cooldown independently cleared.
data["codex"] = 1.0
data["cursor"] = 1.0

path.write_text(json.dumps(data, indent=2) + "\n")
print(path.read_text())
PY
```

Or, by hand: open the file in an editor, replace `"codex": 0.0` with
`"codex": 1.0` and `"cursor": 0.0` with `"cursor": 1.0`, save.

Setting `1.0` (full headroom) rather than a partial estimate is deliberate: a
weekly-limit reset genuinely restores full capacity, and there is no better
estimate available than "the provider says it reset." `harness_router`'s
smooth-weighted selection will naturally rebalance from there as real usage
accrues — nothing about the restore value needs to be precise.

## Verify the edit took

```bash
cat ~/.config/ateles/harness-headroom.json
python3 -c "
import sys
sys.path.insert(0, 'execution/daemons/apis')
from harness_router import configured_headroom
print(configured_headroom())
"
```

`configured_headroom()` must report the restored values — it takes the FIRST
of (file, env) that parses, so a stray `APIS_HARNESS_HEADROOM` env var could
otherwise silently override a correctly-edited file (see
`dispatch_role._headroom_note()`, which exists to surface exactly that
precedence trap).

## After restoring: the first live test

Do not run a live dispatch to confirm the restore — that would spend a model
call to test a config file. Instead confirm via
`harness_lens_runner.py --dry-run`, which reads the same `configured_headroom()`
path and refuses (loudly, before any worktree or dispatch) if headroom is
still `0.0`:

```bash
python3 execution/scripts/harness_lens_runner.py \
  --repo markmhendrickson/ateles --pr <PR> --head <sha> \
  --lens <lens> --agent <agent> --provider codex \
  --brief <path-to-the-lens-brief> --dry-run --json
```

A `HeadroomExhausted` refusal here means the file was not actually restored
(or an env var is overriding it) — fix that before spending the first real
model call. Once the dry run reports `"no_model_call_made": true` with a
sane `example_command`, the first live test described in this PR's body is
ready to run.
