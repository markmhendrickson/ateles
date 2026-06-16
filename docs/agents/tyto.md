# tyto

Screenshot watcher + meeting recording transcription + analysis daemon. (1) Polls TYTO_SCREENSHOTS_DIR for new image files, stores screenshot entities in Neotoma. (2) Polls TYTO_RECORDINGS_DIR for new *remote* AAC/M4A/MP4 files from Audio Hijack; pairs with matching mic file, waits for both to settle, runs transcribe_audio.py with two-file [You]/[Speaker_N] merge (ElevenLabs word timestamps) or remote-only diarization fallback. (3) After transcription, invokes claude --print with /analyze-meeting skill (+ /analyze-neotoma-feedback inline when meeting is Neotoma-oriented) to extract insights, decisions, action items (stored as Neotoma tasks), psychological/interpersonal dynamics, and recap messages. Sends Telegram notification on each stage. Set TYTO_ANALYZE_ENABLED=0 to disable analysis.

| Field | Value |
| --- | --- |
| Tier | T3 |
| Genus | Tyto |
| Status | active |
| AAuth sub | tyto@ateles-swarm |
| Agent grant | service |
| Allowed tools | neotoma_read, neotoma_write, filesystem_read |
| Harness | polling loop via asyncio.sleep |
| Entity ID | ent_affecbbecf52edb633c534f8 |

---

Operational prompt: [`.claude/skills/tyto/SKILL.md`](../../.claude/skills/tyto/SKILL.md)

*Reference card mirrored from Neotoma `agent_definition` `ent_affecbbecf52edb633c534f8`. Do not edit directly — correct the entity and run `python3 execution/scripts/render_agent_docs.py`.*
