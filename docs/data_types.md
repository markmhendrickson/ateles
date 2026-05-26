# Ateles — Neotoma Data Types

Canonical reference for all Neotoma entity types owned or consumed by the Ateles swarm. Each entry lists the schema version, key fields, and relationships to other entity types.

Schema IDs are stable Neotoma UUIDs; use them when calling `retrieve_entity_by_identifier` or when registering schema-linked relationships.

---

## Governance schemas

### `execution_policy`
**Schema ID**: `0e61f23f-b1bd-46a3-8824-9dde710db9e6` · **v1.0**

Per-plan autonomy calibration for swarm execution. Defines permission scope, quality criteria, blocking checkpoints, and fallback instructions.

| Field | Type | Required | Notes |
|---|---|---|---|
| `title` | string | ✓ | Human-readable label |
| `plan_entity_id` | string | | The plan this policy governs |
| `swarm_confidence` | string | ✓ | `low` / `medium` / `high` |
| `permission_scope` | array | ✓ | List of allowed action types |
| `quality_criteria` | array | | Pass/fail acceptance criteria |
| `checkpoints` | array | | Blocking review gates |
| `assigned_agents` | array | | AAuth subs of authorised agents |
| `fallback_instruction` | string | | What to do on failure |
| `status` | string | | `active` / `paused` / `retired` |

### `checkpoint_brief`
**Schema ID**: `b0bfcfab-1f07-4526-8fa5-d5ace343b004` · **v1.0**

Snapshot record of a checkpoint review: what was assessed, what was decided, by whom.

---

## Attribution schemas

### `participation_record`
**Schema ID**: `682a3df1-1d1c-4f9d-8c95-52f1c1130b43` · **v1.2.0**

Gate-progression persistence. One record per `(work_entity_id, gate_name)` pair. Written by Anthus at dispatch; updated at satisfaction/skip.

| Field | Type | Required | Notes |
|---|---|---|---|
| `work_entity_id` | string | ✓ | The issue/PR entity being worked |
| `gate_name` | string | ✓ | Workflow gate (canonical_name anchor) |
| `agent` | string | ✓ | AAuth sub of the dispatched agent |
| `status` | string | ✓ | `pending` / `dispatched` / `satisfied` / `skipped` |
| `workflow_definition_id` | string | | Entity ID of the active workflow_definition |
| `dispatched_at` | string | | ISO 8601 |
| `satisfied_at` | string | | ISO 8601 |
| `skipped_at` | string | | ISO 8601 |
| `artifact_ref` | string | | Entity ref of the emitted artifact |
| `skip_reason` | string | | Why the gate was skipped |
| `aauth_token_jti` | string | | JTI of the AAuth token used |
| `agent_definition_ref` | string | | Entity ID of the agent_definition at dispatch |
| `agent_definition_observation_id` | string | | Observation ID that produced the loaded snapshot |
| `agent_strategy_ref` | string | | Active agent_strategy entity at dispatch |
| `version` | string | | Schema version stamp |

**canonical_name_fields**: `[work_entity_id, gate_name]`

### `retrieval_event`
**Schema ID**: `23d7a165-bd82-4804-9f3c-4811317f2df5` · **v1.0**

Records every `retrieve_*` call made by a dispatched agent. Created automatically by `mcpsrv_neotoma` when `ATELES_PARTICIPATION_REF` is set in the environment (see [ateles#23](https://github.com/markmhendrickson/ateles/issues/23)).

| Field | Type | Required | Notes |
|---|---|---|---|
| `agent_sub` | string | ✓ | AAuth sub of the calling agent |
| `tool_call_name` | string | ✓ | e.g. `retrieve_entities`, `retrieve_entity_by_identifier` |
| `query` | string | | Free-text search query if used |
| `filters` | object | | Structured filters (entity_type, etc.) |
| `result_entity_ids` | array | | Entity IDs returned |
| `result_count` | number | | Count of results |
| `truncated` | boolean | | Whether the result was capped by limit |
| `latency_ms` | number | | Round-trip latency |
| `participation_record_ref` | string | | Links to the enclosing participation_record |
| `at` | string | | ISO 8601 timestamp |

**canonical_name_fields**: `[agent_sub, tool_call_name, at]`

### `agent_action_observation`
**Schema ID**: `dffff4f3-c8bd-4a25-9185-085e51cbaf75` · **v1.0**

Records a single tool call or decision emitted by an agent during a gate. Written by the harness at artifact emit.

| Field | Type | Required | Notes |
|---|---|---|---|
| `agent_sub` | string | ✓ | AAuth sub |
| `tool` | string | ✓ | Tool or action name |
| `pat_attribution` | string | | GitHub PAT identity used |
| `owner` | string | | GitHub owner |
| `repo` | string | | GitHub repo |
| `started_at` | string | | ISO 8601 |
| `finished_at` | string | | ISO 8601 |
| `success` | boolean | | Whether the action succeeded |
| `result_summary` | string | | Short human-readable outcome |
| `error` | string | | Error message on failure |
| `aauth_token_jti` | string | | JTI of AAuth token |
| `participation_record_ref` | string | | Links to the enclosing participation_record |
| `inputs_consulted` | array | | Subset of retrieval_events the agent relied on; each item: `{entity_id, schema_version, retrieved_via_event_ref}` |

**canonical_name_fields**: `[agent_sub, tool, started_at]`

---

## Policy schemas

### `agent_policy`
**Schema ID**: `74c79ceb-161b-49d4-b2ef-de2ef5c2168f` · **v1.0**

Declarative policy rules scoped to an agent or domain. Used by the harness to gate tool calls and by Onychomys to surface override conflicts.

| Field | Type | Required | Notes |
|---|---|---|---|
| `scope` | string | ✓ | `global` / `agent` / `domain` |
| `rule_kind` | string | ✓ | `allow` / `deny` / `require` / `prefer` |
| `description` | string | ✓ | Human-readable rule text |
| `agent_sub` | string | | AAuth sub if agent-scoped |
| `domain` | string | | Domain tag (canonical_name anchor) |
| `rule` | string | | Machine-readable rule expression |
| `effective_from` | string | | ISO 8601 |
| `effective_until` | string | | ISO 8601 (null = indefinite) |
| `supersedes` | string | | Entity ID of superseded policy |
| `overridable_by` | string | | AAuth sub that may override |
| `status` | string | | `active` / `suspended` / `retired` |

**canonical_name_fields**: `[domain, rule_kind, description]`

---

## Agent identity schemas

### `agent_definition`
**Schema**: in Neotoma (`ent_99ace4dd6673aa36ed08b1fe` references key IDs) · **v1.5.0**

Canonical identity record for every agent in the swarm. Loaded by `AgentLoader` at daemon startup and by Anthus at gate dispatch.

See [taxonomy.md](taxonomy.md) for the full agent roster and AAuth sub assignments.

---

## Observability schemas

### `daemon_report`
**Schema ID**: `a9ea8131-502f-44e7-87a6-8149bab7d55c` · **v1.0**

Structured status/error report emitted by T3 daemons. Anthus surfaces `error` and `critical` severity reports to Onychomys.

### `harness_event`
**Schema ID**: `689230f4-cd83-49b6-baa7-a752cf70629d` · **v1.0**

Low-level harness lifecycle events (gate open, artifact emit, gate close).

### `escalation`
Escalation entities are created by any daemon or agent when a condition requires operator attention. Anthus forwards all escalations to Onychomys via Telegram.

---

## Strategy hierarchy schemas

Four-layer DAG: `business_strategy → domain_strategy → agent_strategy → agent_definition`. Execution-only agents (Gryllus, Vanellus, Struthio, Regulus) inherit from `domain_strategy` without a personal `agent_strategy` layer.

### `business_strategy`
**Schema ID**: `21a8a4bb-c5ca-465e-b4ed-2e703a18a8c5` · **v1.0**

Root strategy entity. One per product/company. Fields: `title` (required), `vision`, `north_star_metric`, `time_horizon`, `success_criteria` (array), `constraints` (array), `status`, `version`, `notes`.

### `domain_strategy`
**Schema ID**: `7089442f-413d-4713-a85c-c4d38ee0893b` · **v1.0**

Per-product-area strategy (product, eng, GTM, compliance, ops). Links to `business_strategy_ref`. Fields: `title` + `domain` (required), plus `objective`, `key_results`, `success_criteria`, `assigned_agents`, `time_horizon`, `status`.

**canonical_name_fields**: `[domain, title]`

### `agent_strategy`
**Schema ID**: `47f33921-8649-4e42-82d1-35cf6a20013c` · **v1.0**

Per-strategic-agent strategy (Pavo, Bombycilla, Accipiter, Ciconia, Buteo, Columba). Links to `domain_strategy_ref`. Fields: `agent_sub` + `title` (required), plus `objective`, `success_criteria`, `evaluation_schedule`, `drift_signal_threshold`, `status`.

**canonical_name_fields**: `[agent_sub, title]`

### `strategy_revision_proposal`
**Schema ID**: `b4577273-5775-49e6-af63-7d96e0b40168` · **v1.0**

Created automatically when a pattern of `strategy_drift_signal` lines accumulates. Fields: `proposing_agent_sub` + `target_entity_id` + `proposed_at` (required), plus `target_entity_type`, `summary`, `drift_signal_refs`, `proposed_change`, `affects_higher_layer`, `operator_decision`, `operator_notes`, `status`.

**canonical_name_fields**: `[proposing_agent_sub, target_entity_id, proposed_at]`

### `strategy_evaluation_report`
**Schema ID**: `94a29cad-f663-42af-8953-2ab02c1d976d` · **v1.0**

Periodic self-evaluation by a strategic agent against its `agent_strategy`. Fields: `agent_sub` + `agent_strategy_ref` + `evaluated_at` (required), plus `period_start`, `period_end`, `observations_reviewed`, `criteria_scores`, `overall_score`, `drift_signals_found`, `revision_proposals`, `summary`, `status`.

**canonical_name_fields**: `[agent_sub, agent_strategy_ref, evaluated_at]`

---

## Payment schemas

### `payment_profile`
**Schema ID**: `8f10fe72-2924-422c-b2ee-d537d9952576` · **v1.0**

Recurring payment configuration used by Monedula. Contains IBANs and transfer amounts — never commit this schema's entity data to plaintext files.
