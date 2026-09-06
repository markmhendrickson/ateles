# Apis autonomy flags — changing them, and rolling them back safely

Apis's autonomy boundaries are environment variables in
`~/Library/LaunchAgents/com.ateles.apis.plist`, mirrored by the reviewed file at
`execution/daemons/apis/com.ateles.apis.plist`.

| Flag | Boundary it governs |
|---|---|
| `ATELES_SWARM_AUTO_BUILD` | A green spec chains into an implementation PR. OFF = spec-only; the swarm stops and waits for the operator's `build` approval. |
| `APIS_AUTONOMY_AUTO_MERGE` | Merge without any operator involvement once the panel is green. |
| `APIS_APPROVAL_TRIGGERS_MERGE` | An explicit operator approval performs the merge itself. Distinct from the above: the human act is still required. |
| `ATELES_SWARM_AUTO_REREVIEW` | Re-review a PR automatically after a fix round pushes new commits. |

## Why this runbook exists

`ATELES_SWARM_AUTO_BUILD` was enabled and rolled back twice, and both times the
re-enable never happened:

- **2026-07-31** — disabled while containing a runaway Cicada dispatch loop; root
  cause filed as ateles#359. The session's own note said "re-enable with one
  PlistBuddy line once the handoff session fixes the root cause." It stayed off
  for 20 days.
- **2026-08-20** — re-enabled at the operator's request, then disabled ~90 minutes
  later after both handoffs failed on ateles#460. PR #482 fixed that cause the
  next day. The flag stayed off.

Neither rollback was wrong. Both were reasonable containment during a live
incident. The failure was that **the intent to restore lived only in a session
transcript**, so when the blocking fix merged, nothing surfaced the commitment.

A second, quieter failure compounded it: the reviewed plist did not declare
`ATELES_SWARM_AUTO_BUILD` at all, so no file recorded what the swarm was
authorised to do, and no diff could show it changing.

## Rules

1. **Every autonomy flag is declared explicitly in the reviewed plist**, including
   flags whose value matches the code default. An absent key is
   indistinguishable from an unmade decision.
2. **A rollback files a task before it is applied.** Create a `task` entity
   `PART_OF` the swarm plan naming the flag, the blocking issue, and the
   condition for restoring it. The task is what surfaces when the issue closes;
   a sentence in a session does not survive the session.
3. **A rollback is a change to the reviewed plist too**, not only to live state —
   otherwise the file silently misdescribes the running daemon.
4. **Never bundle an autonomy-flag change into an approval about something else.**
   The 2026-08-20 rollback rode along with an unrelated release decision. Ask for
   it on its own.

## Changing a flag

`kickstart -k` restarts the process but **reuses the cached service definition**,
so it does not re-read an edited plist. A previous session set a value, ran
`kickstart -k`, and the live process still reported the old value — the
verification lied. Always `bootout` then `bootstrap`:

```bash
/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:ATELES_SWARM_AUTO_BUILD 1" ~/Library/LaunchAgents/com.ateles.apis.plist
```

```bash
launchctl bootout gui/$UID/com.ateles.apis && launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.ateles.apis.plist
```

Then verify against the **running process**, never the file on disk:

```bash
ps eww $(launchctl list com.ateles.apis | awk -F'= ' '/"PID"/{print $2}') | tr ' ' '\n' | grep ATELES_SWARM_AUTO_BUILD
```

If the flag was set with `PlistBuddy`, mirror it into
`execution/daemons/apis/com.ateles.apis.plist` and commit, or the drift check
below will report it on every restart.

## The drift check

`lib/daemon_runtime/plist_drift.py` compares the live launchd environment against
the reviewed plist at Apis startup and logs at ERROR on divergence. Advisory by
default, matching `checkout_drift`'s posture — a dispatch daemon that refuses to
boot over a config difference would cause a larger outage than the drift it
reports. Set `ATELES_ENFORCE_PLIST_CONFIG=1` to make it fatal.

A missing or unparseable live plist reports `unknown`, not drift: daemons
launched by hand or in CI have nothing to compare against, and reporting that as
drift would train operators to ignore the warning. `HOME` and `PATH` are ignored
as legitimately machine-specific.

## Related

- `lib/daemon_runtime/checkout_drift.py` — the same class of problem for code
  rather than config.
- CLAUDE.md, "Which checkout a daemon runs from" — Apis runs from
  `~/ateles-rc-src`, never the shared session clone.
