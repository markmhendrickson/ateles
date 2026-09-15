# Transcription storage contract

`transcribe_audio.py` emits `NEOTOMA_TRANSCRIPTION_ENTITY_ID` only after retrieving the entity and verifying the exact audio hash, original source name, capture method, backend, consent basis, transcript and byte count. When audio is attached, it also checks the source hash and an observation linking the transcription to that source. A missing field, mismatched value, omitted requested attachment or unavailable read is incomplete ingestion and raises an error, so Tyto retains its retry behavior. An idempotency mismatch is not a successful duplicate receipt.

The store payload is stable under the content-keyed idempotency key (`data_source` uses the audio hash, not a wall-clock timestamp), so a committed write followed by a read-back failure can retry without permanent rejection. File-backed stores fail closed when the audio file is missing or unreadable. Explicit `attach_audio_file=False` metadata-only imports may supply size and hash from the transcription result when the file is absent.

`--consent-basis` supplies an explicit attestation; `TRANSCRIPTION_CONSENT_BASIS` supplies the runtime default. Without either, the value is `unknown`. Capture method never implies consent. This metadata does not grant authorization to record.

The compatible Neotoma schema declares all these fields and preserves case and whitespace for text and names. Existing deployments require separate schema review and activation before this caller can complete successfully. Remote attachment support must also be active. Keep Tyto activation and retry draining behind both dependencies; do not treat local tests as production verification.

Legacy path lookup and explicit `attach_audio_file=False` callers remain supported. Historical observations are not rewritten. The recording/transcript identity redesign is tracked separately in issue #953.
