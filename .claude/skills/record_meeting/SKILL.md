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

Recording runs through **Audio Hijack**. Start and stop the configured recording session in Audio Hijack's UI; the previously documented `execution/scripts/audio_hijack_control.sh` is not present in the supported repository and must not be invoked or described as installed. Audio Hijack writes paired `*remote*`/`*mic*` files into its output folder, and the **Tyto daemon** watches that folder and performs transcription + `/analyze-meeting` automatically. Do not infer a working chat or Strix control integration from this skill.

## Default (no argument)

When the user invokes `/record_meeting` with no argument (or "toggle"), inspect the configured Audio Hijack recording session and use its visible start/stop control only when supported app control is available. Otherwise report that the recording UI must be operated directly; do not invent a command or substitute another capture mechanism. Transcription and analysis happen downstream in Tyto when the recording file settles — this path does not transcribe inline.

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

Audio Hijack capture uses the application's visible recording controls for its configured session. Verify its current state before toggling and read back the resulting recording state. A supported programmatic Audio Hijack entrypoint is not installed in this repository.

An existing, separate local device-capture controller is available at:

```bash
bash execution/scripts/meeting-recording-control.sh status
bash execution/scripts/meeting-recording-control.sh start
bash execution/scripts/meeting-recording-control.sh stop
```

This controller launches `record_meeting_audio.py`; it does not control Audio Hijack. Use it only when that capture path is explicitly selected and the devices/configuration are verified. Do not switch the operator's Audio Hijack capture setup merely because this script exists. This repository has no root `package.json`, so `npm run record_meeting` is not a supported repo-root command.

## Behavior

- `toggle` (default) — use the verified visible Audio Hijack recording control if supported app control is available.
- `start` — start the Audio Hijack session (begin recording). Print the consent reminder above.
- `stop` — stop the Audio Hijack session. Audio Hijack finalizes the `*remote*`/`*mic*` files; the **Tyto daemon** then transcribes (`transcribe_audio.py`, using local FluidAudio diarization under agent_policy `ent_1cf60379aaf1537787e571a5`) and auto-invokes `/analyze-meeting`. This skill does not transcribe inline.
- `status` — inspect and report the Audio Hijack session's actual recording state.

### Downstream (handled by Tyto, not this skill)

- Tyto watches `TYTO_RECORDINGS_DIR` (Audio Hijack output, stamped `capture_method=audio_hijack_system`) and optionally `TYTO_NATIVE_RECORDINGS_DIR` (Zoom/Meet/Teams native recordings, stamped `capture_method=platform_native`).
- On a settled recording, Tyto runs `transcribe_audio.py` (passing `--capture-method`) → stores a Neotoma `transcription` entity → auto-invokes `/analyze-meeting`.
- The `transcription` entity ID and prod Inspector link (`http://localhost:3180/inspector/entities/<entity_id>`) come from Tyto's run, not from this skill.

## Setup (macOS)

1. **Audio Hijack:** Install Audio Hijack, create a session named `Tyto` that captures the meeting app's audio (system/far-end) on one recorder block and your mic on another, writing files whose names contain `remote`/`system` (far end) and `mic` (you). Use the application's supported visible controls; do not assume the absent `audio_hijack_control.sh` is available.
2. **Tyto daemon:** Point `TYTO_RECORDINGS_DIR` at the Audio Hijack output folder (default `$RECORD_MEETING_DIR` or `~/Documents/data/recordings`). Optionally set `TYTO_NATIVE_RECORDINGS_DIR` for platform-native recordings.
3. **Local transcription:** Install FluidAudio and its diarization/ASR models for supported Apple Silicon machines. Meeting diarization runs locally by default under agent_policy `ent_1cf60379aaf1537787e571a5` (operator ruling 2026-11-04). Preserve raw speaker/timestamp output, source transcript and deterministic channel identities. No provider API key is needed for the local route; do not automatically fall back to paid/cloud transcription or undiarized output when it fails. Diagnose the local error and report any remaining blocker.

## Implementation detail

Audio Hijack capture is controlled through the application; downstream processing is implemented by `execution/daemons/tyto/tyto.py` and `execution/scripts/transcribe_audio.py`.

The separate device-capture path is implemented by `execution/scripts/meeting-recording-control.sh` and `execution/scripts/record_meeting_audio.py`. Its availability does not establish that it is selected, that its audio devices are configured, or that it controls Strix/Audio Hijack.
