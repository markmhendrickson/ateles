---
name: tyto-ocr
description: "OCR + entity extraction for a single Tyto screenshot entity. Reads the source image, extracts visible text, identifies confidently-extractable entities (task references, people, dates, URLs), and checks for a matching existing `task` entity by title/content. Emits a single machine-parseable JSON line to stdout — no other output format. Invoked headlessly by Tyto's OcrConsumer via `claude --print`; not user-invocable."
triggers: []
user_invocable: false
---

# Tyto OCR

Headless OCR + entity-extraction worker dispatched by Tyto's `OcrConsumer` (`execution/daemons/tyto/tyto.py`) for one `screenshot` entity at a time. This skill has exactly one caller and one required output contract — do not add conversational framing, confirmations, or follow-up questions.

## Invocation

Tyto pipes a prompt on stdin containing:
- The absolute path to the source image file.
- The screenshot's Neotoma `entity_id`.
- A snippet of candidate `task` entities (id + title) already retrieved by Tyto, to match against.

## What to do

1. Read the image at the given path. If the file is missing, zero-byte, or unreadable, skip straight to the failure output below.
2. Extract all visible text from the image (OCR).
3. From the extracted text, identify confidently-extractable entities: task references, people, dates, URLs. Do not invent or guess entities not actually present in the text.
4. Compare the extracted text against the candidate task titles supplied in the prompt. If the text confidently references one of them (title or close paraphrase appears in the OCR'd text), note its `entity_id` as a match. At most one match — pick the strongest. No match is a normal, expected outcome, not a failure.

## Required output — last line of stdout

Emit exactly one line containing `TYTO_OCR_RESULT=` followed by a single-line JSON object, and nothing else after it:

```
TYTO_OCR_RESULT={"ok": true, "ocr_text": "...", "extracted_entities": [{"type": "task_reference", "value": "..."}], "matched_task_entity_id": "ent_...", "error": null}
```

Field contract:
- `ok` (bool, required) — `true` if OCR ran (even with zero extracted entities); `false` only on unreadable/missing/corrupt image or an unrecoverable extraction error.
- `ocr_text` (string, required when `ok`) — full extracted text; empty string if none found (not a failure).
- `extracted_entities` (array, required when `ok`) — may be empty. Each item: `{"type": "task_reference"|"person"|"date"|"url", "value": "<string>"}`.
- `matched_task_entity_id` (string or null) — the single strongest confident match from the candidate list, or `null` if none.
- `error` (string or null) — short human-readable reason when `ok` is `false`; `null` otherwise.

Do not emit prose, markdown, or explanation before or after this line — Tyto parses stdout for the `TYTO_OCR_RESULT=` prefix and discards everything else, but keep output minimal since it also appears in logs.
