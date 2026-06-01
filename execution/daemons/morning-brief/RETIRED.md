# morning-brief — RETIRED 2026-06-01

This daemon is **retired and unloaded**. Do not re-enable it.

## Why

`morning-brief` ran at 05:30 and tried to re-summarize the `checkpoint_brief`
entities that Cotinga (05:00) stores for meetings, composing an Onychomys-voice
digest via Claude. In practice it:

- **Duplicated Cotinga's brief** — two morning messages for the same day.
- **Produced contradictory output** — it only reads `checkpoint_brief` entities
  (created by deep-prep agents for *meetings*), never the calendar directly. When
  no briefs were found within its 20-minute wait, it fed Claude an empty list and
  Claude emitted "Clear calendar today." even on days with a full calendar.
- Had a doubled-log bug (StreamHandler + FileHandler writing the same lines) and
  used the wrong Telegram flag (`--thread-id` instead of `--topic`).

## Replacement

**Cotinga is now the single source of the morning brief.** It fetches all
calendars (primary, Tontitos, Family), sends one accurate shallow brief at 05:00,
and spawns deep-prep agents that send their own follow-ups for meetings.

## To fully remove later

The launchd job has already been booted out and the `~/Library/LaunchAgents`
symlink removed. The `.py`, `.plist`, and `install.sh` are kept only for history;
they can be deleted once you're confident the consolidation is stable.
