---
name: record_meeting
description: Start/stop meeting recording in background and transcribe+store on stop.
triggers:
  - meeting recording start
  - meeting recording stop
  - record meeting start
  - record meeting stop
  - /record_meeting start
  - /record_meeting stop
  - start
  - stop
user_invocable: true
entity_id: ent_b0976fda824a000ee984a5da
---

# Record meeting

Use this skill when the user wants command-style control of meeting recording.

## Default (no argument)

When the user invokes `/record_meeting` with no argument (or "toggle"): run `npm run record_meeting`. That **starts** recording if not running, or **stops** (and transcribes + Neotoma store) if running. If the user asks to stop **without** transcribing, run `RECORD_MEETING_SKIP_TRANSCRIBE=1 npm run record_meeting` (or set `RECORD_MEETING_SKIP_TRANSCRIBE=1` in `.env`) so the WAV is saved but `transcribe_audio.py` is not run.

## Guardrail

- Treat plain `start`/`stop` as meeting-recording commands only when the current thread is clearly about meeting transcription/recording.

## Commands

Run from repo root:

```bash
npm run record_meeting
RECORD_MEETING_SKIP_TRANSCRIBE=1 npm run record_meeting   # toggle stop: WAV only, no transcribe
RECORD_MEETING_SKIP_TRANSCRIBE=1 npm run record_meeting:stop
npm run record_meeting:start
npm run record_meeting:stop
npm run record_meeting:status
```

### Real-time transcription (chunked)

Set `RECORD_MEETING_REALTIME_INTERVAL=30` (or any seconds > 0) in `.env` or pass `--realtime-interval 30` directly to print a live partial transcript every N seconds while recording. Uses Whisper only (no diarization, no Neotoma store). The full diarized transcription still runs at stop as usual.

```bash
RECORD_MEETING_REALTIME_INTERVAL=30 npm run record_meeting:start
```

### Video capture + frame extraction

Screen recording starts automatically alongside audio when `ffmpeg` is available (disable with `RECORD_MEETING_VIDEO=0`). Defaults: 2fps, primary display (index 1). On stop, frames are extracted every 60s (override: `RECORD_MEETING_FRAME_INTERVAL`) to a `frames/` directory next to the video, and linked to the Neotoma `transcription` entity when a transcription entity ID is present.

```bash
# Manual frame extraction from an existing recording:
execution/venv/bin/python execution/scripts/extract_meeting_frames.py path/to/meeting.mp4
execution/venv/bin/python execution/scripts/extract_meeting_frames.py path/to/meeting.mp4 \
  --interval 30 --transcription-id ent_abc123
```

Env overrides:
- `RECORD_MEETING_VIDEO=0` — disable screen capture
- `RECORD_MEETING_VIDEO_SCREEN=1` — avfoundation screen index (default 1 = primary)
- `RECORD_MEETING_VIDEO_FPS=2` — recording framerate (default 2)
- `RECORD_MEETING_FRAME_INTERVAL=60` — seconds between extracted stills (default 60)

## Behavior

- `record_meeting` (no suffix)
  - **Toggle:** start if not running; stop (and transcribe + store) if running. With **`RECORD_MEETING_SKIP_TRANSCRIBE=1`**, a stop saves the WAV only (no `transcribe_audio.py`, no Neotoma `transcription` row); transcribe later with the command the script prints.
- `record_meeting:start`
  - Starts `record_meeting_audio.py` in background.
  - Uses existing defaults (BlackHole capture + default mic).
  - Records with **stereo channel separation** by default (`--separate-sources`: ch1=system audio, ch2=mic) for speaker diarization. Set `RECORD_MEETING_SEPARATE_SOURCES=0` to disable.
  - Starts **parallel screen recording** via `ffmpeg` (2fps, primary display) when ffmpeg is installed. Disable with `RECORD_MEETING_VIDEO=0`.
  - Starts **live transcription** thread if `RECORD_MEETING_REALTIME_INTERVAL` > 0.
- `record_meeting:stop`
  - Sends stop signal to recorder.
  - With **`RECORD_MEETING_SKIP_TRANSCRIBE=1`** (env or `.env`), stops after saving the WAV; skips everything below.
  - Reads saved audio path from recorder log.
  - Runs `transcribe_audio.py` on the saved audio.
  - **Speaker diarization** is enabled automatically when `ELEVENLABS_API_KEY` is set (passes `--diarize`). Falls back to plain Whisper transcription on failure. Set `RECORD_MEETING_DIARIZE=0` to force plain mode.
  - Prints the transcription in a clear block (--- TRANSCRIPTION --- ... --- END TRANSCRIPTION ---), then prints the **Neotoma `transcription` entity ID** and **audio WAV** path.
  - Stores the transcription as a `transcription` entity in Neotoma via `neotoma store` (combined entities + `--file-path` WAV). No Parquet write.
  - **Direct-to-prod transport (since 2026-05-13):** `transcribe_audio.py` calls the CLI with `--api-only --base-url $NEOTOMA_PROD_BASE_URL` (default `http://localhost:3180`) and inherits `NEOTOMA_BEARER_TOKEN` from the shell env (sourced from the ateles `.env`). It now performs an auth preflight and **fails loudly** if the bearer token is missing instead of silently falling back to the unauthenticated dev server (3080). Override via `NEOTOMA_PROD_BASE_URL` if you need to target a different host. The `http://localhost:3180/inspector/entities/<entity_id>` link reported in chat is therefore guaranteed to resolve when the script returned successfully.
  - **Auto-invoke `/analyze-meeting` (general):** After transcription completes successfully, **automatically invoke [`/analyze-meeting`](../analyze-meeting/SKILL.md)** with the transcript file path as the source, in the same turn. Do not wait for the user to ask. This produces a structured analysis (summary, decisions, action items, open questions), stages recap-email drafts (Neotoma `email_draft` + best-effort Gmail draft), and stages `proposed_github_issue` drafts for relevant repos with PII scrubbed.
    - Gate: skip when `RECORD_MEETING_AUTO_ANALYZE_MEETING=0` is set in env (default: on). Also skipped by `/analyze-meeting` itself when the transcript is empty / too short / clearly not a meeting (see that skill's "Skip silently" rule).
    - The analyze-meeting skill does NOT open real GitHub issues by default — it stages drafts. Set `MEETING_ANALYSIS_OPEN_GH_ISSUES=1` (or pass `--open-issues` when invoking manually) to actually open.
    - The two analyze skills are complementary and BOTH fire when the Neotoma heuristic below also triggers. They cross-link via the shared `transcription` and `contact` entities.
  - **Neotoma-related call detection (auto-analyze):** After transcription completes, scan the transcript text for Neotoma-related signal using this heuristic: the transcript qualifies if it contains the literal word "Neotoma" (case-insensitive), OR contains at least 2 of the following domain terms: `entity`, `schema`, `MCP`, `memory`, `observation`, `store`, `transcription`, `knowledge graph`, `feedback_analysis`. Apply this heuristic to the full transcript text returned by `transcribe_audio.py`.
    - If `RECORD_MEETING_AUTO_ANALYZE_NEOTOMA=1` is set in env and the heuristic fires: **additionally invoke `/analyze-neotoma-feedback`** with the transcript file path as the source, in the same turn. This runs alongside `/analyze-meeting` (above), not instead of it.
    - If the heuristic fires but the env var is not set: print a one-line suggestion after the stop summary: `> Neotoma-related call detected. Run /analyze-neotoma-feedback <transcript_path> to analyze feedback.` where `<transcript_path>` is the absolute path to the transcript (if `transcribe_audio.py` wrote one) or `last_meeting_transcription.txt` path.
    - If the heuristic does not fire: skip silently — do not mention detection to the user.
    - The heuristic runs only when transcription succeeded (not when `RECORD_MEETING_SKIP_TRANSCRIBE=1`).
  - **When reporting stop in chat:** Always include (1) the **audio WAV path**, (2) the **Neotoma `transcription` entity ID**, (3) the **transcription text**, and (4) the **video path and frames directory** if video was captured. Always link to the prod Inspector (`http://localhost:3180/inspector/entities/<entity_id>`) — if the script succeeded, the row is on prod. If the script aborted on the auth preflight, surface the error and ask the user to source the env file rather than fabricating an Inspector link.
  - **Legacy parquet:** One-off migration from ``$DATA_DIR/transcriptions/transcriptions.parquet``: run ``execution/venv/bin/python execution/scripts/migrate_transcriptions_parquet_to_neotoma.py`` (use ``--dry-run`` / ``--limit`` as needed).
  - **Repair basename merges:** If imports collapsed rows that shared ``audio_file_name``, run ``execution/venv/bin/python execution/scripts/repair_transcription_merge_duplicates.py`` (default: only duplicate-basename rows; ``--all-rows`` to re-key everything).
  - **Link transcription to people / feedback analysis (optional):** After Neotoma stores the ``transcription``, ``transcribe_audio.py`` can create ``REFERS_TO`` edges from that ``transcription`` to related entities (transcription → contact, transcription → ``feedback_analysis``). Three ways to pass targets (merged; CLI overrides sidecar overrides env for feedback_analysis id; contact lists are unioned in CLI → sidecar → env order):
    1. **Sidecar JSON** next to the WAV: ``<stem>_neotoma_relations.json`` (same directory). Example: ``{"relate_contact_entity_ids": ["ent_…"], "relate_feedback_analysis_entity_id": "ent_…"}``.
    2. **Environment** (sourced from repo ``.env`` when using ``meeting-recording-control.sh``): ``NEOTOMA_TRANSCRIPTION_CONTACT_ENTITY_IDS`` (comma-separated ``ent_`` ids) and ``NEOTOMA_TRANSCRIPTION_FEEDBACK_ANALYSIS_ENTITY_ID``.
    3. **CLI** (advanced): ``transcribe_audio.py path.wav --relate-contact-entity-id ent_… --relate-feedback-analysis-entity-id ent_…`` (repeat ``--relate-contact-entity-id`` for multiple contacts). Add ``--relate-verbose`` to print each relationship attempt.
    4. **Backfill on an existing ``transcription``:** ``execution/venv/bin/python execution/scripts/link_transcription_neotoma_relations.py --transcription-id ent_… --contact-entity-id ent_… --feedback-analysis-entity-id ent_…`` (with prod Neotoma env same as transcribe).
- `record_meeting:status`
  - Reports whether recorder is currently running.

## Device setup (macOS)

1. **System audio:** Install [BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole) (e.g. `brew install blackhole-2ch`), **reboot**, then in **Audio MIDI Setup** create a **Multi-Output Device** that includes your speakers/headphones **and** BlackHole so meeting apps play into BlackHole.
2. **Python:** Use `execution/venv` with `numpy` and `sounddevice` (see `execution/scripts/requirements.txt`). If `venv/bin/pip` fails with a bad interpreter, run `execution/venv/bin/python3 -m pip install -r execution/scripts/requirements.txt`.
3. **Mic / device overrides (optional):** Set in repo `.env` (sourced by `meeting-recording-control.sh`): `RECORD_MEETING_DEVICE` (substring for system capture, default `BlackHole`), `RECORD_MEETING_MIC` (mic substring; empty string = no mic). If unset, the mic defaults to the **current PortAudio default input** (e.g. built-in mic).
4. **Transcription:** `OPENAI_API_KEY` in `.env` for Whisper fallback. With `ELEVENLABS_API_KEY` set, `transcribe_audio.py` uses ElevenLabs speech-to-text by default: **multichannel** for stereo merges `[System]` / `[Mic]` **in time order** when word timestamps exist (otherwise channel order: system then mic). **Diarization** for mono. Set `RECORD_MEETING_DIARIZE=0` or run `transcribe_audio.py --no-diarize` to force Whisper only.

## Implementation detail

Controller script:

`execution/scripts/meeting-recording-control.sh`

NPM scripts:

- `record_meeting` — toggle (default)
- `record_meeting:start`
- `record_meeting:stop`
- `record_meeting:status`
