# Neotoma vs. alternatives: choosing a substrate for an agent fleet

## Purpose

Help a reader decide whether to build a multi-agent fleet on a Neotoma-canonical substrate — the approach Ateles takes — or to assemble alternatives. This is an evaluation guide, not a sales pitch: it names the guarantees a fleet needs, shows honestly how the 2026 ecosystem provides each one, and gives a decision framework for when the unified approach is worth its cost and when a bolt-on stack is the better call.

## Scope

Covers the cross-cutting guarantees any always-on multi-agent fleet eventually needs — shared coordination state, provenance/audit, per-agent identity and authorization, event-driven coordination, externalized prompt/config, and idempotency — and how mainstream frameworks, memory layers, durable-execution engines, and identity systems satisfy each. Does not cover Ateles' agent inventory (see `taxonomy.md`), the orchestration model (`swarm_orchestration.md`), or Neotoma's API surface. Complements the "Build vs. adopt decisions" and "Why Neotoma instead of {LangSmith, PromptLayer, custom DB}?" sections in `architecture.md`.

---

## The decision in one paragraph

A fleet of agents that act on shared work, over a long time, eventually needs answers to: *What is true right now? Who changed it, when, and why? What is this agent allowed to do? How do agents react to each other's work?* Ateles answers all of these from **one canonical, provenance-tracked entity store (Neotoma)**. The mainstream alternative is to answer each question with a **separate best-of-breed tool** — a memory layer, a tracing platform, a durable-execution engine, a secrets/identity system, a prompt CMS, an event bus — and stitch them together. The unified approach trades up-front data-model discipline for a single queryable history; the assembled approach trades faster starts and best-of-breed components for cross-system fragmentation. Neither is universally correct. The rest of this doc is about telling them apart for *your* situation.

---

## The guarantees a fleet needs

These are the evaluation axes. Each is a real need that shows up once a fleet outgrows a single prototype agent.

1. **Shared durable coordination state** — plans, tasks, decisions, and "who is working on what" that any agent can read and write, surviving restarts.
2. **Provenance / audit** — the ability to answer "which agent wrote this field, when, from what input, and why" — months later, not just in a live trace.
3. **Identity + authorization** — a real identity per agent, and enforcement of what each agent may do, ideally not dependent on the agent's own code behaving.
4. **Event-driven coordination** — agents reacting to changes in shared state rather than polling or hard-coded handoffs.
5. **Externalized, versioned config** — prompts and tool-allowlists that change without a redeploy, with history.
6. **Idempotency / consistency** — safe concurrent and retried writes without duplication or drift.

---

## How alternatives provide each guarantee

| Guarantee | Common alternatives (2026) | What you actually get | Gap vs. a canonical store |
|---|---|---|---|
| **Shared coordination state** | LangGraph checkpointers + `Store`; CrewAI memory; OpenAI Agents SDK Sessions/handoffs; Microsoft Agent Framework session/workflow checkpoints; DIY Redis/Postgres/vector "blackboard" | Durable-or-ephemeral **scratchpads** and message-passing. Persistent with a Redis/Postgres backend, but schemaless and usually per-thread. | None natively model plans/tasks/participation as **typed, queryable entities**. Sharing across agents means agreeing on namespaces in a schemaless KV. |
| **Agent memory (facts)** | Letta (shared memory blocks); **Zep + Graphiti** (bitemporal knowledge graph); mem0; Cognee | Real shared memory. **Zep/Graphiti** is the strongest substitute — facts carry `valid_from`/`valid_to`, so it versions and tracks contradictions. | Stores *extracted facts/memories*, not coordination state (plans, workflow, participation). It's a truth layer for "what is known," not "what the fleet is doing." |
| **Provenance / audit** | OpenTelemetry GenAI semconv → LangSmith, Langfuse, Arize Phoenix, AgentOps, Braintrust | Rich **short-term** traces: reasoning, tool calls, I/O, cost. | Spans are **ephemeral, sampled, TTL'd** (retention often days to months; some platforms drop payloads). Attributed to a span, not a versioned record; not per-field; weakly queryable long-term. |
| **Durable audit log + idempotency** | Temporal, Restate, DBOS (durable execution) | The strongest **exactly-once + full-history** story available off the shelf; replayable event history per run. | Organized **per workflow run, not per data field**. Answers "what did this execution do," not "every write to entity X over time." Idempotent external writes still need manually threaded dedup keys. |
| **Identity + authorization** | SPIFFE/SPIRE (workload identity); AWS Bedrock AgentCore Identity (scoped OAuth, on-behalf-of token exchange); framework tool-allowlists | Transport identity (SPIFFE) or scoped OAuth tokens (Bedrock). | SPIFFE authenticates *who the workload is*, not what data it may touch. Bedrock enforces OAuth scope at each resource. Framework allow-lists are **advisory in-code** — the consensus is to enforce at a sandbox/data layer, "not by expecting the model to self-police." Industry surveys report the large majority of non-human identities are over-privileged. |
| **Coordination protocols** | MCP (agent↔tool); A2A (agent↔agent); Kafka/Pulsar + durable-execution signals | Standardized transport and discovery; durable messaging. | MCP/A2A standardize the wire, **not coordination or authorization** (A2A delegates authz entirely; auth is recommended, not enforced). Event buses guarantee message delivery, not business completion. |
| **Config / prompt versioning** | LangSmith Prompt Hub, Langfuse, PromptLayer, git | Externalized, commit-hashed prompts that deploy without a code release. | This **validates** the versioned-config approach — but these are **separate prompt CMSes**, disjoint from the identity, authz, and memory systems. |

---

## The honest tradeoff

The unified, canonical approach is not free. Its costs are real:

- **Data-model discipline.** Every meaningful thing becomes a typed entity with a schema. That's friction up front and an ongoing tax on "just store this somewhere."
- **Write friction.** Agents write through an API with attribution and validation, not to a scratch dict. This is slower to author against than a Redis key.
- **You run the store.** A canonical store is infrastructure you operate, back up, and keep available. A bolt-on stack lets you offload pieces to managed SaaS.

The assembled approach has the inverse profile: fast to start, best-of-breed per component, managed options available — but the well-documented failure mode is **fragmentation**. Lineage spans multiple systems with different retention and no shared key, so "why did this happen" becomes an archaeology project, and inconsistent state across the memory/trace/execution stores produces real production incidents.

---

## A decision framework

**Lean toward a canonical, Neotoma-style substrate when:**

- The fleet is **long-lived** and you'll need to answer "why did this happen" weeks or months later — provenance must outlive a trace's TTL.
- You operate on **sensitive or personal data** and need per-agent capabilities **enforced**, not advisory.
- You want agent **config to change without redeploys**, with full history and attribution.
- A **small team** (even one operator) can't realistically run and reconcile six separate systems.
- You value a **single queryable graph** spanning prompts, actions, data, events, and identity over best-of-breed isolation.

**Lean toward an assembled / bolt-on stack when:**

- You're **prototyping** or building a single-purpose agent where ephemeral traces are fine.
- You have the **team to integrate and operate** best-of-breed components and want their specific strengths.
- You're already on a **managed platform** (e.g. AWS Bedrock AgentCore bundles Identity + Memory + Gateway + Observability) and want to stay in it.
- Your provenance needs are **short-lived** — debugging the last few runs, not auditing history.
- You want to **avoid the write-friction and schema discipline** a canonical store imposes.

**A common hybrid:** Zep/Graphiti for fact memory + Temporal for durable execution + a tracing platform for live debugging. This is a reasonable middle path — just budget for the integration work and accept that cross-system lineage will be partial.

---

## Where the field is converging

Three signals suggest the canonical-graph idea is not idiosyncratic to Ateles:

- **Bitemporal knowledge-graph memory** (Zep/Graphiti) has become a recognized pattern — facts with validity intervals and contradiction tracking, i.e. a versioned truth layer for what's known.
- **Durable execution** (Temporal, Restate, DBOS) has normalized the idea that an agent's history should be a replayable, durable log rather than ephemeral telemetry.
- **Academic provenance research** is independently building Neotoma's thesis. **PROV-AGENT** (Souza et al., IEEE e-Science 2025) extends the W3C PROV standard plus MCP into a single unified graph of agents, tools, prompts, model calls, and data — explicitly arguing that telemetry "expires or is sampled" and is unfit for governance. A 2025 follow-up (*LLM Agents for Interactive Workflow Provenance*, SC'25 Workshops, with open-source code on the Flowcept project) adds agents that query that provenance graph in natural language. These remain HPC/scientific-workflow research systems — provenance **capture and query**, not a config/identity/capability plane — but the direction of travel is toward unified provenance graphs and enforced agent identity.

The takeaway is not "Neotoma wins." It's that the guarantees Neotoma bundles are real, increasingly recognized needs, and that the choice is between **buying them as one unified graph** (with discipline cost) or **assembling them from parts** (with fragmentation cost).

---

## Further reading

- LangGraph persistence: <https://docs.langchain.com/oss/python/langgraph/persistence>
- CrewAI memory: <https://docs.crewai.com/en/concepts/memory>
- OpenAI Agents SDK: <https://openai.github.io/openai-agents-python/>
- Letta shared memory: <https://docs.letta.com/guides/agents/multi-agent-shared-memory>
- Zep / Graphiti (temporal knowledge graph): <https://vectorize.io/articles/zep-vs-cognee>
- OpenTelemetry GenAI semantic conventions: <https://opentelemetry.io/docs/specs/semconv/gen-ai/>
- Temporal event history: <https://docs.temporal.io/workflow-execution/event>
- SPIFFE for agentic AI: <https://www.hashicorp.com/en/blog/spiffe-securing-the-identity-of-agentic-ai-and-non-human-actors>
- AWS Bedrock AgentCore (on-behalf-of token exchange): <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/on-behalf-of-token-exchange.html>
- MCP / A2A protocol convergence: <https://zylos.ai/research/2026-03-26-agent-interoperability-protocols-mcp-a2a-acp-convergence>
- PROV-AGENT (unified provenance, W3C PROV + MCP): <https://arxiv.org/abs/2508.02866>
- LLM Agents for Interactive Workflow Provenance (follow-up + code): <https://arxiv.org/abs/2509.13978> · <https://github.com/flowcept/FlowceptAgent-WORKS25>
