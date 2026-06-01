# Neotoma MCP instructions vs ateles workspace harness

## Single behavioral source

Canonical agent behavior (turn order, store recipes, retrieval, provenance, display, QA, errors, onboarding) is the **first fenced code block** in the Neotoma repo:

- [`../../neotoma/docs/developer/mcp/instructions.md`](../../neotoma/docs/developer/mcp/instructions.md)

That block is what the Neotoma MCP server sends to clients at runtime (unless compact mode is on). CLI-only hosts can print the same text with `neotoma instructions print`.

## What this repo still loads locally

| Surface | Role |
|---------|------|
| [`.cursor/rules/neotoma_harness.mdc`](../.cursor/rules/neotoma_harness.mdc) | Always-on **workspace** layer: Neotoma-only access summary, pointer to MCP `[TURN LIFECYCLE]`, mandatory `🧠 Neotoma` turn report format (with Issues/Repair/Feedback disclosure), invariants, condensed QA. Does **not** duplicate full MCP sections. |
| [`.cursor/rules/neotoma_qa_reflection_deep.mdc`](../.cursor/rules/neotoma_qa_reflection_deep.mdc) | Requestable Tier 2–4 QA detail. |
| `neotoma_cli.mdc` (often symlink) | Thin transport + CLI cheat sheet from the Neotoma package; points at the same canonical block. |

## Avoiding duplicate context

1. **Neotoma MCP server:** set `NEOTOMA_MCP_COMPACT_INSTRUCTIONS=1` in the **Neotoma server** environment when this workspace loads `neotoma_harness.mdc` and Neotoma MCP is enabled, so the client receives a short checklist plus pointers instead of the full fenced block twice.
2. **Disable Neotoma MCP** in chats that do no Neotoma I/O (removes MCP `INSTRUCTIONS.md` from context entirely). See [neotoma_cursor_context.md](neotoma_cursor_context.md).
3. **`neotoma instructions print`** for agents without MCP tools but with CLI access.

## Related

- [neotoma_cursor_context.md](neotoma_cursor_context.md) — sizing, when to enable MCP, compact mode
- [../../neotoma/docs/developer/agent_instructions.md](../../neotoma/docs/developer/agent_instructions.md) — canonical map in the Neotoma repo
