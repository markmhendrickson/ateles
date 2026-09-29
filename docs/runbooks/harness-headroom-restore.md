# Restoring harness headroom after a provider quota reset

## Purpose

Give the operator (or a session the operator explicitly directs) the exact, verified steps to restore
Codex/Cursor headroom in `~/.config/ateles/harness-headroom.json` once a provider's quota resets, and the
exact command to confirm the restore took effect before spending a live model call.

## Scope

Covers only the headroom-restore step for `execution/daemons/apis/harness_router.py` and the callers that
read it (`dispatch_role.py`, `execution/scripts/harness_lens_runner.py`). It does not cover provisioning new
provider credentials, changing `APIS_HARNESS_PROVIDERS`, or any other harness configuration.

## What this file governs (and what outranks it)

`execution/daemons/apis/harness_router.py` resolves each provider's headroom
through `configured_headroom()` to decide whether Claude, Codex, or Cursor is
eligible for the NEXT dispatch — including a lens review run through
`execution/scripts/harness_lens_runner.py` and
`execution/daemons/apis/dispatch_role.py`. The value is NOT read from this file
alone. Per provider, the first of these that applies wins:

1. **A manual or dated override object** in the file (or
   `APIS_HARNESS_HEADROOM`): an entry with `"cooldown_reason": "manual"`, or
   with a `cooldown_until` still in the future. It beats everything below,
   including the live usage snapshot.
2. **The live usage snapshot** (`~/.config/ateles/harness-usage.json`, or
   `APIS_HARNESS_USAGE_FILE`), when it has an opinion. A live `0.0` (the
   provider reported exhausted) is never outvoted by a hand-set number. A live
   reading also supersedes an undated override that was written before the
   snapshot's observation.
3. **An undated override**: a bare JSON number, or an object without
   `cooldown_until`. The file is the first source; `APIS_HARNESS_HEADROOM` is
   consulted only when the file is absent, unreadable, malformed, or not a JSON
   object.
4. **The default `1.0`.**

An override whose `cooldown_until` has passed is ignored. The default file is
`~/.config/ateles/harness-headroom.json`, and `APIS_HARNESS_HEADROOM_FILE`
selects a different file path.

Bare numbers from `0.0` through `1.0` are the normal form:

```json
{
  "claude": 0.15,
  "codex": 0.0,
  "cursor": 0.0
}
```

Non-numeric provider values resolve to `1.0`. Object entries with a nested
`headroom` field are read (rule 1 or 3 above, depending on their
`cooldown_reason` / `cooldown_until`).

**What a bare `1.0` edit can and cannot do.** Editing a bare number to `1.0`
overrides an older undated `0.0` and the default. It CANNOT override (a) a
manual or future-dated override object still present for that provider, or (b)
a live usage snapshot that still reports the provider exhausted. When either
applies, the edit takes no effect and the refusal will say which source won.

`harness_lens_runner.check_headroom()` refuses a dispatch outright when the
resolved value is exactly `0.0`, which is the operator-reset signal this
runbook is about, and its refusal names the source that produced the zero.

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

# This command is for the first Codex test. Restore ONLY Codex. Cursor remains
# at 0.0 until its separate reset is confirmed (the bound task currently says
# 2026-10-15, but re-check the account rather than trusting that estimate).
# Leave Claude's entry untouched unless its own cooldown independently cleared.
data["codex"] = 1.0

path.write_text(json.dumps(data, indent=2) + "\n")
print(path.read_text())
PY
```

Or, by hand: open the file in an editor, replace `"codex": 0.0` with
`"codex": 1.0`, and save. Leave `"cursor": 0.0` unchanged until Cursor's
own reset is confirmed. At that later point, repeat the same one-provider
edit for Cursor only.

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
from harness_router import headroom_resolution
for provider, (value, source) in headroom_resolution().items():
    print(provider, value, source)
"
```

Each line prints the resolved value AND the source that produced it:
`manual_override`, `dated_cooldown_override`, `live_usage_snapshot`,
`undated_override`, or `default`. After restoring Codex the Codex line must
show `1.0` with source `undated_override` (or `default`, if you removed its
entry). A `0.0` with any other source means the edit did not take: go to
"Refusal survives the edit" below. `dispatch_role._headroom_note()` prints the
resolved values, and which files are in play, at the start of every dispatch.

## Refusal survives the edit

The refusal from `harness_lens_runner.py` names the winning source. Match it:

| Source named | Why the edit did not take | What clears it |
|---|---|---|
| `LIVE USAGE SNAPSHOT` | The usage snapshot still records the provider as exhausted, and a live `0.0` is never outvoted by a hand-set number. | Nothing in the headroom file. The provider's reported reset must pass and a fresh usage observation must be recorded. Diagnose with `python3 -c "import sys; sys.path.insert(0, 'execution/daemons/apis'); import harness_router as h; print(h.live_headroom('codex'))"` (`None` means the snapshot has no opinion; `0.0` means it does). |
| `MANUAL override object` | An entry with `"cooldown_reason": "manual"` is still in the file (or `APIS_HARNESS_HEADROOM`). | The operator removes or replaces that whole entry; changing a nearby number is not enough. |
| `override object with a FUTURE cooldown_until` | A dated cooldown is still in force. | It expires on its own at that time, or the operator removes or replaces the entry. |
| the override file or `APIS_HARNESS_HEADROOM` | An undated zero is still there, or the file you edited is not the one in use (`APIS_HARNESS_HEADROOM_FILE` selects another path; an invalid file falls through to the variable). | Edit the named source. |

Never loop on re-editing the file when the refusal names the live snapshot or
an override object.

## After restoring: the first live test

Do not run a live dispatch to confirm the restore — that would spend a model
call to test a config file. Instead confirm via
`harness_lens_runner.py --dry-run`, which reads the same `configured_headroom()`
path and refuses (loudly, before any worktree or dispatch) if headroom is
still `0.0`:

```bash
python3 execution/scripts/harness_lens_runner.py \
  --repo markmhendrickson/ateles --pr <PR> --head <sha> \
  --lens <lens> --provider codex \
  --brief <path-to-the-lens-brief> --dry-run --json
```

(`--agent` is optional — it resolves from `--lens` via `review_panel.LENSES`
when the lens is in that registry; pass it explicitly only for a lens outside
it.)

A `HeadroomExhausted` refusal here names the source that produced the zero
(see "Refusal survives the edit" above): it may be the file, but it may equally
be the live usage snapshot or an override object — fix that source before
spending the first real model call. Authentication and sandbox-guard refusals
use the same JSON contract: `"ok": false` with a concrete `"reason"`. The CLI
intentionally exits zero for a cleanly reported `--dry-run` refusal, so
automation and human callers MUST inspect the JSON body and require
`"ok": true`; exit status alone does not mean the preflight passed. Once
`"ok": true` and `"no_model_call_made": true` appear with a sane
`example_command`, the first live test described in this PR's body is ready to
run.

## Monitoring a live dispatch from Neotoma alone

Pass `--task-entity-id ent_...` on the real (non-dry-run) command, then query
`harness_event` filtered on that same id — see `harness_lens_runner.py`'s own
module docstring, USAGE section, for the exact `retrieve_entities` call. No
access to this script's own process or stdout is needed to see whether the
dispatch started, completed, or failed.
