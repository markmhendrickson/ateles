---
name: import-audio
description: "Import and transcribe audio files from Desktop and Voice Memos. Use when user says \"import audio\", \"import audio files\", \"transcribe audio from desktop\", or \"import voice memos\". Can be invoked via /import-audio."
triggers:
  - import audio
  - import audio files
  - transcribe audio from desktop
  - import voice memos
  - import-audio
user_invocable: true
entity_id: ent_f971d8f20a9d26cc15acdf10
---

# Import Audio

Import audio files from Desktop and macOS Voice Memos, transcribe with Whisper, and store transcriptions. Follows data entry and migration rules for locations and storage order.

## When to Use

Use this skill when:
- User says "import audio", "import audio files", "transcribe audio from desktop", or "import voice memos"
- User wants to batch-import and transcribe audio from Desktop or Voice Memos

## Required Documents (load first)

1. **Audio import workflow:** [docs/data_entry_requirements_rules.mdc](docs/data_entry_requirements_rules.mdc) (Audio File Import Workflow)
2. **Storage order:** [docs/neotoma_parquet_migration_rules.mdc](docs/neotoma_parquet_migration_rules.mdc) (Neotoma first, then Parquet if still used)

## Workflow

1. **Scan sources** for audio files (`.wav`, `.mp3`, `.m4a`, `.ogg`, `.flac`, `.aac`, `.wma`, `.mp4`, `.webm`, `.qta`):
   - `~/Desktop`
   - `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/` (macOS Voice Memos)
2. **Import** files to `$DATA_DIR/imports/audio/` with timestamped names:
   - Desktop files are **moved**.
   - Voice Memos files are **copied** (system-managed; do not move).
3. **Transcribe** using OpenAI Whisper via `execution/scripts/transcribe_audio.py`, or run the full pipeline with `execution/scripts/import_audio_from_desktop.py`.
4. **Store** transcriptions: Neotoma MCP first per migration rules; if transcriptions still in Parquet, use `$DATA_DIR/transcriptions/transcriptions.parquet` via Parquet MCP.
5. **Auto-invoke [`/analyze-meeting`](../analyze-meeting/SKILL.md)** on each successfully transcribed file. The analyze-meeting skill's own "skip silently" rule (≥2 speakers OR ≥200 words AND a commitment verb) handles non-meeting audio (voice memos, lectures, ambient recordings) so unrelated audio does not produce spurious tasks/emails/issues. Disable per-run with `--no-analyze`.
6. **Link continuations**: After all transcriptions are stored, review the batch for recordings that are continuations of each other (same topic, recorded within minutes, or explicitly referencing a previous memo). Create a `continues` relationship between them in Neotoma (earlier → later).
7. **Extract and relate entities**: For each transcription, identify mentioned entities — people, places, organizations, topics, tasks, decisions, feedback items, etc. — and create or update corresponding Neotoma entities. Then relate each transcription to every entity it produced or updated using a `mentions` relationship (transcription → entity).

## Entity Extraction Guidelines

- **People**: anyone named or clearly referenced (first name + context is enough). Entity type: `person`.
- **Feedback**: any opinion, reaction, or evaluation of a product/feature. Entity type: `feedback`. Include `source` (person name) and `subject` (product/feature).
- **Tasks / to-dos**: anything actionable the speaker intends to do. Entity type: `task`. Mark `status: open`.
- **Decisions**: resolved choices stated as facts ("we decided to…"). Entity type: `decision`.
- **Places**: locations mentioned in context (addresses, named places). Entity type: `place`.
- **Topics / themes**: recurring subjects that don't fit a narrower type. Entity type: `topic`.
- Always search Neotoma first (`retrieve_entities` / `retrieve_entity_by_identifier`) before creating — update existing entities rather than duplicating.
- Relate the transcription to each created/updated entity: `mcp__mcpsrv_neotoma__create_relationship` with predicate `mentions`.

## Continuation Detection

- Check recording timestamps: files recorded within ~5 minutes of each other from the same session are candidate continuations.
- Check content: does the transcript begin mid-thought, reference "as I was saying", or clearly continue a prior topic?
- When confirmed, create a `continues` relationship: earlier transcription entity → later transcription entity.

## Constraints

- Use `$DATA_DIR/imports/audio/` for imported files; use timestamped filenames.
- **Never move Voice Memos files** — always copy them; the Recordings directory is managed by macOS.
- Store transcriptions in Neotoma first; only then use Parquet if the data type still uses Parquet.
- Prefer `import_audio_from_desktop.py` when running the full workflow from repo root (scans both Desktop and Voice Memos by default).
- To scan a specific source only: `import_audio_from_desktop.py --source ~/Desktop`
- Always perform entity extraction and continuation linking after the transcription step — do not skip even if the transcript is short.
- Analysis is LLM-driven via `/analyze-meeting`; the old `--analyze` keyword-count flag has been removed.

## Related Rules

- [docs/data_entry_requirements_rules.mdc](docs/data_entry_requirements_rules.mdc) — Audio File Import Workflow
- [docs/neotoma_parquet_migration_rules.mdc](docs/neotoma_parquet_migration_rules.mdc) — Write path (Neotoma first)
