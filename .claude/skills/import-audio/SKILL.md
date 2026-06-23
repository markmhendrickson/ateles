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

Import audio files from desktop, transcribe with Whisper, and store transcriptions. Follows data entry and migration rules for locations and storage order.

## When to Use

Use this skill when:
- User says "import audio", "import audio files", "transcribe audio from desktop"
- User wants to batch-import and transcribe desktop audio

## Required Documents (load first)

1. **Audio import workflow:** [docs/data_entry_requirements_rules.mdc](docs/data_entry_requirements_rules.mdc) (Audio File Import Workflow)
2. **Storage order:** [docs/neotoma_parquet_migration_rules.mdc](docs/neotoma_parquet_migration_rules.mdc) (Neotoma first, then Parquet if still used)

## Workflow

1. **Scan desktop** for audio files: `.wav`, `.mp3`, `.m4a`, `.ogg`, `.flac`, `.aac`, `.wma`, `.mp4`, `.webm`.
2. **Move** files to `$DATA_DIR/imports/audio/` with timestamped names.
3. **Transcribe** using OpenAI Whisper via `execution/scripts/transcribe_audio.py`, or run full pipeline with `execution/scripts/import_audio_from_desktop.py --analyze`.
4. **Analyze** transcription (word count, key indicators, preview).
5. **Store** transcriptions: Neotoma MCP first per migration rules; if transcriptions still in Parquet, use `$DATA_DIR/transcriptions/transcriptions.parquet` via Parquet MCP.

## Constraints

- Use `$DATA_DIR/imports/audio/` for imported files; use timestamped filenames.
- Store transcriptions in Neotoma first; only then use Parquet if the data type still uses Parquet.
- Prefer `import_audio_from_desktop.py --analyze` when running the full workflow from repo root.

## Related Rules

- [docs/data_entry_requirements_rules.mdc](docs/data_entry_requirements_rules.mdc) — Audio File Import Workflow
- [docs/neotoma_parquet_migration_rules.mdc](docs/neotoma_parquet_migration_rules.mdc) — Write path (Neotoma first)
