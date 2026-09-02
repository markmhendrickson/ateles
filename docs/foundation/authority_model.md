# Authority model: the tuple as implemented, where it fails open, and a verdict on "extension, not rewrite"

**Vision phase:** P1 (governed execution for one principal). **Kind:** consolidation with a verdict, not
design. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-20 to PR-28, section 4 the
single-principal inventory, C8, C9, C13, C14, C17, C19), prior art `ent_08460968e6f49dac21510f4a` (Track 2),
the README's Vision section, ateles#560, #561, #669, #487, #423, #471, #378. Code read on `origin/main` at
`496bab3`, 2026-09-02. `docs/aauth.md` is an input, not a source: #471 records it must be rewritten against
verified state.

## Purpose

State the authority tuple `principal + domain + scope + action + conditions + time` as the code implements
it, the paths where it fails open, the single-principal inventory as measured, and a verdict per extension
point on the README's claim that multi-operator is an extension of the entity model, not a rewrite. This is
the baseline the P2 and P3 documents (phase 2.1) extend from.

## Scope

`lib/daemon_runtime/grant_checker.py`, `agent_loader.py`, `aauth_signer.py`, `gating.py`,
`execution/mcp/mcp_tool_grant_proxy/proxy.py`, `execution/daemons/apis/a2a_gateway.py` and `a2a_executor.py`,
`lib/approval/`, `lib/notify/`, and the entities `agent_grant` (29), `execution_policy` (13), `agent_policy`
(25), `checkpoint_brief` (288), `operator_profile` (1). P2+ design is out of scope; its open decisions are
named, not answered.

## The tuple as implemented

| Term | Implemented as | Where |
|---|---|---|
| principal | the AAuth `(sub, iss)` pair for agents, realm literal `@ateles-swarm`; plus one operator as a GitHub login (`_OPERATOR_LOGIN`, env with a literal default), one email (`OPERATOR_EMAIL`), one chat id (`TELEGRAM_CHAT_ID`), and the magic value `agent_grant == "operator"` | `swarm_dispatch.py:275`, `github_gateway.py:63`, `email_channel.py:62-64`, `notifier.py:116-118`, `agent_loader.py:235-236` |
| domain, scope | `agent_grant.capabilities` (op × entity types × repos, per-tool `param_constraints`), matched on `sub` and `iss`; 20 of 29 auto-derived 2026-06-17; two human-device grants are full wildcards; `execution_policy.permission_scope` | `grant_checker.py`, `proxy.py` |
| action | `action_type`, resolved to `LOW`, `HIGH`, or `NEVER` | `gating.py` `blast_radius_for()` |
| conditions | `confidence_threshold`, `auto_execute_after_n_successful_recurrences`, per-boundary checkpoints, `operator_only` | `gating.py` `evaluate_gate()` |
| time | **not implemented.** No `expir`, `valid_until`, `expires_at`, or `ttl` token in `grant_checker.py`, `agent_loader.py`, or `gating.py`. The lease on a claimed task is the only time-bounded authority, and it is not on main either (`work_model.md`) | |

The shape is ABAC (XACML; Cedar). `gating.py` and `grant_checker.py` are the policy decision point; their
call sites are enforcement points. `time` has a minimal form in OpenFGA's `current_time < grant_time +
grant_duration` evaluated at check time: an expiry on `agent_grant` read by the checker, no engine required.

## Where it fails open

Each is an enforcement point mapping an `Indeterminate` decision (unreachable policy source, no policy
found, timed-out load) to `Permit`. Under `failure_posture.md` and principles 5 and 7 each is wrong; the fix
in every case is default-deny with a distinct `unknown`.

1. **ateles#560, open, live on main.** `grant_checker.py:171` `return True  # no grants recorded =
   permissive`; lines 110 to 124 make unreachable Neotoma a permissive fallback; `check_tool()` allows when
   no grant declares any tool. The docstring calls this "advisory in Phase 5". An agent with no grant is
   indistinguishable from one with full permission.
2. **ateles#561, open, live on main.** Delegation does not attenuate: the delegate runs on its own full
   standing grant, not a subset of the dispatcher's; `authorize_caller()` gates whether a caller may
   delegate, never what. `agent_grant` has no `scope`, `expiry`, or `delegated_by`. The chain exists only as
   prose: `a2a_executor.py:190` appends `— delegated via A2A by: <caller>` to the task description.
3. **ateles#669, open; fixed at the loader, not at the caller.** The synthesis reports #669 fixed at
   `496bab3`; on inspection, half true. `agent_loader.py` `_stub()` now marks a failed load `is_stub=True`,
   logs at ERROR, sets status `UNDEFINED_STATUS`, and still returns `tool_allowlist="*"`; its docstring says
   callers must check `is_stub`. No dispatch caller does: `skill_runner.py:164` caches `AgentLoader(role).load()`
   per process with no `is_stub` branch, and nothing under `execution/daemons/apis/` or `lib/daemon_runtime/`
   reads `AgentDefinition.is_stub` (the nine `is_stub` reads on main are all `signer.is_stub`). A timed-out
   load still dispatches an agent with no instructions and wildcard tools, cached for the process lifetime.
4. **`a2a_gateway.authorize_caller()`, no issue.** Returns `(True, "grant_check_unavailable_advisory")` when
   the grant checker raises.
5. **`mcp_tool_grant_proxy/proxy.py:108-110`, no issue; beyond the task's list.** With no `ATELES_AGENT_SUB`,
   every tool call passes through. The proxy is otherwise the nearest thing to a real enforcement point
   (`GrantEnforcer(agent_sub, server_name)` gates every call), which is why its identity-absent branch matters.

## The single principal, measured

Sweep of `lib/`, `execution/daemons/`, `execution/mcp/`, `.claude/hooks/`, and daemon-imported
`execution/scripts/` modules at `496bab3` (synthesis section 4, file and line per site): **138 sites; 60
hardcoded literals or magic values; 63 single-valued parameters (an env or config read once for the whole
swarm); 15 per-principal (resolved from an entity per call). All 15 key on an agent identity. None keys on a
human.** `operator_profile`, which the agent policies say holds operator identity, has one instance and zero
runtime readers under `lib/` and `execution/daemons/`; 26 of 40 prompts mention it, 3 of 40 declare it.

Three chokepoints answer most of the 123 human-assuming sites at once: `notifier.py:116-118` (who to
notify); `email_channel.py:62-64` with `swarm_dispatch.py:4781/4981` (who may approve, by email and GitHub);
`gating.py:573` with `server.py:509/555` (a checkpoint has no owner, so the queue cannot be scoped and
resolution cannot be authorized). ateles#487 and #423 are two of these sites filed as bugs.

## The extension points, and the verdict per point

The README asserts agents carry verified identities, capability is entity-scoped, high-blast actions
checkpoint to a principal, and every action is attributed and replayable, so multi-operator is an extension
of the entity model. Four extension points carry the claim, each judged against the inventory's per-site
classification.

| Extension point | Holds today? | Evidence |
|---|---|---|
| A second `operator_profile` | **No.** Adding the entity changes nothing: zero runtime readers. | inventory |
| Agent-to-principal binding per principal | **No.** `agent_grant` keys on the agent's `sub` with no principal or tenant dimension; the human grants are wildcards; the operator is the string `"operator"`. A second human today is another wildcard grant. | PR-22, C9 |
| Ownership edges | **No.** No principal entity exists to draw an edge to; the principal is a login string, an email, and a chat id. | C9, C10 |
| Delegation edges that attenuate | **No.** #561; no `scope`, `expiry`, `delegated_by`; the chain is prose. | C14 |

**Verdict.** True of the entity model, false of the substrate. The per-agent pattern is a real template:
`AgentLoader(name)`, `GrantChecker(aauth_sub)`, `GrantEnforcer(agent_sub)`, per-agent keypairs with the `sub`
threaded into signed writes, `resolve_policy_for_agent()`, per-agent GitHub logins, and Anthus resolving
`workflow_definition` per project are the 15 sites where identity is a parameter, and a principal dimension
extends each. But 123 of 138 sites resolve "the operator" without asking, concentrated in the notify,
approve, and checkpoint paths P3 is about; each README property exists per agent and stops at the human
boundary (identity at the swarm realm and one bearer; capability at agent grants with no principal;
checkpoints at one unowned queue; attribution at `sender_kind="operator"`). Measured answer: extension of the
model, rewrite of the notify/approve/checkpoint substrate, with three chokepoints that convert most of it at
once. The first P2 change is a principal that is an entity, so an edge has somewhere to point (prior art:
ReBAC as a data model, not Zanzibar as a system).

## Contradictions this document touches

**C9, what identifies the principal: open, deliberately.** Four candidate keys, no mapping:
`operator_profile` (one entity, zero runtime readers); Neotoma's `user_id` (the store's authenticated
principal, which collapses every writer onto one value on a shared instance, so it does not identify the
human there); the AAuth `sub` (agents only; issuer default a personal domain at `skill_runner.py:1283`); and
#378's proposed `operator` entity with `operator_id` and `principal_id`. `multi_tenant.md` keeps
`operator_profile`; #378 introduces `operator`. This document does not pick: picking silently would hand P2
two identity models. The choice is an operator decision for `principals_and_ownership.md` (phase 2.1), stated
with the mapping to `user_id` and the AAuth `sub`. Canonical today is none of the four; the de facto principal
is the login string, the email, and the chat id.

**C8, "every action is attributed to a verified agent identity": unverified.** The AAuth signing audit
(`ent_eaa198482b7bbe4c965eeed0`, 2026-08-06) found daemons loaded signers but attributed writes to the
operator bearer; the fallback strings are still at `aauth_signer.py:163-165` and six daemon sites, and the
ateles MCP server holds one bearer and never identifies its caller (`server.py:64, 99`). Not re-tested on
2026-09-02. Open: the README's sentence is not restated until a write is traced to a per-agent signature.

**C14 and C17.** Resolved above and in `failure_posture.md`.

**C13, per-human routing.** `multi_tenant.md` section 4 routes through `swarm_roster` and `channel_config`;
#378 puts `approving_principal_id` on `checkpoint_brief` and defines `operator` and `team`. Neither is what
the code does, and they name different entities for one routing table. Open for phase 2.1; the three
chokepoints are where it lands.

**C19, #378's gate map says implement while the plan says design.** #378 shows pm, ux, and arch signed off
with a build checklist for seven new and five delta schemas; plan `ent_533d4ec2f7bfb60f66fb3fce` records no
P2 design exists and phase 2.1 is blocked on the operator's `multi_tenant.md` section 7 decisions and the
proving-ground pick. One must yield before a schema PR opens. Open; recorded for #378's owner.

## Prior art

XACML's four decisions name the failure class: every fail-open path treats `Indeterminate` as `Permit`;
Cedar's rule (zero permits is deny; forbid wins) is the fix at the enforcement point. Attenuation by
construction (macaroons: a subset at every hop, restrictions only added) is the invariant #561 lacks,
enforced by reading the chain in the record rather than by cryptography while the record is single and
central. RFC 8693's nested `act` claim is the shape of `authority_chain`. Separation of duties survives at a
dozen principals only if an operator and the agents they built count as one principal (Clark-Wilson), a P3
and P4 rule stated so the P1 seed (`gates_and_workflows.md`) is not built against a different one. Sources: `ent_08460968e6f49dac21510f4a`.

## Beyond the sources

The verdict per extension point, the fifth fail-open path, and the correction on #669 are this document's;
the inventory, counts, and chokepoints are the synthesis's.
