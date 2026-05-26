---
name: find-technician-slot
description: "Finds the best calendar slots for a technician appointment using Google Calendar. Prefers first thing after morning workout (gym, yoga, strength training, or similar) or 4–6pm. Use when scheduling a technician, repair visit, installer, or when the user asks for the best time for an appointment or to find a slot."
triggers:
  - find technician slot
  - schedule technician
  - technician appointment
  - best time for appointment
  - find a slot
  - repair visit
  - installer appointment
  - /find-technician-slot
user_invocable: true
entity_id: ent_d0c33c9fe9672f2cb3aa53fb
---

# Find Technician Appointment Slot

Finds available times for a technician (or similar) appointment by checking the user's calendar. Prefers **morning right after workout** (gym, yoga, strength training, or similar) or **4–6pm**. Uses the **project-0-ateles-google-calendar** MCP; call its tools directly (do not list descriptor files for this workflow).

## Defaults

- **Window**: Tomorrow through the next 3 weeks (≈21 days). If the user specifies a range (e.g. "next 2 weeks", "between March 1 and March 15"), use that instead.
- **Timezone**: `Europe/Madrid` (Barcelona). Override only if the user specifies another timezone.
- **Appointment length**: 1 hour unless the user says otherwise (e.g. "2 hour window").
- **Buffer after workout**: Prefer slots starting 30 minutes after a morning workout ends.

## Workflow

1. **Get current time**
   - Call `get_current_time` with `timeZone: "Europe/Madrid"` so all later ranges use the correct "today" and "tomorrow".

2. **Set time range**
   - `timeMin`: Start of tomorrow (00:00) in Europe/Madrid.
   - `timeMax`: End of day at (today + 21 days), or the user-specified end date.
   - Format: `YYYY-MM-DDTHH:mm:ss` (no Z); the MCP uses the timeZone parameter when needed.

3. **Get free/busy**
   - Call `get_freebusy` with:
     - `calendars`: `[{ "id": "primary" }]` (or use calendar IDs from `list_calendars` if the user has multiple and you need to aggregate).
     - `timeMin`, `timeMax` from step 2.
     - `timeZone`: `"Europe/Madrid"`.
   - The response lists **busy** periods per calendar. Free slots are the gaps: between timeMin and the first busy start, between consecutive busy end and next busy start, and between last busy end and timeMax. Each gap that is at least the appointment length (e.g. 1 hour) is a candidate slot.

4. **Find morning workouts**
   - Call `search_events` (or `list_events`) with the same `timeMin`/`timeMax`/`timeZone`.
   - Search for **workout-related events** by running separate searches for: `"gym"`, `"yoga"`, `"strength"`, `"strength training"`, and `"workout"` (or `"training"`). Merge results and deduplicate by event id. This catches gym, yoga, strength training, and similar descriptors.
   - Collect each event's **end** time; "first thing after workout" = first free slot that starts ≥ 30 minutes after a workout end (e.g. workout ends 8:30 → prefer slots from 9:00 onward that day).
   - When labelling slots, use a short label from the event summary when possible (e.g. "After yoga", "After strength", "After gym").

5. **Define preferred windows**
   - **Morning-after-workout**: For each day that has a workout, take the workout end time + 30 minutes, then find the next free block that can fit the appointment length (default 1 hour).
   - **4–6pm**: For each day in the range, find free blocks that fall entirely within 16:00–18:00 (4–6pm) and can fit the appointment length.

6. **Rank and return**
   - Rank slots: (1) Morning-after-workout slots, (2) 4–6pm slots. Within each group, order by date then start time.
   - If the user asked for "a few options", return roughly 5–10; otherwise return all that fit preferences, or the top 3–5.
   - For each slot show: date, day of week, start–end time, and short label (e.g. "After yoga", "After strength", "After gym", or "4–6pm window").

## Output format

Present a short summary then a list, e.g.:

```markdown
**Best slots for a 1h technician appointment (next 3 weeks)**

| Date       | Time      | Note        |
|------------|-----------|-------------|
| Thu 27 Feb | 09:00–10:00 | After gym   |
| Thu 27 Feb | 09:30–10:30 | After strength |
| Thu 27 Feb | 16:00–17:00 | 4–6pm       |
...
```

If no preferred slots exist, say so and optionally list other free slots in the window (e.g. midday) so the user can choose.

## Optional: create the event

If the user says to "schedule it" or "put it on the calendar", after they pick a slot use the calendar MCP `create_event` with that start/end and a sensible title (e.g. "Technician visit" or the service they mentioned). Confirm the created event and time before finishing.

## Checklist

- [ ] Called `get_current_time` first (Europe/Madrid).
- [ ] Used tomorrow → +3 weeks (or user range) for timeMin/timeMax.
- [ ] Fetched free/busy and identified free blocks.
- [ ] Searched for gym, yoga, strength, strength training, workout (or similar) to get morning workout end times.
- [ ] Built morning-after-workout and 4–6pm slots; ranked and returned in a clear table.
- [ ] If user asked to schedule, created the event and confirmed.
