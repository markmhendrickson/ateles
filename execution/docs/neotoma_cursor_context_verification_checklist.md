# Verification checklist after Neotoma context optimizations

Run with **Neotoma MCP enabled** and workspace rules present (ateles).

1. **User-phase store** — First tool batch after a user message includes a `store_structured` (or explicit waiver for no user-visible content).
2. **Closing store** — Assistant reply ends with closing `store_structured` + `PART_OF` when a user-visible reply was produced.
3. **Turn report** — Reply ends with `🧠 Neotoma` section per [`neotoma_harness.mdc`](../.cursor/rules/neotoma_harness.mdc) (Conversation / Reads / Created / Updated / Issues / Repairs as applicable).
4. **Read-first** — If the user asks for data that lives in Neotoma, a bounded retrieval runs before answering from memory.
5. **Compact MCP mode** — With `NEOTOMA_MCP_COMPACT_INSTRUCTIONS=1`, reconnect Cursor to Neotoma MCP and repeat steps 1–3; behavior should match full mode for normal chat turns.

Optional: disable MCP and confirm CLI backup path still works if you use `neotoma` CLI with `--api-only` / aligned env.
