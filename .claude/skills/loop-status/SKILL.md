---
name: loop-status
description: Show current agent state
user_invocable: true
---

# Agent Status

Show current state of the agent without entering the loop.

## Display

1. Read `daemon/health.json` for last cycle info
2. Read `daemon/queue.json` for pending tasks
3. Check wallet status (locked/unlocked)
4. Check sBTC and STX balances
5. Read `daemon/outbox.json` for pending outbound messages and budget
6. Output a concise status summary
