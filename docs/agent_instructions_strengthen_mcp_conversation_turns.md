# MCP instructions for conversation-turn storage

## Purpose

Point maintainers to where and how to strengthen MCP conversation-turn storage behavior (Neotoma MCP instructions).

## Scope

Applies when editing or documenting agent instructions for turn storage; does not apply to other MCP servers or in-repo rule content.

---

**Do not add or maintain MCP instruction content in this repo.** The instructions that clients receive are in the Neotoma repo.

- **Edit:** `../neotoma/docs/developer/mcp/instructions.md` — first fenced code block only. That block is what the Neotoma MCP server sends to clients.
- **Process:** Invoke the `neotoma-learn` skill (`/.cursor/skills/neotoma-learn/SKILL.md`) with a scenario (for example "Agent must store every turn before responding"). It applies the instruction update directly in the target Neotoma instructions block.

To strengthen turn-storage behavior, change the instructions in the neotoma repo and reload the MCP server; no deploy required.
