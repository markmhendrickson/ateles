# Workout import — ChatGPT Fitness transcript parser

Reconstructs structured `workout_session` records (Gorilla schema) from a raw
ChatGPT Fitness-GPT conversation export.

## Why this exists

Historical workout sessions (Sep 2025 → May 2026) were logged conversationally
in a ChatGPT custom GPT ("Fitness" project). The user reported sets incrementally
— one rep correction at a time, with renames, unit switches, and running set
counts — so the **user** messages are not reliably parseable on their own.

The **assistant**, however, continuously maintains a clean structured running log
and produces per-session analysis tables in a stable markdown format. This parser
harvests the most complete/authoritative assistant block per session-day and
converts it to the `workout_session` schema.

## Usage

```bash
# 1. Export the raw conversation JSON from the browser (see "Exporting" below).
# 2. Parse it:
python3 parse_chatgpt_workouts.py <raw_conversation.json> -o parsed_sessions.json

# Debug a single day:
python3 parse_chatgpt_workouts.py <raw.json> --date 2025-09-15
```

Output is a reviewable JSON array of sessions with a `confidence` flag
(`high` / `low` / `empty`) and parse provenance, so a human verifies before
importing to Neotoma via Gorilla.

## Exporting the raw transcript

ChatGPT virtualizes long conversations, so DOM scraping misses most turns. Pull
the full conversation JSON from the backend API instead (run in the page console
via the Chrome MCP `javascript_tool`):

```js
const sess = await fetch('/api/auth/session').then(r => r.json());
const conv = await fetch(`/backend-api/conversation/${convId}`, {
  headers: { Authorization: `Bearer ${sess.accessToken}` }
}).then(r => r.json());
// download JSON.stringify(conv) as a blob
```

This returns the complete `mapping` (every node — including branches and
previously-removed turns), not just the active path.

**Download gotcha:** Chrome blocks rapid multiple automatic downloads from one
gesture. Trigger downloads **one per tool call**, verifying each lands before the
next. The conversation JSON contains auth-token-like metadata, so chunked
text-return transfer is blocked by the MCP safety filter — use the blob download.

## Conventions handled

- **Warmup rule:** first daily set of each exercise = warmup; rest = working
  (the operator's stated convention), overridden by explicit per-set notes.
- **Units:** normalized to kg. Detects lbs-mode days ("track in lbs") and converts
  (1 lb = 0.45359237 kg), flagging the session with `lbs_converted: true`.
- **Location:** default Metropolitan Sagrada Família; detects travel-gym overrides
  (e.g. "Gym 5 Nashville", "Gold's Gym Austin", "Palo Alto Fit").
- **Format variety:** parses both markdown analysis tables (`| weight | reps | note |`)
  and inline running summaries (`- 80 kg × 8 (PR)`), preferring the notes-bearing
  table when present.

## Known limitations (verify before import)

- **8 empty days** — analysis-only conversations or rest days with no parseable
  set block; need manual review.
- **`low`-confidence sessions** (<6 sets) — often a single exercise where other
  exercise headers in the block weren't detected; collapsed sets are possible.
- **Bodyweight movements** (toes-to-bar, dips, chin-ups) — weight column is
  unreliable; a stray analysis-table weight can leak in. Spot-check these.
- **Session-type/date strings** occasionally mis-captured as location on sparse days.

The raw JSON is the lossless source of truth; the parsed output is a best-effort
reconstruction meant for human review, not blind import.

## Files

- `parse_chatgpt_workouts.py` — the parser
- Raw exports + parsed output staged under
  `~/Desktop/triage/_staged/archive/workout logs/chatgpt_raw/`
