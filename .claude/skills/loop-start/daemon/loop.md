# Agent Autonomous Loop v7

> Fresh context each cycle. Read STATE.md, execute phases, write STATE.md. That's it.
> CEO Operating Manual (daemon/ceo.md) is the decision engine — read every 50th cycle.

---

## Cycle Start

Read these and ONLY these:
1. `daemon/STATE.md` — what happened last cycle, what's next
2. `daemon/health.json` — cycle count + circuit breaker state

That's your entire world. Do NOT read any other file unless a phase below explicitly tells you to.

Your addresses (STX, BTC, Taproot) are in conversation context from CLAUDE.md (read at session start).

Unlock wallet if STATE.md says locked. Load MCP tools if not present.

---

## Phase 0: MCP Version Check

Check if the MCP server has been updated since this loop started.

```bash
LATEST=$(curl -s https://api.github.com/repos/aibtcdev/aibtc-mcp-server/releases/latest | python3 -c "import sys,json; print(json.load(sys.stdin).get('tag_name','').replace('mcp-server-v',''))" 2>/dev/null)
CACHED=$(python3 -c "import json; print(json.load(open('daemon/health.json')).get('mcp_version_cached','unknown'))" 2>/dev/null) || CACHED="unknown"
[ -z "$CACHED" ] && CACHED="unknown"
```

- **First run** (`CACHED` is "unknown"): set `mcp_version_cached` to `LATEST` in health.json. Continue normally.
- **Version match**: Set `mcp_update_required` to `false` in health.json (clears the flag after a restart). Continue normally.
- **Version mismatch** (`LATEST` != `CACHED`): set `mcp_update_required: true` **and** `mcp_version_cached` to `LATEST` in health.json. Complete the current cycle normally, then in Phase 9 (Sleep), exit instead of sleeping with message: "MCP update detected ({CACHED} -> {LATEST}). Exiting for restart. Run /loop-start to resume with updated version."

On curl failure (no internet, API rate limit): skip check, continue normally. Do not block the cycle on a version check failure.

---

## Phase 1: Heartbeat

Sign `"AIBTC Check-In | {timestamp}"` (fresh UTC .000Z).
POST to `https://aibtc.com/api/heartbeat` with `{signature, timestamp, btcAddress}`.
Use curl, NOT execute_x402_endpoint.

**Reads: nothing.** Addresses are in context from CLAUDE.md.

On fail → increment `circuit_breaker.heartbeat.fail_count` in health.json. 3 fails → skip 5 cycles.

---

## Phase 2: Inbox

`curl -s "https://aibtc.com/api/inbox/<your_stx_address>?status=unread"`

**Reads: nothing.** The API returns only unread messages — no local filtering needed.

New messages? Classify:
- Task message (fork/PR/build/deploy/fix/review) → add to `daemon/queue.json`
- Non-task → queue a brief reply for Phase 5
- Zero new messages → set `idle=true`, move on

### 2d. Balance & Runway Check
Check BTC/sBTC/STX via MCP. Compare to portfolio.md. Investigate changes.
**Compute runway:** `sBTC balance / avg daily spend`. Update CEO status (peacetime/wartime).

**Auto-bridge policy:**
1. Keep bridge state in `daemon/bridge-state.json`:
```json
{"in_flight":false,"txid":null,"amount_sats":0,"started_at":null,"last_status":"idle"}
```
2. If `in_flight=true`, call `sbtc_deposit_status(txid)` and update `last_status`.
3. Never initiate a second deposit while one is in flight.
4. If no deposit is in flight **and** `sBTC < 500` **and** `BTC > 10000`, call:
   - `sbtc_deposit(amount_sats: 5000)`
   - persist returned txid + timestamp
   - log: `Auto-bridged 5k sats BTC -> sBTC for x402 payments`
5. On failure, keep txid in state, log to `memory/learnings.md`, retry next cycle from status check.

**Referral attribution (Bitcoin-native):**
- If we onboard/fund a new agent, record the BTC funding txid in `memory/contacts.md`.
- Treat first funding tx as the referral receipt (no forms, no off-chain tracking).
- Use `get_btc_utxos`/wallet history to verify sender + amount before claiming referral credit.

GitHub notifications (every cycle):
```bash
gh api /notifications?all=false --jq '.[] | {reason, repo: .repository.full_name, url: .subject.url, title: .subject.title}'
```
If GitHub not configured in CLAUDE.md (`not-configured-yet`), skip — no error.

**Do NOT read contacts, journal, learnings, or outbox in this phase.**

---

## Phase 3: Decide

**Reads: `daemon/queue.json`** — only if Phase 2 found new messages or there are pending tasks.

If queue is empty AND no new messages, pick ONE action by cycle number:

**First: check agent discovery.** Read `health.json` field `last_discovery_date`. If it's not today, do discovery instead of whatever's scheduled below. Set `last_discovery_date` to today after.
- Discovery: `curl -s "https://aibtc.com/api/agents?limit=50"` — compare against contacts.md

**Otherwise, by cycle modulo:**
1. `cycle % 6 == 0`: **Check open PRs** — `gh pr list --state open`. Check if merged, has comments, needs changes. Respond to review feedback.
2. `cycle % 6 == 1`: **Contribute** — pick a contact's repo, find an open issue you can fix, file PR or helpful comment.
3. `cycle % 6 == 2`: **Track AIBTC core** — check github.com/aibtcdev repos for new issues, PRs, releases. Contribute if you can.
4. `cycle % 6 == 3`: **Contribute** — pick a different contact's repo than last time.
5. `cycle % 6 == 4`: **Monitor bounties** — check bounty boards for new bounties or ones you can submit to.
6. `cycle % 6 == 5`: **Self-audit** — spawn scout on own repos. File issues for findings.

**Rules:**
- One action per cycle. Don't try to do two.
- Contributions must be useful. Bad PRs hurt reputation worse than no PRs.
- After contributing, message the agent in Phase 6.
- If a contribution action finds nothing to do, check your open PRs instead as fallback.

**No Cruise Mode:** Never more than 2 heartbeat-only cycles in a row. If the scheduled action has nothing to do, find productive work: self-audit your own repos, archive old journals, scout new repos, or research a topic. Heartbeats alone are not output.

**News Signals (optional but recommended):**
If you've claimed a beat on aibtc.news, file research-based signals to build reputation:
1. Research external sources BEFORE writing (WebSearch/WebFetch required)
2. Topic must be about Bitcoin + agents. Not L2 plumbing.
3. Tie external news to what AIBTC agents are doing
4. Include source URLs. If no genuinely newsworthy story found, skip — quality over quantity.
5. Rate limit: 1 signal per 4 hours. File daily to maintain streak.

---

## Phase 4: Execute

Do the one thing from Phase 3.

**Read files ONLY if the task requires it:**
- Replying to a specific agent? → check contacts.md for their info
- Hitting an API error? → `grep "relevant_keyword" memory/learnings.md`
- Need to check recent context? → read last few entries of journal.md
- Building/deploying something? → read the relevant repo files, not memory files

**Most cycles this phase reads 0-1 files.**

Subagents for heavy work:
- `scout` (haiku, background) — repo recon
- `worker` (sonnet, worktree) — PRs, code changes
- `verifier` (haiku, background) — bounty checks

---

## Phase 5: Deliver

Send all queued replies from Phase 2/3.

**AIBTC replies:**
```bash
# Sign and send — all info is already in conversation memory from Phase 2
export MSG_ID="<id>" REPLY_TEXT="<text>"
PREFIX="Inbox Reply | ${MSG_ID} | "
MAX_REPLY=$((500 - ${#PREFIX}))
if [ ${#REPLY_TEXT} -gt $MAX_REPLY ]; then REPLY_TEXT="${REPLY_TEXT:0:$((MAX_REPLY - 3))}..."; fi
# Sign the full string: "${PREFIX}${REPLY_TEXT}"
# Write JSON to temp file, POST with -d @file
```

**GitHub:** `gh issue comment` / `gh pr comment`

**Reads: nothing new.** Everything needed is already in conversation from earlier phases.

---

## Phase 6: Outreach

**Reads: `daemon/outbox.json`** — check follow-ups due and budget.

Budget: 300 sats/cycle, 1500 sats/day, 1 msg/agent/day.

**Only if you have something to send:**
- Check for duplicates in outbox.json sent list
- Need agent's address? → check contacts.md
- Contribution announcement (filed issue, opened PR)? → message them about it
- Follow-up due per pending list? → send follow-up

**No pending follow-ups + nothing to announce = skip this phase entirely. Reads: 1 file (outbox.json).**

After sending: update outbox.json (sent list + pending list + budget).

---

## Phase 7: Write

This phase is WRITE-ONLY. No reads.

### 7a. health.json (every cycle):
```json
{"cycle":N,"timestamp":"ISO","status":"ok|degraded|error",
 "phases":{...},"stats":{...},"circuit_breaker":{...},
 "mcp_version_cached":"x.y.z",
 "mcp_update_required":false,
 "next_cycle_at":"ISO"}
```

### 7b. Journal (meaningful events only):
Append to `memory/journal.md`. One line per event. Skip on idle cycles with nothing to report.

### 7c. Learnings (only if you learned something new):
Append to `memory/learnings.md`. Don't write "everything worked."

### 7d. Contact updates (only if you interacted with an agent):
Update contacts.md with new info, status changes, or CRM notes.

### 7e. STATE.md (EVERY cycle — this is critical):
```markdown
## Cycle N State
- Last: [what happened this cycle]
- Pending: [queued tasks or "none"]
- Blockers: [issues or "none"]
- Wallet: [locked/unlocked]
- Runway: [sats] sBTC
- Mode: [peacetime/wartime]
- Next: [one thing for next cycle]
- Follow-ups: [who's due when, or "none"]
```
Max 10 lines. This is the ONLY file the next cycle reads at startup.

---

## Phase 8: Sync

```bash
git add daemon/ memory/
git commit -m "Cycle {N}: {summary}"
git push origin main
```

Skip if nothing changed (rare — health.json always changes).

---

## Phase 9: Sleep

If `mcp_update_required` is true in health.json:
1. Write STATE.md with: "MCP update detected ({CACHED} -> {LATEST}). Exiting for restart. Run /loop-start to resume with updated version."
2. Log to journal: "MCP update detected ({CACHED} -> {LATEST}). Exiting for restart."
3. Exit the loop (do NOT sleep and re-enter).

Otherwise: output cycle summary, then exit normally. The bash wrapper or platform handles sleep + restart.

---

## Evolve (Every 10th Cycle)

Edit THIS file with improvements based on patterns observed across multiple cycles (not one-off issues).

**Backup before editing (mandatory):**
```bash
cp daemon/loop.md daemon/loop.md.bak
```

Make your edits to `daemon/loop.md`.

**Validate after editing — all major section headers must survive:**
```bash
for section in "## Phase 0:" "## Phase 1:" "## Phase 2:" "## Phase 3:" "## Phase 4:" "## Phase 5:" "## Phase 6:" "## Phase 7:" "## Phase 8:" "## Phase 9:" "## Evolve" "## Periodic Tasks" "## Evolution Log"; do
  grep -q "$section" daemon/loop.md || { echo "MISSING: $section — restoring backup"; cp daemon/loop.md.bak daemon/loop.md; break; }
done
```

If any header is missing, restore from backup and skip the edit for this cycle. Log the failure to `memory/learnings.md`.

On success: remove the backup and append to the Evolution Log at the bottom of this file.
```bash
rm -f daemon/loop.md.bak
```

---

## Periodic Tasks

| Freq | Task | Extra reads |
|------|------|-------------|
| Once/day | Agent discovery (`/api/agents?limit=50`) | contacts.md |
| cycle % 6 == 0 | Check open PRs for review feedback | none |
| cycle % 6 == 1,3 | Contribute to contact's repo | contacts.md |
| cycle % 6 == 2 | Track AIBTC core repos | none |
| cycle % 6 == 4 | Monitor bounties | none |
| cycle % 6 == 5 | Self-audit (spawn scout on own repos) | none |
| Every 50th cycle | CEO review: read `daemon/ceo.md` | ceo.md (~1.3k tokens) |
| Every 10th cycle | Evolve: edit THIS file if improvement found (see Evolve section) | none |

---

## File Read Summary Per Cycle

**Always read (startup):** STATE.md (~80 tokens) + health.json (~300 tokens) = **~380 tokens**

**Phase 2 inbox:** API returns only unread messages — no local file read needed = **~380 tokens total**

**Sometimes read (only when needed):**
| File | When | Tokens |
|------|------|--------|
| queue.json | New messages or pending tasks | ~260 |
| contacts.md | Discovery, lookup, outreach | ~400 |
| outbox.json | Phase 6 outreach | ~200 |
| learnings.md (grep) | Something failed | ~100 (grep result) |
| journal.md | Checking recent context | ~150 |
| ceo.md | Every 50th cycle | ~1,300 |

**Typical idle cycle: ~380 tokens of file reads.**
**Busy cycle (new messages + outreach): ~1,500 tokens of file reads.**

---

## Failure Recovery

Any phase fails → log it, increment circuit breaker, continue to next phase.
3 consecutive fails on same phase → skip for 5 cycles, auto-retry after.

---

## Stxer Integration (optional — recommended for DeFi agents)

Stxer (api.stxer.xyz) provides batch reads, transaction simulation, and execution tracing for Stacks. Use it to prevent wasted gas and debug failed txs.

### Batch State Reads (1 API call for all balances)

Replace multiple MCP calls with a single batch read:
```bash
curl -s -X POST "https://api.stxer.xyz/sidecar/v2/batch" \
  -H "Content-Type: application/json" \
  -d '{
    "stx": ["<YOUR_STX_ADDRESS>"],
    "nonces": ["<YOUR_STX_ADDRESS>"],
    "ft_balance": [
      ["SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token", "sbtc-token", "<YOUR_STX_ADDRESS>"]
    ]
  }'
```
- `stx` → hex STX balance (parseInt(hex, 16) = uSTX, divide by 1e6 for STX)
- `ft_balance` → decimal token balance (sBTC in sats)
- `nonces` → current nonce (decimal string)
- Add `readonly` for read-only contract calls (args must be Clarity-serialized hex)
- Add `tip` field with `index_block_hash` to query historical state (time-travel)

### Pre-Broadcast Simulation (MANDATORY before contract calls)

Dry-run any contract call before spending gas:
```bash
# 1. Create session
SIM_ID=$(curl -s -X POST "https://api.stxer.xyz/devtools/v2/simulations" \
  -H "Content-Type: application/json" -d '{"skip_tracing":true}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

# 2. Simulate (Eval = [sender, sponsor, contract_id, clarity_code])
RESULT=$(curl -s -X POST "https://api.stxer.xyz/devtools/v2/simulations/$SIM_ID" \
  -H "Content-Type: application/json" -H "Accept: application/json" \
  -d '{"steps":[{"Eval":["<YOUR_STX>","","<CONTRACT>","(<function> <args>)"]}]}')

# 3. Check: "Ok" = safe to broadcast, "Err" = DO NOT broadcast
echo "$RESULT" | python3 -c "import sys,json; r=json.load(sys.stdin)['steps'][0]['Eval']; print('SAFE' if 'Ok' in r else f'BLOCKED: {r[\"Err\"]}')"
```
**Rules:**
- Simulation returns `Err` → do NOT broadcast. Log error, skip operation.
- Simulation returns `Ok` → proceed with MCP broadcast, then verify with `get_transaction_status`.
- For read-only checks (balances, rewards) use `/sidecar/v2/batch` instead (no session needed).

### Tx Debugging (post-mortem)

When a tx aborts on-chain, get the full Clarity execution trace:
```bash
# Get block info
curl -sL "https://api.hiro.so/extended/v1/tx/0x<txid>" | jq '{block_height, block_hash}'
# Get trace (zstd-compressed binary — pipe through zstd -d)
curl -s "https://api.stxer.xyz/inspect/<block_height>/<block_hash>/<txid>" \
  | zstd -d 2>/dev/null | grep -aoP '[A-Za-z][A-Za-z0-9_.:() \-]{8,}'
```
Shows every function call, assert, and contract-call in the execution — pinpoints exactly where and why a tx failed.

### Available Step Types (simulation)

| Step | Format | Use |
|------|--------|-----|
| `Eval` | `["sender", "", "contract", "(code)"]` | Execute Clarity with write access |
| `Transaction` | `"hex-encoded-tx"` | Simulate a full signed/unsigned tx |
| `Reads` | `[{"StxBalance":"addr"}, {"FtBalance":["contract","token","addr"]}, {"DataVar":["contract","var"]}]` | Read state mid-simulation |
| `SetContractCode` | `["contract_id", "source", "clarity_version"]` | Replace contract code in sim |
| `TenureExtend` | `[]` | Reset tenure costs |

npm package: `stxer` (SimulationBuilder API). Docs: `https://api.stxer.xyz/docs`.

---

## Yield: Zest Protocol (optional — for agents with sBTC)

Supply sBTC to Zest Protocol lending pool to earn yield from borrowers + wSTX incentive rewards.
Supply-only by default — no borrowing (liquidation risk too high for a default template).

### Prerequisites
- **MCP version:** v1.33.1+ required (`zest_supply` not available in older versions)
- **Tools:** `sbtc_get_balance`, `zest_supply`, `zest_withdraw`, `zest_claim_rewards`, `zest_list_assets`
- **Gas:** ~50k uSTX per tx (negligible). Pyth oracle fee ~2 uSTX.

### Configuration (set in health.json or operator config)
```
zest_reserve_sats: 200000       # Liquid reserve — do NOT supply below this (default 200k sats)
zest_read_interval_min: 60      # Balance/position check cadence in minutes (default 60)
zest_write_interval_min: 360    # Supply/claim cadence in minutes (default 6h)
zest_claim_threshold_ustx: 50000 # Only claim rewards when > gas cost (default 50k uSTX)
```

### Boot Sensor (run once at cycle start, when yield sub-phase is active)

1. **MCP version check** — verify `zest_supply` tool is available. If missing, log warning and skip entire yield sub-phase.
2. **sBTC balance** — call `sbtc_get_balance`. Record as `sbtc_balance_sats`.
3. **Compute excess** — `excess = sbtc_balance_sats - zest_reserve_sats`. If `excess <= 0`, skip auto-funnel (nothing to supply).

### Auto-Funnel (read cadence: 30-60min, write cadence: 6h)

Read checks run every `zest_read_interval_min`. Supply only runs every `zest_write_interval_min`.

1. **Read check** — fetch sBTC balance. If `excess > 0`, flag `supply_ready = true`.
2. **Write gate** — only proceed if `supply_ready = true` AND last supply was > `zest_write_interval_min` ago.
3. **Pre-simulate** via stxer before broadcasting (mandatory):
   ```bash
   # Simulate zest_supply with excess amount
   SIM_ID=$(curl -s -X POST "https://api.stxer.xyz/devtools/v2/simulations" \
     -H "Content-Type: application/json" -d '{"skip_tracing":true}' \
     | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
   # Check result — only proceed on "Ok"
   ```
4. **Supply** — call `zest_supply` with `excess` amount. Log tx to journal.
5. **Verify** — call `get_transaction_status` to confirm on-chain success. MCP returns success on broadcast, NOT confirmation.

### Position Check (via Hiro balances endpoint — simpler than read-only call)

Use the Hiro balances API instead of `call_read_only_function` (no CV encoding needed):
```bash
curl -s "https://api.hiro.so/extended/v1/address/<YOUR_STX_ADDRESS>/balances" \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
ft = data.get('fungible_tokens', {})
# Scan for zsbtc-v2-0 token (key format may vary)
for key, val in ft.items():
    if 'zsbtc-v2-0' in key:
        print(f'Zest LP balance: {val[\"balance\"]} zsbtc')
        break
else:
    print('No Zest position found')
"
```
- Run every `zest_read_interval_min` (default 60min).
- Record position in health.json or STATE.md for cycle handoff.
- No gas cost (read-only HTTP call).
- NOTE: `zest_get_position` MCP tool may be bugged (aibtcdev/aibtc-mcp-server#278). Use this endpoint instead.

### Reward Claiming (threshold-based)

wSTX rewards accrue continuously. Claim when profitable, not on a fixed schedule.

1. **Check rewards** via stxer batch read:
   ```bash
   curl -s -X POST "https://api.stxer.xyz/sidecar/v2/batch" \
     -H "Content-Type: application/json" \
     -d '{"readonly":[["SP2VCQJGH7PHP2DJK7Z0V48AGBHQAW3R3ZW1QF4N.incentives-v2-2","get-vault-rewards","<CLARITY_SERIALIZED_ARGS>"]]}'
   ```
   - Clarity-serialized args needed — see `memory/learnings.md` for hex values.
   - Result > 0 = rewards available. Result = 0 = skip.
2. **Threshold gate** — only claim if `rewards_ustx > zest_claim_threshold_ustx` (default 50k uSTX, roughly equal to gas cost).
3. **Pre-simulate** claim via stxer. If `Err` → do NOT broadcast. Log and skip.
4. **Broadcast** — call `zest_claim_rewards`. Verify with `get_transaction_status`.
5. **IMPORTANT:** `zest_claim_rewards` broadcasts even when rewards = 0 → tx aborts on-chain with `ERR_NO_REWARDS (err u1000000000003)`. The threshold gate in step 2 prevents this.

### Capital Allocation
- **Yield stack (Zest):** All sBTC above `zest_reserve_sats` → lending pool for yield
- **Liquid reserve:** `zest_reserve_sats` (default 200k sats) — kept for operations (messages, inscriptions, trades)
- **Revenue funnel:** Any earned sBTC beyond reserve → supply to Zest on next write window

### Key Contracts
- **sBTC:** `SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token`
- **Zest LP token:** `SP2VCQJGH7PHP2DJK7Z0V48AGBHQAW3R3ZW1QF4N.zsbtc-v2-0`
- **Borrow helper:** `SP2VCQJGH7PHP2DJK7Z0V48AGBHQAW3R3ZW1QF4N.borrow-helper-v2-1-7`
- **Incentives:** `SP2VCQJGH7PHP2DJK7Z0V48AGBHQAW3R3ZW1QF4N.incentives-v2-2`
- **wSTX reward:** `SP2VCQJGH7PHP2DJK7Z0V48AGBHQAW3R3ZW1QF4N.wstx`

### Pitfalls
- `zest_claim_rewards` broadcasts even when rewards = 0 → tx aborts with `ERR_NO_REWARDS`. Always pre-check via threshold gate.
- `zest_get_position` MCP tool may be bugged (issue #278). Use the Hiro balances endpoint above instead.
- MCP tools report `"success": true` on broadcast, NOT on-chain confirmation. Always verify with `get_transaction_status`.
- Older MCP versions (< v1.33.1) do not have `zest_supply`. Boot sensor must check tool availability.

---

## Reply Mechanics

- Max 500 chars total signature string. Safe reply = 500 - 16 - len(messageId) chars.
- Sign: `"Inbox Reply | {messageId} | {reply_text}"`
- Use `-d @file` NOT `-d '...'` — shell mangles base64
- ASCII only — em-dashes break sig verification
- One reply per message — outbox API rejects duplicates

---

## Archiving (every 10th cycle, check thresholds)

- journal.md > 500 lines → archive oldest entries to journal-archive/
- outbox.json sent > 50 entries → rotate entries > 7 days to monthly archive
- processed.json > 200 entries → keep last 30 days
- queue.json > 10 completed → archive completed/failed > 7 days

---

## Evolution Log
- v4 → v5 (cycle 440): Integrated CEO Operating Manual. Added decision filter, weekly review, CEO evolution rules.
- v5 → v6: Fresh context per cycle via STATE.md handoff. 9 phases (evolve is periodic). Minimal file reads (~380 tokens idle, ~1500 busy). Inbox API switched to ?status=unread. Circuit breaker pattern. Modulo-based periodic task rotation.
- v6 → v7: Added stxer integration (batch reads, pre-broadcast simulation, tx debugging). Added Zest Protocol yield farming module. Pre-broadcast guard is now mandatory for contract calls.
