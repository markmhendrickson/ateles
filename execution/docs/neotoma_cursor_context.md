# Neotoma Cursor context (baseline and operations)

## Measure sizes

From repo root:

```bash
python3 execution/scripts/measure_neotoma_cursor_context.py
python3 execution/scripts/measure_neotoma_cursor_context.py --with-mcp-path "$HOME/.cursor/projects/<your-project>/mcps/user-neotoma/INSTRUCTIONS.md"
```

Token counts are **approximate** (`bytes / 4`). Use the same script after edits to avoid accidental regressions.

## When to enable or disable `user-neotoma` MCP

| Situation | Recommendation |
|-----------|----------------|
| Chats that require Neotoma `store_structured`, graph reads, or Inspector-linked turn reports | Enable Neotoma MCP |
| Pure repo work (refactors, tests) with **no** Neotoma reads/writes | Disable Neotoma MCP to drop the large MCP instruction block from context |
| Neotoma unavailable but policy still requires persistence | Use Neotoma CLI (`neotoma store`, etc.) per `neotoma_cli` / backup rules; MCP can stay off |

Disabling MCP **does not** remove workspace `.cursor/rules/neotoma_harness.mdc` (unless you change rules). It removes the duplicate contract shipped in `INSTRUCTIONS.md`.

## Compact MCP instructions (Neotoma server)

When the Neotoma MCP **server** runs with **`NEOTOMA_MCP_COMPACT_INSTRUCTIONS=1`** (or `true`), it sends the **compact** instruction block instead of the full fenced block from `docs/developer/mcp/instructions.md`.

Use this when the agent host **already loads** the expanded policy from:

- [`.cursor/rules/neotoma_harness.mdc`](../.cursor/rules/neotoma_harness.mdc) (always-on consolidated harness)

Set the variable in the environment of the **Neotoma API/MCP process** (for example `../neotoma/.env`, launchd, or the script that starts `neotoma mcp` / HTTP MCP). Exporting it only in a Cursor-side proxy shell does **not** affect what the server returns unless that variable is forwarded into the server process.

**Do not** use compact mode for clients that rely on MCP instructions alone with no workspace Neotoma rules.

## Canonical text without MCP

Run:

```bash
neotoma instructions print
```

from an installed Neotoma CLI, or open `../neotoma/docs/developer/mcp/instructions.md` in the Neotoma checkout.

## Related

- [neotoma_mcp_instructions_overlap.md](neotoma_mcp_instructions_overlap.md) — how MCP, harness, and CLI layers relate
- [neotoma_cursor_context_verification_checklist.md](neotoma_cursor_context_verification_checklist.md) — post-change verification
