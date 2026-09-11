---
name: record_meeting
description: Start/stop meeting recording (Audio Hijack) and transcribe+store via Tyto.
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

Recording runs through **Audio Hijack** (local BlackHole capture is retired). The
`audio_hijack_control.sh` script starts/stops the Audio Hijack session named
`Tyto` via AppleScript; Audio Hijack writes paired `*remote*`/`*mic*` files into
its output folder, and the **Tyto daemon** watches that folder and does the
transcription + `/analyze-meeting` automatically. The same script backs the Strix
menu-bar toggle, so the chat command and the menu bar share one mechanism.

## Default (no argument)

When the user invokes `/record_meeting` with no argument (or "toggle"): run
`bash execution/scripts/audio_hijack_control.sh toggle`. That **starts** the
Audio Hijack session if stopped, or **stops** it if running. Transcription and
analysis happen downstream in Tyto when the recording file settles — this skill
does not transcribe inline.

## Guardrail

- Treat plain `start`/`stop` as meeting-recording commands only when the current thread is clearly about meeting transcription/recording.

## Recording disclosure & consent (legal guardrail)

The operator is always a party to recordings made via this skill (operator-triggered, operator present). That satisfies US federal one-party consent and Spain's Penal Code Art. 197 (no interception of others' communications). The residual gaps are **US all-party-consent states** and EU transparency. Apply this disclosure ladder, best to worst:

1. **Platform-native recording (preferred).** If the call is on Zoom/Meet/Teams, prefer the platform's own Record button: it auto-discloses to everyone — on-screen banner + consent splash + an audio announcement to phone/dial-in participants. This is the only method that reaches dial-ins, and it sidesteps the disclosure problem entirely. Route the resulting file through Tyto's `TYTO_NATIVE_RECORDINGS_DIR` (stamped `capture_method=platform_native`) so the transcribe+analyze pipeline is identical. When the operator is on a supported platform, **suggest native recording instead of the local Audio Hijack capture.**
2. **Verbal announcement.** "I'm recording this for my notes." Reaches everyone; continuing to talk after it is valid implied consent in every state.
3. **In-meeting chat message.** Valid + creates a written record, BUT misses (a) phone/dial-in participants (they never see chat) and (b) late joiners (most platforms don't show pre-arrival messages). If using chat, repeat for late joiners and announce verbally for any dial-in.
4. **Booking-page disclaimer only** (e.g. markmhendrickson.com/meet): prior notice, not contemporaneous — leaves a residual gap in all-party states (esp. California) for anyone who didn't book through the page (forwarded invite, booked by someone else).

On `record_meeting:start`, after the recorder starts (Audio Hijack local capture = `capture_method=audio_hijack_system`, which carries **no** built-in disclosure), ALWAYS print:

```
⚠️  Local capture has no built-in recording notice. If on Zoom/Meet/Teams,
    prefer the platform Record button (auto-discloses to all, incl. dial-ins).
    Otherwise announce recording — required in CA, CT, FL, IL, MD, MA, MI, MT,
    NV, NH, OR, PA, WA and good practice everywhere. Booking-page disclaimer
    covers participants who booked via /meet; announce verbally for anyone who
    didn't (forwarded invite, dial-in).
```

This is a reminder, not a blocking prompt — do not gate the start on a confirmation.

**Hard refusal:** do NOT auto-start or schedule recording of a call the operator is not a party to (e.g. capturing a third-party call autonomously, or starting before the operator has joined). Non-party recording loses both the US one-party basis and the Art. 197 safe harbor. If asked, refuse and explain.

## Commands

Run from repo root (all via the shared Audio Hijack control surface):

```bash
bash execution/scripts/audio_hijack_control.sh toggle   # start if stopped, stop if running (default)
bash execution/scripts/audio_hijack_control.sh start    # begin recording
bash execution/scripts/audio_hijack_control.sh stop      # end recording
bash execution/scripts/audio_hijack_control.sh status    # "running" or "stopped"
```

Env:
- `AUDIO_HIJACK_SESSION` — session name to control (default `Tyto`).
- `AUDIO_HIJACK_APP` — app name for the AppleScript `tell` (default `Audio Hijack`).

Audio Hijack must be running with a session named `$AUDIO_HIJACK_SESSION` whose
recorder block writes the `*remote*` (far-end / system) and `*mic*` (you) files
that Tyto consumes.

## Behavior

- `toggle` (default) — start the Audio Hijack session if stopped; stop it if running.
- `start` — start the Audio Hijack session (begin recording). Print the consent reminder above.
- `stop` — stop the Audio Hijack session. Audio Hijack finalizes the `*remote*`/`*mic*` files; the **Tyto daemon** then transcribes (`transcribe_audio.py`, diarized when `ELEVENLABS_API_KEY` is set) and auto-invokes `/analyze-meeting`. This skill does not transcribe inline.
- `status` — print whether the Audio Hijack session is currently recording.

### Downstream (handled by Tyto, not this skill)

- Tyto watches `TYTO_RECORDINGS_DIR` (Audio Hijack output, stamped `capture_method=audio_hijack_system`) and optionally `TYTO_NATIVE_RECORDINGS_DIR` (Zoom/Meet/Teams native recordings, stamped `capture_method=platform_native`).
- On a settled recording, Tyto runs `transcribe_audio.py` (passing `--capture-method`) → stores a Neotoma `transcription` entity → auto-invokes `/analyze-meeting`.
- The `transcription` entity ID and prod Inspector link (`http://localhost:3180/inspector/entities/<entity_id>`) come from Tyto's run, not from this skill.

## Setup (macOS)

1. **Audio Hijack:** Install Audio Hijack, create a session named `Tyto` that captures the meeting app's audio (system/far-end) on one recorder block and your mic on another, writing files whose names contain `remote`/`system` (far end) and `mic` (you). Audio Hijack must expose AppleScript (it does by default) so `audio_hijack_control.sh` can `start`/`stop` the session.
2. **Tyto daemon:** Point `TYTO_RECORDINGS_DIR` at the Audio Hijack output folder (default `$RECORD_MEETING_DIR` or `~/Documents/data/recordings`). Optionally set `TYTO_NATIVE_RECORDINGS_DIR` for platform-native recordings.
3. **Transcription keys:** `OPENAI_API_KEY` for Whisper fallback; `ELEVENLABS_API_KEY` to enable diarization / multichannel merge.

## Implementation detail

Control surface (shared with Strix menu-bar toggle):

`execution/scripts/audio_hijack_control.sh`  (start | stop | status | toggle)

**Deprecated (retired BlackHole path, kept for reference only):**
`execution/scripts/meeting-recording-control.sh` and
`execution/scripts/record_meeting_audio.py` — no longer wired to Strix or this skill.
