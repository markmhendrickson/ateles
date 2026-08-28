---
name: stream-transcript
description: Start or stop a live transcript stream for an in-progress recording. On start, asks what the agent should watch for and under what write posture, launches the chunk tailer against the currently-growing Audio Hijack recording, and arms a Monitor so transcript chunks arrive in the session as they are spoken. On stop, ends the stream, reports where the live chunks and the authoritative transcript live, and offers to run /process-meeting. Detect-only with respect to the recorder — it never starts or stops Audio Hijack itself.
triggers:
  - stream transcript start
  - stream transcript stop
  - start live transcript
  - stop live transcript
  - live transcript
  - /stream-transcript start
  - /stream-transcript stop
user_invocable: true
---

# Stream transcript

Feed a live meeting transcript into the current session while the meeting is
still happening, so the agent holds the conversation as context rather than
reading it afterwards.

This is the **live, advisory** half of meeting capture. The **authoritative**
half is unchanged and still owned by Tyto + [`process-meeting`](../process-meeting/SKILL.md):
full-file, diarized, stored in Neotoma, reconciled against the graph.

## Relationship to the rest of the pipeline

| | Live stream (this skill) | Post-hoc (Tyto → `process-meeting`) |
|---|---|---|
| When | While recording | After the file settles |
| Engine | Whisper only, no diarization | Best available, diarized |
| Boundaries | Arbitrary N-second cuts | Whole file |
| Speakers | System track only — **not the operator's mic** | Both tracks, merged |
| Neotoma | Nothing durable | Authoritative entities |
| Purpose | Orient the agent mid-meeting | Metabolize the meeting |

Live chunks are **provisional by construction**. They are cut mid-sentence, they
garble non-speech, and they never see the operator's own voice. Treat every live
claim as something the authoritative pass will restate better.

## Recorder control: detect only

This skill **does not start or stop Audio Hijack.** Audio Hijack 4 exposes
scripted control via App Intents (`AH4RunScriptIntent`), and this machine already
has a `Tyto` session plus `start_tyto_*` / `stop_tyto_test` scripts and Shortcuts
— but as of 2026-08-28 all three Shortcuts fail with "An unknown error occurred"
and change no recording state, likely because scripted control requires
confirmation (`confirmAH4RunScript:completion:`). Until that is resolved, the
operator starts and stops the recorder; this skill detects what is running.

Note: `shortcuts run` **exits 0 even when the shortcut fails**. Never trust its
exit code — parse its output.

---

## `/stream-transcript start`

### 1. Detect a growing recording

```bash
cd "$ATELES_REPO" && execution/venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "execution/scripts")
import live_transcript_tail as lt
rec = lt.find_growing_recording(lt.DEFAULT_DIR)
print(rec if rec else "NONE")
PY
```

**If a recording is already growing**, go straight to step 2.

**If it prints `NONE`, do not stop and hand the problem back.** The operator has
just asked to stream — they are about to start recording, or are reaching for it
now. Tell them to start the Audio Hijack `Tyto` session, then **wait for it**:

```bash
D="$HOME/Documents/data/recordings"
newest() { ls -t "$D"/*system.mp4 "$D"/*remote.mp4 2>/dev/null | head -1; }
F=$(newest); B=$([ -n "$F" ] && stat -f%z "$F" 2>/dev/null || echo 0)
for i in $(seq 1 120); do
  sleep 5
  C=$(newest)
  if [ -n "$C" ] && [ "$C" != "$F" ]; then echo "RECORDING STARTED: $C"; exit 0; fi
  if [ -n "$C" ]; then
    S=$(stat -f%z "$C" 2>/dev/null || echo 0)
    if [ "$S" -gt "$B" ]; then echo "RECORDING STARTED: $C"; exit 0; fi
  fi
done
echo "TIMEOUT: no recording started within 10 minutes"
```

Run it with `run_in_background: true` — one notification on a terminal
condition, which is exactly what background Bash is for. Note it fires on a
**new** file *or* on the newest file growing again, so it catches both a fresh
session and a resumed one.

Ask the two setup questions (step 2) **while the watch runs** rather than after.
By the time recording starts the answers are in hand and the tailer launches
immediately — no dead air, and no second round trip to the operator.

On `RECORDING STARTED`, continue to step 3 with that file. On `TIMEOUT`, say so
and ask whether they still want to stream; do not silently keep waiting.

Because this skill cannot start Audio Hijack itself (see *Recorder control*
above), waiting is the closest thing to it: the operator hits record once, and
everything else proceeds on its own.

### 2. Ask the two setup questions — in ONE batch

Ask both together; do not serialize them across turns.

**a. What should I watch for?**

Free text. Concrete watch items work; vague intent does not.

- Good: *"flag if they mention pricing"*, *"track commitments I make"*,
  *"note any file paths or error messages"*
- Poor: *"update the records"* — not actionable per chunk

Default when the operator has nothing specific: **passive accumulation** — hold
the transcript as context, surface nothing unprompted.

**b. Write posture?**

- **`surface-only`** (default) — never writes to Neotoma. Surfaces observations
  in-session.
- **`provisional`** — may create entities *explicitly marked provisional* and may
  *propose* corrections to existing ones. Reconciled at stop by `process-meeting`.

Under **neither** posture may a live chunk silently overwrite an established
field on an existing entity. Neotoma is append-only, so a wrong value is
recoverable — but it is live in the graph until corrected, and other agents
(Turdus drafting mail, for one) read that graph in the meantime. Chunks are
sequential, not overlapping: a later chunk supplies surrounding context but never
a better take of the same audio. Only the stop-time pass can re-hear a sentence.

### 3. Launch the tailer

```bash
cd "$ATELES_REPO" && nohup execution/venv/bin/python execution/scripts/live_transcript_tail.py \
  --interval 30 > /tmp/livetail.out 2> /tmp/livetail.err &
```

`--interval 30` is a reasonable default: shorter means more, worse fragments;
longer means staler context. The JSONL path is printed on stdout.

### 4. Arm the Monitor

Tail the JSONL from the beginning, rendering chunks and surfacing transcription
errors:

```bash
tail -f -n +1 "<jsonl_path>" | python3 -u -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try: r = json.loads(line)
    except Exception: print('MALFORMED: ' + line[:200]); continue
    if r.get('ok'):
        print(f\"[{r['start_s']:.0f}-{r['end_s']:.0f}s] {r.get('text','')}\")
    else:
        print(f\"[TRANSCRIPTION ERROR chunk {r.get('chunk')}] {r.get('error')}\")
"
```

Use `persistent: true` — a meeting outruns the default timeout.

**The error branch is not optional.** Without it, a failing transcription and a
quiet meeting look identical: silence. Any `grep` added to this pipeline needs
`--line-buffered`, or lines sit unflushed through a quiet stretch.

Record the Monitor's task id — `/stream-transcript stop` needs it.

### 4b. Arm the lifecycle watch (auto-stop)

The tailer exits on its own when the recording stops. Without a second watch,
nobody notices: the chunk Monitor just goes quiet, and quiet is indistinguishable
from a lull in the conversation.

Arm a `run_in_background` Bash wait on the tailer process. It fires exactly once,
when the tailer exits:

```bash
while pgrep -f live_transcript_tail >/dev/null; do sleep 5; done
echo "TAILER EXITED"; tail -3 /tmp/livetail.err
```

Use `run_in_background: true` (not Monitor) — this is a single notification on a
terminal condition, which is what background Bash is for. Monitor is for repeated
events.

The tailer's stderr names which of three exits happened:

| stderr line | Meaning | Response |
|---|---|---|
| `recording appears to have stopped — exiting` | Operator stopped Audio Hijack | **Run the `stop` sequence automatically** |
| `recording appears to have stopped — flushing final Ns slice` then `final slice written — exiting` | Same, with a trailing remnant shorter than one chunk | **Run the `stop` sequence automatically** — the last words are in the final chunk |
| `could not probe duration — recording ended?` | File vanished or became unreadable | Run `stop`; note the anomaly |
| `5 consecutive failures — stopping` | Transcription is genuinely broken | Do **not** treat as meeting-over; surface the errors — the recording may still be running |

Silence does **not** count toward that failure streak. A quiet interval is written
as `{"ok": true, "text": "", "silence": true}` and renders as nothing, so a
mid-meeting break cannot kill the tailer.

**On the first case, run the `/stream-transcript stop` sequence without being
asked** and tell the operator streaming ended because recording stopped. This is
the auto-stop: the operator stops Audio Hijack and the handoff happens on its
own, rather than the feed dying silently mid-meeting.

Do not auto-run `process-meeting` — stopping is mechanical, but spending a heavy
multi-phase analysis is the operator's call.

### 5. Confirm

Report: recording being tailed, chunk interval, JSONL path, watch list, write
posture. Note that the first chunk lands after roughly one interval plus
transcription time (~15s), and that the operator's own mic is not in the feed.

### While streaming

Apply the watch list to each chunk as it arrives. Surface only what matches, or
what is plainly significant. Do not narrate every chunk back — the operator is in
a meeting.

Send a PushNotification only for something worth acting on *now*. A chunk
arriving is not itself an event.

---

## `/stream-transcript stop`

Runs either when the operator asks, or **automatically** when the lifecycle watch
(step 4b) reports the recording stopped. Both paths do the same thing; the
automatic path additionally says why streaming ended.

### 1. Stop both halves

```bash
pkill -f live_transcript_tail
```

Then `TaskStop` the chunk Monitor's task id from start, and the lifecycle watch
if it has not already fired. A Monitor left armed on a dead file sits until the
session ends.

When triggered automatically the tailer has usually already exited — `pkill`
finding nothing is expected, not an error.

### 2. Report the live artifact

```bash
python3 -c "
import json,sys
p='<jsonl_path>'
ok=err=0; chunks=[]
for line in open(p, encoding='utf-8'):
    line=line.strip()
    if not line: continue
    r=json.loads(line)
    if r.get('ok'): ok+=1; chunks.append(r)
    else: err+=1
print(f'chunks: {ok} ok, {err} errors')
if chunks: print(f'covered: {chunks[0][\"start_s\"]:.0f}s - {chunks[-1][\"end_s\"]:.0f}s')
"
```

Give the operator the JSONL path and the counts. **Say explicitly that this is
the rough live transcript, not the authoritative one.** If errors > 0, show them
— a silent failure mid-meeting means the agent was blind for that stretch.

### 3. Locate the authoritative transcript

> **Known outage (2026-08-28): Tyto does not run.** It crash-loops on
> `Notifier.from_neotoma() got an unexpected keyword argument 'telegram_topic_env'`
> — a kwarg that never existed on that method, present in both checkouts'
> `tyto.py`. Until that is fixed, **no recording is transcribed automatically**,
> and the check below will keep finding nothing. Say so plainly rather than
> reporting a transcript as merely "pending", and offer to run `/process-meeting`
> directly on the recording file, which does not depend on Tyto. Tracked as task
> `ent_ac9843d5c4807c927e02694f`. Delete this note once Tyto runs again.

Tyto waits `SETTLE_SECS` (default 8) after the file stops growing, then
transcribes and stores a `transcription` entity.

So at stop time the authoritative transcript **usually does not exist yet.** Do
not claim it does. Check:

```bash
# Is the recording actually finished?
F="<recording_path>"; s1=$(stat -f%z "$F"); sleep 4; s2=$(stat -f%z "$F")
[ "$s2" -gt "$s1" ] && echo "STILL RECORDING" || echo "finished"
```

- **Still recording** — the operator stopped the stream but not the recorder.
  Say so; Tyto will process it when they stop.
- **Finished** — Tyto should pick it up shortly. Offer to check back, or check
  for a `transcription` entity whose `audio_file_name` matches.

Never assert a transcript exists without verifying, and never fabricate an
Inspector link.

### 4. Offer `process-meeting`

Once the authoritative transcript exists:

> Full transcript is stored as `<entity_id>`. Run `/process-meeting <entity_id>`
> to metabolize it — graph reconciliation, tasks, sub-analyses, follow-up
> recommendations?

Offer; do not auto-invoke. `process-meeting` is a heavy, multi-phase run and it
is the operator's call whether to spend it now.

If the write posture was **`provisional`**, say so here explicitly and list what
was created. `process-meeting` Phase 5 reconciles against existing entities and
is the right place for those provisional rows to be confirmed or corrected.

---

## Failure modes

| Symptom | Cause | Response |
|---|---|---|
| `no active recording found` | Nothing growing in the watch dir | Operator starts the recorder |
| Chunks stop arriving | Recording ended, or tailer died | Check `/tmp/livetail.err` |
| 5 consecutive failures | Tailer self-stops by design | Read the error lines in the JSONL (silence is excluded from this count) |
| `ModuleNotFoundError: config` | `execution/scripts/config.py` missing | Untracked + gitignored; copy from a worktree |
| Garbled text on non-speech | Whisper straining on music/noise | Expected; not a defect |
| Operator's voice absent | Only the system track is sliced | By design; see the follow-up task |

## Related

- [`record_meeting`](../record_meeting/SKILL.md) — BlackHole capture path and the
  recording-disclosure ladder, which applies unchanged to Audio Hijack capture
  (local capture carries **no** built-in notice)
- [`process-meeting`](../process-meeting/SKILL.md) — the authoritative pass
- `execution/scripts/live_transcript_tail.py` — the tailer
