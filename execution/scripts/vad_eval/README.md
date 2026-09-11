# VAD evaluation harness — live-transcription input gate

Measures candidate voice-activity / speech-presence detectors against the
operator's labelled corpus, to decide whether any of them should replace
`webrtcvad` as layer 2 of the input gate in `../stream_transcript.py`.

**Result: negative. Nothing here beat the baseline usefully, and nothing was
wired in.** See "Finding" below.

## Corpus

Built from `*_live.jsonl` next to their recordings. Each JSONL row carries
`start_s`/`end_s`, so every emitted transcript line can be cut back out of the
audio and scored.

Labels:

* **fabrication** — the text contains non-Latin script. The operator speaks
  English, Spanish, and Catalan (`locale_profile ent_ea9a413189860f872c6cc99a`),
  so Japanese/Korean/Arabic/Georgian output cannot be a transcription of them.
* **genuine** — Latin script, and not already suppressed by the existing gate.

Rows whose `start_s` lies past the end of the audio are dropped as unscoreable
(100 of 1365). Final corpus: **1106 genuine, 159 fabrication, 23 audio files.**

## Validate before you trust it

`validate.py` reproduces the published webrtcvad numbers from `pyproject.toml`
and ateles#631 — the −31.6 dBFS Georgian fabrication scoring **more**
speech-like than genuine operator speech — and checks that the RMS
reimplementation matches production's recorded `rms_db`.

Run it before believing any score this harness prints:

    python3 validate.py

This is not ceremony. Two separate instrument bugs surfaced during this
evaluation, each of which produced a confident, plausible, wrong answer:

1. Measuring whole-span mean RMS instead of production's p95-of-3s-windows
   (`live_transcript_tail.sustained_rms_db`) put the Georgian case at −37.5 dBFS
   instead of −31.6.
2. A hand-rolled ONNX Silero call mishandled the recurrent `state` tensor and
   scored ~0.001 on *everything*, including loud speech webrtcvad scored 0.91.
   Read naively, that was a dramatic negative result. It was a broken harness.

## Finding

Scores are the fraction/mean of per-window speech probability over each
labelled span. AUC is P(genuine scored higher than fabrication); 0.5 is no
signal, below 0.5 is inverted.

Across the whole corpus (n=1106/159), Silero leads webrtcvad — AUC 0.76 vs
0.70. But that comparison is confounded: fabrications are mostly *quiet*
(median −62.9 dBFS vs −40.8 for genuine), so a corpus-wide AUC largely rewards
detecting loudness, which layer 1 (RMS) already does.

The population that matters is the audio that **already passed RMS**, since
that is the only audio layer 2 is ever consulted on. Restricted to spans above
−40 dBFS (**n = 519 genuine, 18 fabrication**):

| detector | genuine median | fabrication median | AUC | overlap (gen/fab) | cost to block 80% of fabrication |
|---|---|---|---|---|---|
| silero (mean) | 0.556 | 0.437 | 0.605 | 93% / 100% | loses 68.8% of genuine speech |
| silero (p90)  | 1.000 | 1.000 | 0.644 | 83% / 100% | loses 59.7% of genuine speech |
| silero (max)  | 1.000 | 1.000 | 0.649 | 93% / 100% | loses 67.1% of genuine speech |
| webrtcvad a=2 | 0.717 | 0.647 | 0.528 | 96% / 100% | loses 92.7% of genuine speech |
| webrtcvad a=3 | 0.468 | 0.436 | 0.461 | 95% / 100% | loses 95.2% of genuine speech |

**100% of fabrication samples fall inside the genuine range at every operating
point.** There is no threshold that removes the fabrications without taking most
of the operator's real speech with it. Silero is a better detector than
webrtcvad on this data, and it is still not good enough to wire in: buying an
80% reduction in fabrication for 60–69% of genuine speech is not a trade the
gate should make silently.

webrtcvad's published inversion reproduces exactly on the clip it was measured
on (0.76 fabrication vs 0.42 genuine mean in that file). At corpus scale the
sign flips positive — the inversion is a property of that clip, not of the
detector. Both statements are true; the single-clip one is what got published.

## Recommendation (not built here)

The input gate is an energy/voicing filter, and fabrication-triggering audio is
frequently real, voiced sound — someone else's speech, a video, room noise with
speech structure. No off-the-shelf VAD separates "voiced" from "voiced by this
operator, in a language they speak". The next layer to try is therefore **not**
another VAD:

* **output-side script/language screening** — the label used to build this
  corpus is itself the strongest available signal, and it is computed *after*
  transcription, where it actually discriminates;
* a **confidence signal from the API**, if one can be obtained per segment;
* **requiring corroboration across consecutive chunks** before surfacing text.

## Files

* `corpus.py` — build the labelled corpus from the JSONL + audio pairs
* `audio.py` — ffmpeg segment extraction, PCM16 dBFS
* `rms.py` — production's p95-of-windowed-RMS statistic
* `det_webrtc.py`, `det_silero.py` — scorers
* `report.py` — AUC, overlap, threshold cost
* `validate.py` — harness validation against the published numbers

Silero needs `torch` + `silero-vad`; both were installed only into a scratch
venv for this evaluation, never into the system Python, and nothing in the
shipped gate depends on them.
