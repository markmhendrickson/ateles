# Local speaker diarization

Meeting capture uses FluidAudio for speaker segmentation and Parakeet v3 for
timestamped transcription on Apple Silicon. Single-channel memos keep local whisper.cpp
for the text and are also diarized by the same offline FluidAudio stage; the
transcript's first line is a header that leads with the outcome, for example
`[Speakers: 1, detected locally by FluidAudio offline (build adb146b0)]`. A
diarization failure never blocks the transcript: the header then names a fixed
failure category (`cli not found`, `timed out`, `exited with error` or
`unreadable output`) and the detail goes to the log only. With more than one
speaker, whisper is re-run with timestamps (and screened like the first pass) to
attribute turns, shown as `[m:ss–m:ss] [S1]`. The header and turn prefixes are not
speech: `lib/transcript_clarity.py` strips them before any length or rate check.
The stage is bounded by four times the audio length (60 s to 900 s). A retry reuses
the pending-store result, so the header does not change between attempts.
`--no-diarize` / `RECORD_MEETING_DIARIZE=0` skip it and add no header. No API credential is needed.

Install the [FluidAudio CLI](https://github.com/FluidInference/FluidAudio) from a
reviewed release, following its installation instructions. Put `fluidaudiocli` on
the execution environment's `PATH`, or set `TRANSCRIBE_FLUID_CLI` to its absolute
path. Download the required diarization and Parakeet v3 models during setup;
inference then runs locally. A missing tool/model, absent output, invalid
timestamps, or untimed ASR result fails the requested operation. It never falls
back to cloud transcription or a transcript without speaker separation.

```sh
python3 execution/scripts/transcribe_audio.py recording.m4a --diarize --language es
python3 execution/scripts/transcribe_audio.py system.m4a --mic-file mic.m4a
```

`--backend fluid` selects the same local route explicitly. `--no-store` writes
the transcript and raw JSON sidecars without uploading to Neotoma. Normal stores
retain the existing consent and provenance requirements. Use
`TRANSCRIBE_FLUID_TIMEOUT` to set the subprocess deadline in seconds (default
3600). The existing `TRANSCRIBE_FFMPEG` override is used for separate channels.

The transcript assigns each timed word to all overlapping speaker segments.
Words crossing a speaker boundary or simultaneous speakers retain an ambiguous
label; words outside every speech segment remain `Unassigned`. The raw JSON
preserves the ASR, diarization output and aligned words for review. Model speaker
IDs are anonymous and do not establish a person's identity. Separate physical
channels and paired files keep their channel labels, even when they contain
similar voices. This does not infer a human name from a channel.

Cloud backends remain available only by explicit backend selection. Their
credentials never influence default routing. An explicit plain local backend or
`RECORD_MEETING_DIARIZE=0` disables automatic diarization for single-speaker mono files.
Paired and multichannel recordings reject plain transcription overrides rather
than omit or downmix a track.
Tyto retries failures
as the same requested operation, retaining both tracks instead of dropping a
microphone track or silently switching to plain ASR.
