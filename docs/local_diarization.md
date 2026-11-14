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

## Two-track calls (`--mic-file`)

A call recorded as two tracks (system = remote side, mic = operator) is merged by
SEGMENT, never by word (`execution/scripts/two_track_transcribe.py`, ported from
the recordings desk's hand method). One automated step:

1. each track is transcribed on its own by chunked local whisper
   (`chunked_whisper.py`): normalised (highpass + dynaudnorm, so a quiet mic is not
   suppressed), cut into 2-minute chunks with a fresh decoder each, a silent chunk
   skipped by the calibrated silence gate, and each chunk seam re-transcribed over
   a 28 s window to repair cut words. A repeat check runs per chunk (a phrase of 1
   to 6 words repeated back to back 4 or more times is a collapse): the chunk is
   re-run once without context; a loop that survives is counted in the header and
   collapsed after same-speaker segments are joined. The output screen judges each
   chunk, with the plausible languages taken from the operator's `locale_profile`
   (`session_language.py`). A chunk it rejects is never dropped silently: its text
   and reason stay in the raw evidence, the transcript shows a timed
   `[SPEECH WITHHELD ...]` marker at its place, and the header counts it. Silent
   chunks are counted in the header too. The model defaults to `ggml-medium.bin`;
   `TRANSCRIBE_WHISPER_MODEL` or `TRANSCRIBE_WHISPER_MODEL_NAME` override it. A
   whisper-cli child is killed with its parent, so Tyto's outer timeout cannot
   leave one running;
2. FluidAudio offline diarizes the two tracks mixed. Each cluster is mapped to the
   track that carries its energy, each track measured against its own speech
   level. Remote lines are labelled `[System]` or, with more than one remote
   cluster, `[System/S1]`, `[System/S2]`; the operator is `[Mic]`. No name is
   inferred;
3. a segment is dropped as cross-talk only when its words (at least three, mostly
   found) also appear in the other track's ASR over the same stretch AND it is the
   quieter of the two there. Level alone never drops a segment: the operator's mic
   is quiet, and his real speech looks far below its own track. Each dropped
   segment keeps its text, match share and level margin in the raw evidence, and
   the header counts them;
4. whole segments are ordered by start time, consecutive ones of one speaker are
   joined (`TRANSCRIBE_UTTERANCE_STITCH_SECONDS`), and each turn is
   `[m:ss–m:ss] [Label]`. The first line is the usual one-line `[Speakers: ...]`
   header: method, cluster count and mapping, that names are not established, and
   the withheld, silent, looping, cross-talk and collapsed-repeat counts. If
   diarization fails the header says so (or that it was not run) and remote lines
   are plain `[System]`; if levels cannot be measured the clusters are not mapped,
   the cross-talk check is skipped and the header says that.

`TRANSCRIBE_PAIRED_ASR=parakeet` keeps FluidAudio's Parakeet words as the text
source (their runs become segments and go through the same merge); the default is
`whisper`. The stored raw evidence holds each track's segments and chunk records,
and the merge record (turns, cluster mapping, dropped segments).

## Word alignment (Parakeet routes)

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
