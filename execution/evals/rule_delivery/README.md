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

## Public helper interface

`fixture_evidence` is stdlib-only and has no store-writing or backend-selection
entry point. Dependent evaluators import this implementation rather than copy it:

| Symbol | Contract |
| --- | --- |
| `decode_input_json(text, path, errors)` | Decode bounded JSON, appending field-labelled diagnostics to the supplied list; return `None` on failure. JSON `null` also returns `None`, so consult `errors`. |
| `structure_errors(value, path)` | Iterative JSON-compatible shape/depth/node validation for already constructed values; return a list of diagnostics. Domain-specific validation stays with the caller. |
| `encode_json(value, path="record", limit=MAX_INPUT_BYTES)` | Deterministic compact UTF-8 bytes; raise `EvidenceError` on unsupported representation, nonfinite numbers or bounds. |
| `read_json_file(path, label="state")` | Return `(value, raw_bytes)` after a bounded read and decode; raise `EvidenceError` or the actual I/O exception. |
| `read_frames(binary_stream, limit=MAX_INPUT_BYTES, byte_limit=None, row_limit=None)` | Yield `(payload_bytes_or_none, framing_error_or_none)` for LF-separated frames. Optional total limits stop before another stream read, including reads used to drain oversized frames. LF is outside the payload budget but inside the total byte budget. |
| `normalize_records(records, exclude_correlation=None, initial_issues=None)` | Normalize already decoded rows into the result described below. |
| `read_trace(path, exclude_correlation=None)` | Bounded trace reading plus normalization, with explicit unreadable/truncated/invalid diagnostics. |

Default budgets are 1 MiB per payload/file/serialized record, 64 nesting levels
and 1,000,000 visited JSON nodes. Trace reading stops at 100,000 rows or its
64 MiB actual stream-consumption budget, including delimiters and discarded or
drained bytes, preserving an explicit limit diagnostic. Exhausted byte/row
budgets prevent fetching another frame; an exhausted byte budget also prevents
further draining of the current frame. Known earlier attempts survive, while
the remaining input stays unqualified. No EOF lookahead is performed after a
budget is exhausted, even if it happens to coincide with the file's end.
Ongoing stdio uses the same frame reader without a total trace budget so a
malformed frame can drain to its delimiter and a later healthy request can run.
These are
synthetic instrument resource bounds, not changes to worker authority. Invalid
UTF-8, unsupported numbers, deep JSON and malformed envelopes never dispatch a
handler. A final valid JSON row without LF may retain known facts, but its
truncated framing always leaves the trace incomplete; stdio never dispatches a
truncated frame.

## Version 1 observations and normalization

All versioned rows have `trace_version: 1`, an `event`, and `correlation_id`.
Received, completed and response rows bind the same deterministic
`request_sha256`. The receipt preserves the bounded original RPC `request`,
including malformed argument values. A new correlation is generated for every
attempt, independent of its RPC ID. The events are:

- `received`: checked append and flush before nested validation or handler use.
- `completed`: actual `handler_outcome` (`returned`, `refused`,
  `validation_error`, `exception` or `not_executed`), `result_status`, the actual
  result when representable, and independent `state_capture` evidence.
- `response`: `delivery` (`written`, `inprocess_return` or `failed`) and any
  encoding/write diagnostic. A possibly partial write is never retried.
- `protocol`: an explicit observation gap. If an initial receipt failed but a
  later diagnostic append succeeds, its original request can remain visible as
  weaker `failed_pre_dispatch_observation`, with `handler_started: false`.
  It never becomes a checked receipt. Persistent trace I/O failure is also
  reported on stderr and remains an infrastructure failure.

`read_trace` and `normalize_records` return:

```text
{
  attempts: [logical_attempt, ...],
  complete: boolean,
  issues: [{code, correlation_id?, index?, ...}, ...],
  provenance: observed_v1 | legacy_or_mixed | unavailable
}
```

Each normalized attempt retains `request`, raw `args`, safe string-or-null
`tool`, raw `received_tool`, `rpc_id`, `correlation_id`, `request_sha256`,
`receipt_observed`, `provenance`, `complete`, `issues`, `handler_outcome` and
`response_delivery`. Available actual `result`, `state_sha256`,
`planning_sha256`, `persisted_entities`, `state_error`, `outcome_error` and
`response_error` are retained. Missing results stay absent. A representable
malformed handler result stays visible while its shape is unqualified. State
capture has an actual file hash; action observations also capture persisted
entities. A null planning hash means the graph shape could not be fingerprinted,
not that its planning state passed validation. Domain-specific fields inside
persisted entities are intentionally not repaired by this producer.

`complete` establishes only this cooperative observation contract. A refused or
invalid request can be completely observed; it is not a successful user action.
A missing/duplicate/mismatched terminal, unavailable state/result, handler
exception or uncertain response marks incomplete evidence without erasing known
facts. Legacy rows retain `legacy_completed_only` provenance, `receipt_observed:
false` and `complete: false`. No empty trace, missing result or disappearance of
an issue is proof of a successful effect. Consumers must inspect completeness
and independently score state and authorized outcomes.

## Scripted transitions and test coverage

Host-authored scripted events use normalized prior logical attempts. Current
receipts and terminal duplicates are excluded from the call count. Identified
incomplete attempts still count once; unidentifiable or corrupt prior requests
hold a transition with an explicit handler error. Existing `on_match` and
once-only applied-event markers retain their boundaries. A later call is not
an automatic replay of an uncertain earlier action.

Run the producer and helper regressions with the locked supported environment:

```sh
uv sync --frozen --extra dev --python 3.13
uv run --frozen --extra dev python -m pytest execution/evals/rule_delivery/ -q
uv run --frozen --extra dev python -m pytest execution/evals/ -q
uv run --frozen --extra dev python -m pytest execution/scripts/ -q
```

The stdio tests initialize the real service, then exercise malformed requests,
healthy follow-up calls, valid read/write/refusal controls, reused RPC IDs across
restarts, checked receipt write/flush faults, handler/state/result failures and
partial response writes. Fault injection is test-local; no request can select a
handler or inject runtime code. These tests establish the synthetic instrument,
not model behavior, instruction adherence or native session recovery.
