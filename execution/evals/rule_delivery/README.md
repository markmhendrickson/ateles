# Synthetic fixture request evidence

This is the standalone local evidence producer for the existing rule-delivery
evaluator. It uses only the run's `neotoma_state.json`, optional host-side
`fixture_control.json` and existing `mcp_calls.jsonl`. It connects to no production
MCP server and supplies no authority, credential, model or native-session adapter.

Qualified stdio and in-process callers use `process_request` or
`observe_tool_call`. `call_tool` and `Store.log` remain low-level/legacy test
interfaces, not proof that every received attempt was observed.

The trace has three correlated records per ordinary tool call: `received`
(checked append/flush before validation or dispatch), `completed` (actual handler
outcome and available state), and `response` (server write/flush or in-process
return boundary). Correlations are distinct from reusable RPC IDs and generated
independently across process restarts. A written response does not establish a
client acknowledgment. No power-loss durability or tamper resistance is claimed.

`fixture_evidence.read_trace(path)` returns `attempts`, `complete`, `issues` and
`provenance`. An unfinished receipt has no fabricated result. Known handler
results, failed attempts and captured state remain visible even when observation
or response delivery is incomplete. Legacy completed-only rows are readable but
retain weaker provenance and cannot establish exhaustive pre-dispatch coverage.

Input, result, state and trace serialization are bounded. Unsupported protocol or
input receives an explicit diagnostic; initial receipt failure prevents dispatch.
Failures after possible effects never cause automatic handler replay. Scripted
transitions count prior logical attempts once, excluding the current receipt and
terminal duplicates, and preserve their existing once-only boundaries.

Opt-in synthetic persistence, pagination, sessions, answer acceptance/consumption
and confined artifact actions remain available. Structurally valid digest writes
may deliberately carry bad claim/artifact values for downstream state-validation
controls. Default-disabled capabilities and ordinary legacy correction/close
behavior remain. This is not a blanket write denial.

This prerequisite implements the producer split from issue #1593 and historical
PR #1594; it does not close their full family-evaluator acceptance. The dependent
consumer starts from this unit's accepted merge and owns all twenty scenarios,
proposal skills and final report-only interpretation. Model behavior, actual
compaction/cross-harness recovery, canonical adoption and installation remain
separate and unqualified.
