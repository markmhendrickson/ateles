# Tyto denied-inference containment

This change contains automatic recording retries and isolates local FluidAudio
execution. It is not the complete recording repair and does not authorize a
daemon restart, grant change, recording replay or backfill.

Normal recording watchers persist retry state independently for Audio Hijack,
platform-native recordings and Voice Memos. Their defaults are respectively
`~/.local/state/ateles/tyto-recording-retries.json`,
`~/.local/state/ateles/tyto-native-recording-retries.json` and
`~/.local/state/ateles/tyto-voice-memo-retries.json`; the matching
`TYTO_RECORDING_RETRY_STATE`, `TYTO_NATIVE_RECORDING_RETRY_STATE` and
`TYTO_VOICE_MEMO_RETRY_STATE` settings override them.

A `capability_denied` store error stops that recording's automatic retries,
including after restart. The paired subprocess path preserves the error code
without falling back to a single track. Existing retry limits and backoff bound
other failures. An unavailable journal fails closed. Original audio and the
existing transcript/raw sidecars are retained. This unit adds no cached complete
inference result or supported recovery command; failed items remain held for the
separate persistence/recovery unit and its operational acceptance.

Watcher and journal keys use filesystem `samefile` evidence to unify case,
Unicode and hard-link aliases, retaining the recorded source path. Legacy aliases
merge the highest attempts and latest retry time; conflicting hold classes stay
restrictive. Distinct files remain distinct even when their bytes match. No
blanket lowercasing or Unicode normalization is used. Missing paths establish no
filesystem equivalence. Both physical tracks must remain regular files inside
the configured source; escaping symlinks are rejected before inference.

FluidAudio offline diarization defaults to `--compute-units cpu-only`, preserving
speaker separation while avoiding Metal execution in that stage.
`TRANSCRIBE_FLUID_COMPUTE_UNITS` accepts `cpu-only`, `ane` or `all`; `all` explicitly
permits GPU use. ASR remains local, and no cloud or undiarized fallback is added.
Errors identify the failed CLI stage and retain the beginning and end of long
stderr so profiling output cannot hide the initial failure. Synthetic command
checks do not prove inference works on actual hardware.

## Storage handoff boundary

The normal watcher still uses its inherited transcription subprocess and stdout
entity marker. The standalone transcription storage and historical idempotency
resolver are unchanged. That resolver can accept a prior nonempty transcript
matching audio hash or path without proving that current requested text,
capture method, engine and consent context persisted. Existing oversized remote
uploads may be stored explicitly without attached audio. The inherited handoff
therefore does not constitute a strong current-result or attached-source receipt.

This containment unit introduces no selected recovery CLI, complete pending
result/configuration fingerprint, host writer lock, strong structured receipt,
strict prior-write equivalence or selected hold-clearing contract. Those belong
together in the dependent persistence/recovery unit. This unit alone does not
clear old holds or the parent repair's fresh-recording/readback/backfill exit.

The normal daemon also runs screenshot intake, enabled analysis and its existing
retry queue. Its activation remains broader than the source checks here, and
pre-upgrade writers require independent operational exclusion. Keep activation
and real recording acceptance with their existing qualified owner and gates.
