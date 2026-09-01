---
name: stream-transcript
description: Start or stop a live transcript stream for an in-progress recording. On start, asks what the agent should watch for and under what write posture, launches the chunk tailer against the currently-growing Audio Hijack recording, and arms a Monitor so transcript chunks arrive in the session as they are spoken. On stop, ends the stream, reports where the live chunks and the authoritative transcript live, and offers to run /analyze-meeting. Detect-only with respect to the recorder — it never starts or stops Audio Hijack itself.
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
half is unchanged and still owned by Tyto + [`analyze-meeting`](../analyze-meeting/SKILL.md):
full-file, diarized, stored in Neotoma, reconciled against the graph.

## Relationship to the rest of the pipeline

| | Live stream (this skill) | Post-hoc (Tyto → `analyze-meeting`) |
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

**`NONE` may be a false negative — check before believing it.**
`find_growing_recording()` probes the newest track's size across a ~3s window,
and Audio Hijack writes in buffered flushes, so a probe can land entirely
between flushes on a recording that is very much live. This has misfired on
real sessions. Before concluding nothing is recording, look for a
recently-modified track:

```bash
ls -lt "$HOME/Documents/data/recordings"/*mic.mp4 \
       "$HOME/Documents/data/recordings"/*system.mp4 2>/dev/null | head -5
```

If one was modified in the last minute or two, the operator **is** recording —
skip the wait below and pass that file explicitly at step 3 with
`--file <path>`, which bypasses auto-detect entirely.

**If nothing recent is there either, do not stop and hand the problem back.** The operator has
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
  *propose* corrections to existing ones. Reconciled at stop by `analyze-meeting`.

Under **neither** posture may a live chunk silently overwrite an established
field on an existing entity. Neotoma is append-only, so a wrong value is
recoverable — but it is live in the graph until corrected, and other agents
(Turdus drafting mail, for one) read that graph in the meantime. Chunks are
sequential, not overlapping: a later chunk supplies surrounding context but never
a better take of the same audio. Only the stop-time pass can re-hear a sentence.

### 3. Launch the tailer

Pass `--out` explicitly. The tailer also prints the JSONL path on stdout, but
stdout is redirected to `/tmp/livetail.out` here — so naming the path yourself
is what lets step 4 tail a path you already know, with nothing to parse back.

```bash
# auto-detect the growing recording
cd "$ATELES_REPO" && nohup execution/venv/bin/python execution/scripts/live_transcript_tail.py \
  --interval 30 --follow --out /tmp/livetail.jsonl > /tmp/livetail.out 2> /tmp/livetail.err &

# or name the recording explicitly, when auto-detect false-negatived (step 1)
cd "$ATELES_REPO" && nohup execution/venv/bin/python execution/scripts/live_transcript_tail.py \
  --file "$HOME/Documents/data/recordings/<REC>.mp4" \
  --interval 30 --follow --out /tmp/livetail.jsonl > /tmp/livetail.out 2> /tmp/livetail.err &
```

`--file` skips detection altogether — the path is used as given, checked only
for existence — so it is the recovery whenever the probe misses a live
recording.

`--interval 30` is a reasonable default: shorter means more, worse fragments;
longer means staler context.

Confirm the tailer came up before arming the Monitor — a failed launch is
otherwise indistinguishable from a quiet meeting:

```bash
sleep 2; cat /tmp/livetail.out; echo "--- stderr ---"; tail -5 /tmp/livetail.err
```

If `/tmp/livetail.jsonl` does not appear, read `/tmp/livetail.err` — do not arm
the Monitor against a path nothing is writing.

Pass `--follow` (see [Pause and resume](#pause-and-resume-taking-a-break))
whenever the operator might take a break. It is the difference between a break
ending the stream and a break being a break.

### 3b. The silence gate

Whisper **does not return empty on silence — it fabricates.** Observed on real
silent audio from this machine: "Bon Appetit!", "thank you for watching",
"please subscribe", and fluent sentences in Japanese, Korean, and Ukrainian.
Each one also costs an API call.

So the tailer measures each slice's **sustained RMS before transcribing** and
skips the call entirely below a threshold. Level-gating is what actually fixes
this; filtering the output afterwards cannot, because the fabrications are an
open-ended set in arbitrary languages — you would be pattern-matching against
every subtitle cliché in every language Whisper knows, forever, and still
missing new ones.

| | |
|---|---|
| Default threshold | **-50 dB**, env `LIVE_TRANSCRIPT_SILENCE_THRESHOLD_DB` or `--silence-threshold-db` |
| Statistic | 95th percentile of ffmpeg's windowed RMS |
| On skip | JSONL gets `{"silence": true, "skipped": "below_threshold", "rms_db": -58.2}` |
| On measurement failure | **Transcribes anyway** — never drop audio because the meter broke |

Skipped slices advance the cursor normally and do **not** count toward the
consecutive-failure kill switch.

**Why the 95th percentile and not the median or the peak.** Measured across 39
labelled chunks of a real session:

- **Median fails.** A speaker who pauses between sentences leaves a 35s window
  with a median of -75 to -82 dB — identical to true silence. Gating on the
  median would have discarded most of a real meeting.
- **Peak fails.** Transient clicks push genuinely silent windows to -22 dB,
  indistinguishable from speech.
- **p95 works.** It asks "was there sustained energy in the loudest ~5% of this
  window", which is what "someone spoke at some point in here" actually means.

On that session the separation at p95 was: real speech **-24 to -38 dB** (single
quietest real chunk -46 dB), hallucinated silence **-52 to -57 dB**. -50 dB was
the only threshold tested that skipped zero real speech and passed zero
hallucinations. Note this is measured on the **mic** track; a different mic,
gain, or room may shift the range, so re-measure before assuming the default
transfers. If real speech starts getting skipped, lower the threshold.

### Pause and resume (taking a break)

With `--follow`, **stopping the recorder is the supported way to take a break.**
The operator stops Audio Hijack, does something else, starts it again, and the
transcript continues in the same JSONL — no new session, no manual restart.

| Event | JSONL line | What it means |
|---|---|---|
| Break starts | `{"event": "paused", "t": …}` | **A break, NOT end-of-meeting** |
| Break ends | `{"event": "resumed", "t": …, "file": …}` | The operator is back |

**Treat `paused` as a break, not as the meeting ending.** Do not run the `stop`
sequence, do not offer `analyze-meeting`, do not summarize as though it is over.
Hold context and wait. On `resumed`, carry on with the same watch list and write
posture — the operator should be able to just start talking.

Mechanics worth knowing:

- **Resume re-slices from second zero of the new file**, not from the moment of
  detection. The operator starts talking the instant they hit record; anything
  else would drop exactly those words. The first post-resume chunk is therefore
  often longer than the interval — that is the point, not a defect.
- Resume is polled every ~4s and triggers on **file appearance**, not confirmed
  growth (`find_growing_recording`'s 3s probe false-negatives on Audio Hijack's
  buffered writes). Measured detection latency: ~3s.
- It resumes on the **same track** — a paused `*mic.mp4` waits for a new
  `*mic.mp4` and ignores a new `*system.mp4`.
- The wait is bounded by `--follow-timeout-min` (default 30, env
  `LIVE_TRANSCRIPT_FOLLOW_TIMEOUT_MIN`). On timeout the tailer exits normally so
  the lifecycle watch fires.

Without `--follow`, behavior is unchanged: the tailer exits on stop.

### 4. Arm the Monitor

Tail the JSONL from the beginning, rendering chunks and surfacing transcription
errors:

```bash
tail -f -n +1 /tmp/livetail.jsonl | python3 -u -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try: r = json.loads(line)
    except Exception: print('MALFORMED: ' + line[:200]); continue
    if r.get('event') == 'paused':
        print('=== RECORDING PAUSED — operator is taking a break, NOT end of meeting')
    elif r.get('event') == 'resumed':
        print('=== RECORDING RESUMED — operator is back')
    elif r.get('event') == 'fatal_transcription_failures':
        print(f\"=== TAILER STOPPED: {r.get('consecutive_failures')} consecutive failures — {r.get('last_error')}\")
    elif r.get('ok'):
        if r.get('silence'): continue
        if r.get('filtered'):
            print(f\"[HALLUCINATION {r['filtered']}] {r.get('text','')[:60]}\")
            continue
        print(f\"[{r['start_s']:.0f}-{r['end_s']:.0f}s] {r.get('text','')}\")
    else:
        print(f\"[TRANSCRIPTION ERROR chunk {r.get('chunk')}] {r.get('error')}\")
"
```

The `event` branches are load-bearing: without them a pause and a resume are
invisible in the feed and a break looks exactly like the meeting going quiet.

**Never treat a `filtered` chunk as something the operator said.** It carries a
`filtered` reason and keeps its text so a false positive stays visible and
recoverable — print it distinctly, never as speech, and never drop the line.

Use `persistent: true` — a meeting outruns the default timeout.

**The error branch is not optional.** Without it, a failing transcription and a
quiet meeting look identical: silence. Any `grep` added to this pipeline needs
`--line-buffered`, or lines sit unflushed through a quiet stretch.

Record the Monitor's task id — `/stream-transcript stop` needs it.

### 4b. Arm the lifecycle watch (auto-stop)

The tailer exits on its own when the recording stops. Without a second watch,
nobody notices: the chunk Monitor just goes quiet, and quiet is indistinguishable
from a lull in the conversation.

Under `--follow` the tailer does **not** exit on a stop — it pauses. So this
watch fires only on a real ending (resume timeout, or the operator stopping the
stream), which is what you want: a break must not trigger the `stop` sequence.
The `paused` / `resumed` events arrive through the chunk Monitor instead.

Arm a `run_in_background` Bash wait on the tailer process. It fires exactly once,
when the tailer exits:

```bash
while pgrep -f live_transcript_tail >/dev/null; do sleep 5; done
echo "TAILER EXITED"; tail -3 /tmp/livetail.err
```

Use `run_in_background: true` (not Monitor) — this is a single notification on a
terminal condition, which is what background Bash is for. Monitor is for repeated
events.

The tailer's stderr names what happened:

| stderr line | Meaning | Response |
|---|---|---|
| `recording stopped — pausing, watching for resume` | `--follow`: operator is taking a break | **Do nothing.** Not end-of-meeting; hold context and wait |
| `recording resumed — following <file>` | `--follow`: operator is back | Resume as before, same watch list |
| `no resume within N min — exiting` | `--follow`: break outlasted the timeout | **Run the `stop` sequence** |
| `recording appears to have stopped — exiting` | Operator stopped Audio Hijack (no `--follow`) | **Run the `stop` sequence automatically** |
| `recording appears to have stopped — flushing final Ns slice` then `final slice written — exiting` | Same, with a trailing remnant shorter than one chunk | **Run the `stop` sequence automatically** — the last words are in the final chunk |
| `could not probe duration — recording ended?` | File vanished or became unreadable | Run `stop`; note the anomaly. Under `--follow` this pauses instead |
| `3 consecutive transcription failures — STOPPING` | Transcription is genuinely broken | Do **not** treat as meeting-over; surface the errors — the recording may still be running. The tailer also exits **non-zero** and appends `{"event": "fatal_transcription_failures", ...}` to the JSONL, so a watcher can detect this without reading stderr |

The first three lines only ever appear with `--follow`. **Only the lines marked
"run the `stop` sequence" end the meeting** — a pause does not.

Neither kind of silence counts toward the failure streak. A quiet interval that
reached Whisper is written as `{"ok": true, "text": "", "silence": true}`; one
skipped before transcription adds `"skipped": "below_threshold"` and `rms_db`.
Both render as nothing, so a mid-meeting lull cannot kill the tailer.

**On a terminal state — and only these — run the `/stream-transcript stop`
sequence without being asked:**

- `recording appears to have stopped — exiting`
- `recording appears to have stopped — flushing final Ns slice` → `final slice written — exiting`
- `no resume within N min — exiting`
- `could not probe duration — recording ended?` **without** `--follow`

**`paused` and `resumed` are explicitly NOT terminal.** Neither is
`5 consecutive failures — stopping`: that is a broken transcription path, not a
finished meeting — surface the errors and say the recording may still be running.

On a terminal state, tell the operator streaming ended because recording stopped. This is
the auto-stop: the operator stops Audio Hijack and the handoff happens on its
own, rather than the feed dying silently mid-meeting.

Do not auto-run `analyze-meeting` — stopping is mechanical, but spending a heavy
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
p='/tmp/livetail.jsonl'
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
> reporting a transcript as merely "pending", and offer to run `/analyze-meeting`
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

### 4. Offer `analyze-meeting`

Once the authoritative transcript exists:

> Full transcript is stored as `<entity_id>`. Run `/analyze-meeting <entity_id>`
> to metabolize it — graph reconciliation, tasks, sub-analyses, follow-up
> recommendations?

Offer; do not auto-invoke. `analyze-meeting` is a heavy, multi-phase run and it
is the operator's call whether to spend it now.

If the write posture was **`provisional`**, say so here explicitly and list what
was created. `analyze-meeting` Phase 5 reconciles against existing entities and
is the right place for those provisional rows to be confirmed or corrected.

---

## Failure modes

| Symptom | Cause | Response |
|---|---|---|
| `no active recording found — start recording first, or pass --file` | Nothing *appeared* to grow during the ~3s probe — either nothing is recording, or the probe landed between Audio Hijack's buffered flushes | Check the watch dir for a recently-modified `*mic.mp4` / `*system.mp4`. If one exists, relaunch with `--file <path>`; only if none does, the operator starts the recorder |
| Chunks stop arriving | Recording ended, or tailer died | Check `/tmp/livetail.err` |
| 3 consecutive failures | Tailer self-stops by design, exits non-zero, writes `event: fatal_transcription_failures` | Read the error lines in the JSONL (silence is excluded from this count). Tune with `--max-consecutive-failures` / `LIVE_TRANSCRIPT_MAX_CONSECUTIVE_FAILURES` |
| `no usable Python interpreter for transcribe_audio.py — refusing to start` | Neither `execution/venv` nor `.venv` can import the transcriber's dependencies — the usual cause in a git worktree | Create `execution/venv`, or set `LIVE_TRANSCRIPT_PYTHON` to an interpreter that can. The tailer now refuses to start rather than failing every chunk silently |
| Garbled text on non-speech | Whisper straining on music/noise | Expected; not a defect |
| Operator's voice absent, agent's voice present | The **system** track was selected — that is the computer's OUTPUT | Startup now logs the selected track and prints a loud `WARNING: SYSTEM/REMOTE TRACK` block. Auto-detect prefers `mic` when both grow; relaunch with `--file '<session> mic.mp4'` |
| Subtitle boilerplate ("thank you for watching", "please subscribe") or unprompted Japanese/Korean/Ukrainian/Georgian | Whisper hallucinating on noise | Two defences. The loudness gate skips most before the API call; anything that gets past it is caught by the **post-transcription filter** and marked `"filtered": "<reason>"` in the JSONL. Filtered chunks keep their text — treat them as non-speech, but they stay reviewable |
| A chunk you know was real speech carries `"filtered"` | Filter false positive | Report it with the text and `rms_db`. The chunk is NOT lost — it is in the JSONL with its `filtered_detail`. `--no-hallucination-filter` turns the marking off |
| Real speech missing, JSONL shows `"skipped": "below_threshold"` | Threshold too aggressive for this mic/room | **Lower** `LIVE_TRANSCRIPT_SILENCE_THRESHOLD_DB`; check the logged `rms_db` against the -50 dB default |
| Stream ends when the operator takes a break | `--follow` not passed | Relaunch with `--follow` |
| Nothing after `paused` | Break outlasted `--follow-timeout-min`, or the resumed recording is a different track | Check `/tmp/livetail.err` for the timeout line |

## Related

- [`record_meeting`](../record_meeting/SKILL.md) — BlackHole capture path and the
  recording-disclosure ladder, which applies unchanged to Audio Hijack capture
  (local capture carries **no** built-in notice)
- [`analyze-meeting`](../analyze-meeting/SKILL.md) — the authoritative pass
- `execution/scripts/live_transcript_tail.py` — the tailer
