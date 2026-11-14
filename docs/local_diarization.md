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
   suppressed), cut into 2-minute chunks with a fresh decoder each, and each chunk
   seam re-transcribed over a 28 s window to repair cut words. A chunk is skipped
   as silent only when fewer than half a second of its 100 ms frames are above
   -60 dB; a chunk-wide percentile would skip a listener's few seconds of speech.
   A repeat check runs per chunk (a phrase of 1 to 6 words repeated back to back 4
   or more times, or a sentence-length phrase of up to 40 words repeated 3 or more
   times): the chunk is re-run once without context; a loop that survives is named
   in the header and collapsed after same-speaker segments are joined. The output
   screen judges each chunk, with the plausible languages taken from the
   operator's `locale_profile` (`session_language.py`). Nothing is dropped
   silently: a chunk the screen rejects keeps its text and reason in the raw
   evidence, and a withheld chunk, a silent chunk, an audible chunk for which
   whisper returned no words, every removed cross-talk segment and every
   dropped bare tag (such as `(inaudible)`; a segment with a letter or a digit,
   like `$300.` or `2025.`, is speech and is kept) each get a
   timed marker in the body (`[SPEECH WITHHELD ...]`, `[SKIPPED AS SILENT ...]`,
   `[NO WORDS RECOGNISED ...]`, `[CROSS-TALK REMOVED ...]`, `[NON-SPEECH TAG DROPPED ...]`) and a track and time
   span in the header. The raw-evidence validator requires all of it. The model
   defaults to `ggml-medium.bin`; `TRANSCRIBE_WHISPER_MODEL` or
   `TRANSCRIBE_WHISPER_MODEL_NAME` override it. whisper-cli, ffmpeg and FluidAudio
   are killed with their parent, so Tyto's outer timeout cannot leave one
   running. Seam repair keeps the words it replaces in the seam's record;
2. FluidAudio offline diarizes the two tracks mixed. Each cluster is mapped to the
   track that carries its energy, each track measured against its own speech
   level. Remote lines are labelled `[System]` or, with more than one remote
   cluster, `[System/S1]`, `[System/S2]`; the operator is `[Mic]`. No name is
   inferred;
3. cross-talk is decided once per matched pair of segments, one on each track. The
   quieter copy (by 3 dB, each track against its own speech level, over the
   stretch the two share) is removed only when its words are also in the other
   track's words over that stretch: at least three words, at least two content
   words (five letters or more), 60% of each found, the other segment sliced to
   the overlapping seconds and padded by 1.5 s. The other copy is protected, so
   one utterance is never lost from both tracks. Level alone never removes a
   segment: the operator's mic is quiet, and his real speech looks far below its
   own track. Each removed segment keeps its text, match share, level margin and
   the partner's span in the raw evidence. A leak that the two tracks transcribed
   differently is kept, so a `[Mic]` turn may repeat a few remote words; the
   header says so;
4. whole segments are ordered by start time, consecutive ones of one speaker are
   joined (`TRANSCRIBE_UTTERANCE_STITCH_SECONDS`), and each turn is
   `[m:ss–m:ss] [Label]`. The first line is the usual one-line `[Speakers: ...]`
   header: method, cluster count and mapping, that names are not established, and
   the withheld, silent, empty, looping, cross-talk and collapsed-repeat counts
   with their tracks and spans. If diarization fails the header says so (or that it
   was not run) and remote lines are plain `[System]`; if levels cannot be
   measured the clusters are not mapped, the cross-talk check is skipped and the
   header says that.

**Long calls.** Measured on a synthetic 60-minute two-track call, the whisper route
takes about 10 s per call-minute. A call whose estimate (15 s per call-minute) is
over Tyto's transcription deadline (`TYTO_TRANSCRIBE_PROCESS_TIMEOUT_SECS`, default
1800 s), roughly over 2 hours, is refused before any work, as is a call whose length
cannot be measured (the length comes from ffprobe, since Tyto's recordings are
`.m4a`, `.aac` or `.mp4`). The refusal leads with the remedy and carries the
permanent-failure code `ERR_CALL_TOO_LONG_FOR_LOCAL_ASR`, so Tyto journals it once
instead of retrying: set `TRANSCRIBE_PAIRED_ASR=parakeet` in Tyto's environment, or
raise the deadline there, then re-run the held call once (stop Tyto first; the hold is a
recoverable disposition and clears only after a verified store):

```sh
python3 execution/daemons/tyto/tyto.py --recording-file '<the recording>' --recover-held
```

Tyto's alert carries this remedy and command itself. The length is measured with
the ffprobe beside the configured ffmpeg (`TRANSCRIBE_FFMPEG`), then on `PATH`; a
missing ffprobe is a transient error (Tyto retries and alerts), never a hold. A
call above 80% of the deadline runs, and the transcript
header carries a `long call` note.

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
