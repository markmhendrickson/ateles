---
name: scrape-chatgpt-workout
description: "Scrape a ChatGPT Fitness GPT conversation and backfill workout sessions into Neotoma. Use when user says \"scrape chatgpt workout\", \"import chatgpt fitness\", \"backfill workouts from chatgpt\", or provides a ChatGPT conversation URL. Can be invoked via /scrape-chatgpt-workout."
triggers:
  - scrape chatgpt workout
  - import chatgpt fitness
  - backfill workouts from chatgpt
  - scrape-chatgpt-workout
---

4-phase skill: Phase 0 cache check (retrieve existing conversation entity), Phase 1 capture via React fiber state walk, Phase 2 parse/reconstruct sessions (assistant summaries + user message backfill), Phase 3 store workout_session entities with REFERS_TO provenance to source conversation, Phase 4 store raw JSONL transcript as file_asset on conversation entity. Key constraints: run Phase 4 before Phase 3 on fresh capture; never store source_device field (unknown_fields error in workout_session schema v1.1.0); extract Chrome JS in batches of 100 due to extension size limits; use mcp__mcpsrv_neotoma__* prod tools.
