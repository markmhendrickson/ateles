---
name: loop-stop
description: Gracefully exit the autonomous loop
user_invocable: true
---

# Stop Agent Loop

Gracefully exit the autonomous loop.

## Steps

1. Complete the current cycle phase (don't abort mid-phase)
2. Write final `daemon/health.json` with status "stopped"
3. Lock wallet: `mcp__aibtc__wallet_lock()`
4. Commit and push any pending changes
5. Output final status summary
6. Stop looping
