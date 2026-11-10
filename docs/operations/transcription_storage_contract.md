# Transcription storage contract

`transcribe_audio.py` emits `NEOTOMA_TRANSCRIPTION_ENTITY_ID` only after retrieving the entity and verifying the exact audio hash, original source name, capture method, backend, consent basis, transcript and byte count. When audio is attached, it also checks the source hash and an observation linking the transcription to that source. A missing field, mismatched value, omitted requested attachment or unavailable read is incomplete ingestion and raises an error, so Tyto retains its retry behavior. An idempotency mismatch is not a successful duplicate receipt.

The store payload is stable under the content-keyed idempotency key (`data_source` uses the audio hash, not a wall-clock timestamp), so a committed write followed by a read-back failure can retry without permanent rejection. File-backed stores fail closed when the audio file is missing or unreadable. Explicit `attach_audio_file=False` metadata-only imports may supply size and hash from the transcription result when the file is absent.

`--consent-basis` supplies an explicit attestation; `TRANSCRIPTION_CONSENT_BASIS` supplies the runtime default. Without either, the value is `unknown`. Capture method never implies consent. This metadata does not grant authorization to record.

The compatible Neotoma schema declares all these fields and preserves case and whitespace for text and names. Existing deployments require separate schema review and activation before this caller can complete successfully. Remote attachment support must also be active. Keep Tyto activation and retry draining behind both dependencies; do not treat local tests as production verification.

Legacy path lookup and explicit `attach_audio_file=False` callers remain supported. Historical observations are not rewritten. The recording/transcript identity redesign is tracked separately in issue #953.

## Denied stores and selective recovery

A deterministic `capability_denied` stops automatic retries and remains in the
source's durable retry journal. This is a recoverable hold, not permission to
change a grant. Meeting, platform-native and Voice Memo watchers have separate
journals. Their existing retry cap/backoff still bounds other failures.

Before storing, the transcription CLI writes `<audio>.pending-store.json`
atomically with mode `0600`. It contains the complete transcription result,
including raw timing/separation output, the resolved path and SHA-256 of each
original track, and the capture/consent/backend/relationship context. Existing
text/raw sidecars and audio are retained. A matching retry reuses this result
without ASR; changed audio or context fails closed. The pending result is retained
after success so an interrupted recovery does not force another ASR run. It is
sensitive local transcript data, subject to the same retention as its sidecars.
No pending result or transcript text is included in recovery status output.

Keep the normal daemon stopped during acceptance and backfill. After the normal
review, deployment and permission gates, invoke the deployed Tyto entry point
with the same configured source directories, journals, identity and CLI runtime.
Replace the example path with one explicitly selected, settled file:

```sh
python execution/daemons/tyto/tyto.py --recording-file /configured/source/selected.m4a
```

This is a single canary, not a daemon start: it never polls directories, constructs
screenshot intake or analysis loops, or creates notification transports. It uses
Tyto's own signer and the normal transcription-plus-audio store/readback path.
An entity receipt is required; verify it against the canonical stored entity and
its expected audio provenance before proceeding. A receipt alone does not prove
model quality, a working hardware backend, or permission for the next recording.

For a named failed recording whose prerequisite has been repaired:

```sh
python execution/daemons/tyto/tyto.py --recording-file /configured/source/failed.m4a --recover-held --store-only
```

`--store-only` requires the matching pending result and cannot run ASR. A missing
or mismatched cache leaves the hold intact. Failures that predate this format
cannot safely reconstruct a complete source-bound result from text alone. For
such a legacy item, omit `--store-only` to authorize one explicit transcription
attempt through the same normal path. Repeat `--recording-file` only for an
individually checked selection; do not supply an archive-wide generated list.
Exclude recordings already owned by another intake process. The selected file's
hold clears only after verified store success and a successful journal save;
other pending/held entries are preserved. No manual journal deletion is needed.

Both entry-point modes share an exclusive host lock (`TYTO_RUNTIME_LOCK`, default
`~/.local/state/ateles/tyto.lock`); a competing process fails before processing.
A pre-upgrade daemon does not honor this new lock and must be confirmed stopped
independently. Normal daemon startup resumes screenshots, enabled analysis and
existing pending recordings; it is broader than these canary/backfill commands.
Source approval and synthetic tests are not evidence that it is safe to resume.

Local FluidAudio diarization defaults to `--compute-units cpu-only`, avoiding
Metal command-buffer timeouts in the offline diarization stage while preserving
speaker separation. `TRANSCRIBE_FLUID_COMPUTE_UNITS=ane` is another supported
off-GPU selection; `all` explicitly permits GPU use. ASR is still local. Errors
name the failed CLI stage and retain both the beginning and end of long stderr
so profiling output cannot conceal an earlier Metal failure. A real supported
hardware run remains required after review; no cloud or undiarized fallback is
introduced by this repair.

The selected and normal Tyto subprocess routes now require an attached-audio
outcome explicitly. Audio over the remote upload limit raises
`ERR_AUDIO_ATTACHMENT_REQUIRED` before any metadata-only write. That hold can be
retried explicitly after its transport prerequisite is repaired; no attachment
omission clears it. Standalone metadata-only import callers retain their
existing explicit behavior.

Tyto accepts a private JSON receipt file, created in its own temporary directory,
rather than parsing diagnostic stdout. The receipt is written only after the
normal storage readback, binds the original track hashes/paths and capture method,
and distinguishes verified audio custody with its stored source ID. Tyto checks
that binding and unchanged local originals before clearing any hold. Filenames
and transcript text can contain marker-looking lines without affecting this
channel. Source files and paired microphones must be regular files within the
resolved configured directory; escaping symlinks are rejected. Journal paths
are normalized on read, with conflicting alias holds kept restrictive.

Pending-result identity also includes a hash of the effective vocabulary and
processing controls (cleanup, labels, language, segmentation, model selection,
placement and backend setup). Vocabulary from environment, file and Neotoma is
resolved once per CLI operation and used consistently by postprocessing. Local
model/binary metadata and processing source revisions participate without loading
models. A changed setting or resolved vocabulary rejects reuse before ASR/store;
return to the original processing context to recover that immutable result. No
model inference, credential value or vocabulary text is included in the receipt.

Filesystem-equivalent case/Unicode spellings and hard links share an existing
recording's journal and watcher identity through `samefile` evidence, preserving
its recorded source name for recovery and receipt binding. Loading legacy alias
entries keeps the strictest conflicting hold, highest attempt count and latest
retry time. Distinct files stay distinct even when their bytes match; no blanket
case folding or Unicode normalization is used. Missing legacy paths retain their
recorded spelling and do not establish filesystem equivalence.

## Exact-result proof for supervised recovery

A supervising `--receipt-file` request selects strict persistence verification
for both fresh stores and idempotent prior writes. The receipt is version 2 and
requires `exact_result_verified: true`, plus a SHA-256 of the actual persisted
transcript. Its capture method and engine come from the verified snapshot.
Tyto rejects older/weaker receipts before any selected hold is cleared.

Strict verification compares the actual persisted transcript, original source
name, audio hash/path/size, capture method, engine, consent basis and language.
A supplied model or duration must also match. Missing fields fail even when the
live schema does not declare them; schema compatibility is not evidence that a
particular requested result persisted. Attached audio requires a separately read
source with the exact content hash. A date-only retry may resolve an equivalent
older record without inference because import/transcription dates are not result
identity. Different or missing result/context fields fail closed with
`ERR_PERSISTED_RESULT_MISMATCH`; no strong receipt or selected hold clearance is
produced. A same-remote-file prior result with different microphone-derived text
is therefore not accepted as the current paired result.

Standalone historical import callers retain the prior weaker lookup by default.
That behavior can recognize an earlier nonempty transcript by hash or path and
must not be represented as exact current-result persistence. It cannot satisfy a
supervisor receipt. This separation introduces no schema, grant or principal
change and does not silently correct or replace an incompatible prior entity.

The strong receipt proves stored transcript/context and main-track audio custody.
Both original tracks are hash-bound locally and retained with the complete raw
pending result; it does not claim that a second microphone asset or raw timing
JSON was uploaded when the underlying storage contract uploads only the main
track. Model quality, coverage and real hardware behavior retain their separate
runtime acceptance checks.

Selected recovery also understands the normal watcher's
`ERR_RECORDING_SOURCE_REJECTED` quarantine. It still validates both original
tracks before inference or storage, so an unsafe/missing/nonregular mic cannot
be bypassed by the recovery flag. After its owner repairs the physical source,
a matching cached result can retry without inference. Likewise a
`ERR_PERSISTED_RESULT_MISMATCH` hold can be explicitly retried after its owning
storage/evidence prerequisite is repaired; the exact persisted-result comparison
still runs and an incompatible result remains held. Neither retry flag waives
source confinement, consent, authority, attachment or persistence predicates.

## Complete inference-result validation

Before writing sidecars, replacing a pending result or entering storage, the CLI
validates the actual backend's complete result contract. The pending reader and
writer apply the same check. Unknown/missing backend or engine cannot select a
weaker default. Common text/model/language/backend/engine/size fields and the
constructor's duration key are required with their supported types; finite
numeric measurements reject booleans, NaN and infinity. A nullable measurement
still retains its key where emitted. Local Whisper additionally retains nullable
RMS and boolean silence; it and OpenAI do not require raw model JSON. OpenAI's
returned nullable language is preserved. Paired ElevenLabs permits unmeasured
conditional duration; paired Fluid does not require RMS or silence.

Fluid results retain the complete tracks and aligned-word structures, including
both System/Mic originals for a pair, nested diarization/ASR containers, timing,
speaker and confidence fields (including null/backend passthrough values). The
same local alignment function checks preserved ASR/diarization against retained
aligned words, including both paired track results; missing words cannot become
silence while speech evidence remains. ElevenLabs validates ordinary/word,
multichannel and segmented bodies through the synchronous response parser and
its event converters, retaining text-only and optional/null metadata forms.
A successful nonempty transcript requires retained usable content, while silent
parts within a valid segmented result remain supported. Extra legitimate JSON
fields are preserved. This checks
constructor completeness, not cryptographic authenticity against the same user
who controls the executable, audio and local files.

Missing/mistyped required fields raise `ERR_INCOMPLETE_TRANSCRIPTION_RESULT`
before any sidecar/cache/storage effect. Existing malformed input caches, complete
raw/text sidecars, originals and selected holds remain unchanged; there is no ASR
fallback in store-only mode. An owning repair may explicitly retry after restoring
a complete valid result, with all source and exact persisted-result checks still
required. No model, server metadata, schema or authorization change follows from
this validation.
