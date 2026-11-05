# Planning-spine reporting eval

This eval exercises `continue-session` and `digest` through ordinary slash-command
invocation in the repository's sandboxed Claude-session harness. It reuses the
rule-delivery eval's session driver, path guard, and fixture Neotoma server.
The default run includes both a single-workstream `continue-session` scenario
and a whole named-session scenario with thirteen lanes distributed across a
transcript, workboard, linked tasks, and terminal handoff. A similarly named
narrow plan is deliberately easy to find first.

The fixture is intentionally public-safe and synthetic. Its graph preserves the
behavior under review:

- a master plan with canonical phases and exit-gate states in record order;
- one workstream structurally bound to phase B2 even though its task title says
  "Phase A";
- one E2-labelled workstream attached directly to the master plan, with no phase
  binding;
- one workstream with two distinct canonical phase ancestors, B2 and E;
- one task with no direct `PART_OF` parent and one with two direct parents; and
- task mechanics named Repair, Review, Merge, and Deploy.

The evaluator fails the original task-stage-only report even when that report
contains every policy phrase the old checker searched for. A passing report must
show the selected master plan first, render its canonical phases and gate states
before task mechanics, place each uniquely bound workstream exactly once through
the graph edge, keep non-unique workstreams out of canonical phase sections,
report missing and duplicate phase ancestry per workstream, report missing and
duplicate task ascent per affected task, and keep `reconcile-planning`
retrospective. Stable fixture entity IDs bind these effects, so changing a human
title cannot change placement or make an equivalent report fail.

The named-session scorer derives the resumable population from that source
union and each canonical task/plan binding from real fixture `PART_OF` edges.
It requires each row to cite its source evidence and latest stored state,
validates task → plan → parent ancestry, rejects a superseded session shell in
favor of its proven canonical replacement, and requires an ambiguous lane to
expose both candidates without asserting either as canonical. A post-inventory
observation changes one lane after the terminal handoff so stale-state selection
goes red. The normal runner selects this scorer from scenario metadata, and a
first-source-only mutation also runs through `runner.run_scenario()`; coverage
cannot pass only through a detached unit-test helper or a prompt that contains
the answer key.

Four additional normal-invocation scenarios cover the safe recovery contract
before plan binding: an exact source that is not found, two corroborated
candidates, an exactly bound zero-lane session, and a bound session with an
unreadable required terminal source. Their scorers require checked surfaces or
bounded candidates, an explicit zero-count ledger for the empty case, and
preservation of both a readable lane and an unresolved lane plus bounded
retry/escalation for partly unreadable evidence. Each requires an explicit
no-domain-action and no-completeness-claim posture rather than treating absence,
ambiguity, zero lanes, or unavailability as a successful resume.

The three `fixtures/skills/*/SKILL.md` files are generated review evidence, not
sources. Neotoma prod remains canonical. `fixtures/review_bundle.json` records
the canonical locators, generation mode, and SHA-256 of every body and the
planning fixture. Regenerate from prod when credentials are available:

```sh
python3 execution/scripts/render_planning_spine_review_bundle.py
```

For credential-free recovery from an already-generated user mirror, pass its
root through an environment-relative path:

```sh
python3 execution/scripts/render_planning_spine_review_bundle.py \
  --source-root "${CODEX_HOME:-$HOME/.codex}/skills"
```

Run deterministic coverage and the credential-free harness setup:

```sh
python3 -m pytest -q execution/evals/planning_spine/test_planning_spine_eval.py
python3 execution/evals/planning_spine/planning_spine_runner.py --dry-run
```

A live effect run is opt-in because it spends model budget:

```sh
python3 execution/evals/planning_spine/planning_spine_runner.py --model sonnet
```

Transport failures are reported as infrastructure errors and are never scored as
skill behavior.

## Family continuation instrument cases

`--scenarios all` includes thirteen additional synthetic cases. Their fixture
variants share `family_base` in `context.json`; the runner expands the selected
variant before starting the existing fixture MCP server. Scoring criteria and
scripted event controls stay outside the worker workspace. No expectations are
placed in the model prompt. The requested JSON audit is an output interface,
not an answer key.

| Case | Required observable evidence |
| --- | --- |
| ordinary-single-lane | Exact task scope, current aggregate ascent, artifact bytes |
| recursive-paginated-family | Consumed pages, recursive native ancestry and acknowledged delegation |
| pagination-or-source-gap | Observed failure, unresolved coverage, independent owned lanes retained |
| changed-ascent-after-checkpoint | Later relationship read replaces the stale ascent |
| direct-late-answer-during-handoff | Fresh human answer and original obligation identity |
| origin-checkpoint-answer-sibling-open | Exact answer route and separate receiver-consumption read; sibling stays open |
| digest-report-only | Verification and tooling-gap scorecard; zero writes/actions |
| digest-default-history | Two turns, stable persisted digest, cumulative history and exact readback, local assigned action |
| inherited-event-attribution | Own child event separated from inherited parent history |
| positive-ack-qualified-custody | Existing successor acknowledgment, custody and observed local action |
| negative-ack-or-custody | No replacement action when custody is unqualified |
| actual-native-compaction | Separately qualified completed/persisted native transition and observed later turn |
| planned-and-abrupt-cross-harness | Separately qualified distinct receiver, acknowledgment and custody |

The runner captures before/after state, actual fixture calls and artifact hashes.
The opt-in backend implements local entity/relationship writes, stable digest
identity, paginated session/history reads, answer receipts and predeclared local
artifact actions. Answer queue acceptance is distinct from a later synthetic
receiver read. These are test counterparts, not production coordination tools.
Confirmed effects and the existing watch must remain unchanged; even an attempted
inert effect replay fails scoring. Legacy checkpoint/status history is retained.

The checked-in deterministic driver substitutes for `drive_session` while using
the actual runner, backend, persistence and scorer. Negative mutations demonstrate
that the instrument goes red for omissions, stale ascents, wrong questions,
queue-only delivery, report-only writes, history loss, unqualified replacement,
inherited-event credit and replay attempts. This is **instrument coverage**, not
causal evidence that a model follows the proposed instructions. Workspace logs
are evidence for cooperative fixture runs, not tamper-resistant audit storage.
The existing guard and no-real-MCP configuration are unchanged; documentation or
this offline run does not qualify their delivery in another harness.

The two native cases deliberately return an infrastructure error without
separately qualified host evidence. The current runner has no native-transition
capture adapter and cannot qualify either by a worker-written file or report.
The native-shape validator is a host-input contract, not provenance verification.
No actual compaction, abrupt-stop recovery, model invocation or cross-harness
continuation is performed by deterministic tests or `--dry-run`.

For this proposal bundle, `review_bundle.json` explicitly records
`source_mode: reviewed_proposal`: the two updated skill bodies are unapplied
review candidates with synthetic entity references. The third skill remains
byte-for-byte unchanged. These fixtures do not publish or install a canonical
policy/skill amendment. Regeneration from current prod would replace that
proposal evidence, so preserve the pinned review head when evaluating it.

```sh
uv sync --frozen --extra dev --python 3.13
uv run --frozen --extra dev python -m pytest -q \
  execution/evals/planning_spine/test_planning_spine_eval.py \
  execution/evals/rule_delivery/test_rule_delivery_evals.py
uv run --frozen --extra dev python -m pytest -q \
  execution/scripts/test_sync_skills_frontmatter.py \
  execution/scripts/test_planning_spine_skill_contract.py
uv run --frozen --extra dev python \
  execution/evals/planning_spine/planning_spine_runner.py \
  --scenarios all --dry-run --out /tmp/planning-family-dry-run-new
```

This evaluator support does not finish the broader preparation plan. Full
baseline coverage, canonical policy adoption, real model/instruction tests,
actual compaction and complete descendant recovery, acknowledged rollout and
workflow handover retain their own acceptance and review/effect gates.

### Output and evidence boundaries

Every family prompt receives the same pinned `family_audit_contract` from the
fixture. It declares all nested types, disposition/verdict enums and nullable
identifiers. A successor assessment additionally reports `successor.session_id`,
`successor.predecessor_id` and boolean `successor.qualified`; child-event
attribution reports `credited_event_ids` as an array of string identifiers. The
contract names fields and possible values, never the expected source IDs or
answers. Malformed output produces a saved failed result with field-level
`audit_errors`, and the CLI continues later scenarios. A malformed worker report
is distinct from a transport failure or an unavailable native observation.

The backend's single tool-effect inventory classifies action endpoints, including
refused close attempts, and defaults unknown capabilities to actions. Capability
availability does not grant scenario scope: only the declared action, original
answer route and default-mode digest writes are admitted by the scorer. The
receiver-read counterpart can consume an existing synthetic receipt, so
report-only also requires unchanged persisted state and artifacts.

Successful action traces capture actual persisted task/checkpoint/digest values.
All existing fixture task/checkpoint entities must survive unchanged, including
intermediate writes, and every successful digest update must retain both claims
and artifact history from the preceding committed snapshot. Custody, handoff
acknowledgment, receiver acceptance and capability opt-ins require literal JSON
`true`; missing, malformed and merely truthy values do not qualify.

Planning evidence requires complete, current-stage source reads with appropriate
outbound/both selectors and actual returned `PART_OF` edges. Unfiltered,
paginated global and related-entity reads can qualify the same ascent. Correct
asserted chains with unrelated, empty, partial or stale-stage relationship reads
cannot pass. The planning fingerprint is test provenance in cooperative fixture
traces; it creates no production authority or tamper-resistant control.

### Malformed persisted source and bounded JSON

Worker output and persisted fixture evidence are separate untrusted inputs. The
scorer validates consumed containers, identifiers, enum/lookup fields and digest
collections before iteration, hashing or history comparisons. It checks the
initial state, final state, returned snapshots and every successful intermediate
entity snapshot in the existing trace. A bad intermediate value remains a
field-specific `state_errors` failure even if a later write restores valid state;
it never becomes a qualified history baseline. The local store still permits
bad-state instrumentation writes, so tests prove actual successful persistence
and readback rather than qualifying only a backend refusal.

The existing input boundary limits each JSON report/state/trace record to 1 MiB
and 64 nesting levels; direct structured evidence also has an iterative node/depth
bound. Quoted brackets and escaped strings do not count as nesting. Unsupported
size/depth, invalid syntax and decoder resource-shape errors produce a saved
failed result with `audit_errors` or `state_errors`; later selected scenarios
continue. These are evaluator resource bounds, not new worker permissions or
human-consent quotas. Normal fixture reports/writes and all prior scenarios
retain their acceptance checks. Transport errors, unavailable fixture I/O and
missing native observations keep their separate infrastructure classification.

The regression controls compare exact prior source behavior with the repaired
normal runner/backend: malformed final collections, malformed successful first
writes followed by valid restoration, task/question identifier shapes, the
20,001-byte deeply nested report, and the one-byte syntax-error control. No
model/native operation or successful response/trace is fabricated by these
checks. They remain deterministic instrument proof only.
