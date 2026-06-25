# Neotoma Multi-Tenancy — Audit Completion & Change Plan (Handoff Brief)

**Audience:** an agent with read access to the **`markmhendrickson/neotoma`** repository (and ideally the live Neotoma MCP server).
**Goal:** finish the code-level audit of Neotoma's tenancy posture, then produce a concrete plan for the Neotoma-side changes required so the **Ateles** swarm can serve **multiple operators** via **multi-tenancy**.

This brief is self-contained. It encodes what has already been established (and how reliable each fact is) so you don't redo it, and it tells you exactly what is still missing — which is everything that requires reading Neotoma's source.

---

## 0. Why this exists

Ateles (the agent swarm) currently assumes **one operator across all agents**. Its own README states the dependency plainly:

> "Ateles assumes one operator across all agents — **multi-operator requires multi-tenant Neotoma.**"

Neotoma is the shared memory/entity store every agent reads and writes. So "make Ateles multi-operator" is gated on "make Neotoma multi-tenant." The canonical design already exists on the Ateles side — `docs/multi_tenant.md` (**Status: Design**) — but nothing in it has been built in Neotoma yet (confirmed below). Your job is to ground that design in Neotoma's actual code and turn it into an executable plan.

**Two deployment shapes to keep distinct** (from `multi_tenant.md` §1):
- **Fork** — a *different* operator runs their *own* Neotoma instance. Tenant isolation is free (deployment-level). This needs almost no Neotoma change.
- **Shared / org** — *many* operators on *one* Neotoma. This is the case that needs real row-level multi-tenancy and is the subject of this brief.

The design discipline (`multi_tenant.md` §6, mirroring `docs/durable_execution_substrate.md`): **pay the cheap schema/identity hedges now; defer the expensive operational machinery (team UX, quotas, cross-tenant admin) behind the trigger "a second operator/org actually exists."**

---

## 1. Current state — VERIFIED against the live system

These were confirmed by introspecting **Neotoma prod via MCP** (not inferred from docs). Treat as ground truth as of this brief; re-confirm with the commands in §5 since schemas version.

| Probe | Result | Implication |
|---|---|---|
| `get_session_identity` | `user_id: 00000000-0000-0000-0000-000000000000`; `attribution.tier: "unverified_client"`; `aauth: {verified:false, admitted:false, grant_id:null, admission_reason:"not_signed"}`; `policy.anonymous_writes: "allow"`; `eligible_for_trusted_writes: true` | The swarm authenticates as the **single all-zeros sentinel user**. Identity model is `user_id` + AAuth trust-tier attribution. **No `tenant_id` in the identity at all.** An unsigned `claude-code` client still resolves to user_id 0 and may write. |
| `list_entity_types(keyword:"tenant")` | `{entity_types: [], total: 0}` | **Zero** tenant-related entity types exist in the schema registry. |
| `describe_entity_type("task")` | schema v1.47.0, **79 fields, 0 required**. Contains `owner`, `assigned_to`, `assignee_name`, **`beneficiary_entity_id` / `beneficiary_name` / `beneficiary_kind`**, `repository`/`repo`/`repository_name`. **No `tenant_id` / `org_id` / `workspace_id`.** | Domain entities carry **no tenant partition**. The "for whom" beneficiary hedge (`multi_tenant.md` §5) **is** already in the schema. The 79-optional-field shape strongly implies a **flexible/JSON entity store with a schema-registry overlay** (confirm in §2). |
| `describe_entity_type("agent_grant")` | schema v1.0.0. Fields: `match_sub`, `match_iss`, `match_thumbprint`, `capabilities`(req), `status`(req), `label`(req), `linked_github_login/_user_id/_verified_at`, … `canonical_name` composites of `thumbprint` / `(sub,iss)` / `sub`. **No `match_tenant`.** | Grant admission is keyed on **subject / issuer / key-thumbprint → capabilities**, exactly as `multi_tenant.md` §3.2 says. **No tenant dimension** in authorization. |

### What this means

**Neotoma is single-tenant at the data layer. Multi-tenancy is genuinely unstarted.** What exists are the *seams* the design intends to extend:

- **`user_id` principal** — present, but collapsed to one sentinel today.
- **AAuth trust-tier + capability-grant model** (`agent_grant` matching `(sub, iss, thumbprint)`) — real and load-bearing, but tenant-blind.
- **Beneficiary "for whom" fields on `task`** — the one multi-tenant-relevant piece already shipped (owner-ref soft-wall, `multi_tenant.md` §5).

**Crucial nuance:** Neotoma's security model today is **trust-tier + per-agent capability** ("is this writer trusted? does this subject hold this capability?"), **not tenant isolation** ("which tenant's data may this principal touch?"). Those are orthogonal axes. The grant model is a clean foundation to *extend* with tenancy — it does not provide it.

### Reliability ladder (so you trust the right things)
- **Verified-live (high):** everything in the table above.
- **Design intent (high, but unbuilt):** Ateles `docs/multi_tenant.md` — the target architecture, not current reality.
- **To-confirm (the audit):** every claim about Neotoma's *access layer, write path, identity resolution, SSE, and storage model* below. These need the repo.

---

## 2. PART 1 — Finish the audit (requires the Neotoma repo)

The schema-level "is it multi-tenant" question is answered (**no**). The audit's job is to map **how isolation works today and where every change site is**, so the plan in Part 2 is grounded in real code rather than the design doc.

> **Path hints below are inferred** from Ateles' references and the MCP tool surface — `server/tools/fetch.py` is named by Ateles' own review-panel test fixture (see §4) as the entity-lookup site. **Confirm actual locations in the tree; do not assume.**

### 2.0 Do this FIRST — determine the storage model
Everything downstream depends on it. The 79-optional-field `task` schema + the `register_schema` / `update_schema_incremental` / `schema_version` surface suggests **schema-on-read**: a generic `entities` table (`id, entity_type, user_id, payload JSONB, timestamps, …`) with schemas as validation overlays in a registry — rather than one table per entity type.

- [ ] Confirm: **generic entity table vs. per-type tables?** Find the migrations / ORM models / DDL.
- [ ] Identify the analogous tables for **relationships** and **observations** (and any timeline/event tables).
- [ ] **Decision driver:** if it's a generic table, `tenant_id` is *one column on ~3 tables* + a central filter — "a constant write, not a reshape" (`multi_tenant.md` §2.2). If it's per-type, scope the N-table migration explicitly.

### 2.1 Identity resolution & auth middleware
- [ ] Where is `user_id` derived from a request? Trace what produced `get_session_identity`'s output.
- [ ] How does an **unsigned** client become **user_id 0**? Is user_id client-supplied, header-derived, or token-derived? (Important: `anonymous_writes:allow` + `eligible_for_trusted_writes:true` for an unsigned client is a permissive default — document the exact resolution rule.)
- [ ] Where is **AAuth signature verification & admission** implemented (the thing that sets `aauth.admitted` / `grant_id` / `admission_reason`)? This is where tenant resolution must slot in.
- [ ] Is the codebase already **multi-*user*** (does it support distinct non-zero `user_id`s end to end), or is user_id effectively nominal because only the sentinel is ever used? **This determines how much tenancy can ride on existing user-scoping vs. is net-new.**

### 2.2 Retrieval / read path (the core isolation question)
- [ ] Find the **central query path** for entity reads (`retrieve_entities`, `retrieve_entity_by_identifier`, `retrieve_graph_neighborhood`, `get_relationship_snapshot`, etc.). Candidate: `server/tools/fetch.py`.
- [ ] **Is every read filtered by `user_id` today?** Or do some lookups query globally? (Ateles' review panel flags `server/tools/fetch.py` entity lookup as **not** scoped — verify whether that's current.)
- [ ] Is filtering **centralized** (one query builder / middleware / Postgres RLS) or **per-call** (each handler adds its own `where`, easy to omit)? Centralized is the safe place to add default-deny tenant scoping; per-call means enumerating every site.
- [ ] Check for **Postgres Row-Level Security** policies. If present, tenancy may be a policy change; if absent, it's app-layer.

### 2.3 Write path
- [ ] Where is `user_id` **stamped** on insert/update (`store`, `correct`, `submit_entity`, `create_relationship`, observations)? From the authenticated principal or client-supplied?
- [ ] This is where `tenant_id` must be stamped (derived from the principal's tenant, never client-asserted).

### 2.4 Grant admission (authorization)
- [ ] Find where `agent_grant` is matched against a request (`match_sub` / `match_iss` / `match_thumbprint` → `capabilities`).
- [ ] This is where `match_tenant` is added, **and** where admission must additionally require the grant's tenant == the target entity's `tenant_id` (`multi_tenant.md` §3.2). Confirm the "absent = denied" instinct holds (a write to a tenant the grant doesn't name fails *before* side effects).

### 2.5 SSE / subscription stream
- [ ] Find the **event stream** endpoint that Ateles' `lib/daemon_runtime/sse_client.py` subscribes to.
- [ ] How are events filtered/broadcast — per `user_id`, globally, per channel? Un-filtered, a tenant-B daemon would receive tenant-A events. Tenant filtering must be added here (`multi_tenant.md` §3.3 "no cross-tenant routing").

### 2.6 API surface & aggregates
- [ ] Enumerate REST endpoints (e.g. `GET /stats`, `/mcp`, dashboard/aggregation routes). Any that aggregate **across** users/tenants? Those become cross-tenant leak points and need scoping or an explicit, audited cross-tenant capability.

### 2.7 Keys / secrets / JWKS
- [ ] How are AAuth signing keys / JWKS managed and where is the issuer trust list? Multi-tenant requires **per-tenant key namespacing** so one tenant's key can't impersonate another's agent (`multi_tenant.md` §3.3). Coordinate with Ateles' secrets design (`docs/secrets_management.md`, `ateles-private`).

### Audit deliverable
Produce **`docs/neotoma_multitenancy_audit.md`** (in whichever repo is appropriate) with, per area above: the **actual file:line sites**, a **verdict** (`isolated` / `partially` / `not isolated`), and the **storage model** finding up top. End with a flat list of **every code site that must change** — this list feeds Part 2.

---

## 3. PART 2 — Plan the Neotoma multi-tenancy changes

Map to `multi_tenant.md` §6.1 ("Do NOW — cheap"), but **ground each item in the concrete code sites found in Part 1**. The three irreducible structural decisions (§6.3) that must land before more data/keys set:

> **(1) `tenant_id` partition with `user_id` kept separate, (2) default-deny tenant scoping on reads/writes including grant admission, (3) per-tenant AAuth subject namespacing.** Everything else can layer on without a data migration once these are locked.

### Proposed change set (sequence roughly in this order)

1. **`tenant_id` on all domain + config entities.** Opaque string. Backfill all existing rows to one sentinel — `tenant_00000000`, derived from `user_id 0` (`multi_tenant.md` §2.2). One column on the generic entity/relationship/observation tables if §2.0 confirms schema-on-read. **Keep `user_id` and `tenant_id` as distinct fields** (principal vs. isolation boundary) — do not collapse.
2. **Default-deny tenant scoping in the access layer.** Every retrieve filtered to the caller's tenant; a query without a tenant scope is a bug, not a wildcard. Prefer enforcing at the central query path / RLS found in §2.2, not per-handler.
3. **`match_tenant` on grant admission.** Extend `agent_grant` matching; admission requires signature valid **and** `(sub,iss)` matches **and** grant tenant == entity tenant.
4. **Per-tenant AAuth subject namespacing.** `<name>@<tenant>-swarm` (today `<name>@ateles-swarm`, `ateles` as the launch tenant slug). Decide tenant-in-`sub` vs. separate field **before minting more keys** (§4 decision 2).
5. **SSE/subscription tenant filtering** (§2.5).
6. **Per-tenant key/JWKS isolation** (§2.7).
7. **Owner-ref on beneficiary/customer entities** — largely **already present** (`beneficiary_*` on `task`); document the "one owning operator per customer within a tenant" convention; defer the *enforcement* gate to org-UX time.
8. **Per-(tenant, target) notification routing** — mostly an Ateles `lib/notify/` change, but note any Neotoma-side `priority_rubric` / `channel_config` shape needed.
9. **Audit/observation partitioning** — observations queryable per tenant only.

### Also produce
- **Migration plan** — exact DDL/migrations, backfill strategy, rollout order, and reversibility. Confirm the "constant write, not a reshape" claim holds for the real storage model.
- **Test strategy** — Ateles already has a `tenant-isolation` **review category** (see §4) but **no runtime enforcement**. Add: cross-tenant read-leak tests, cross-tenant write-denial tests, SSE cross-tenant non-delivery tests. Wire `tenant-isolation` from a review heuristic into actual CI assertions.
- **Effort/risk per item** and a **critical-path** ordering (which items block others; §6.3 trio first).

### Defer (don't plan in detail — note the trigger)
Team UX (invites, onboarding, book-of-business screens), within-tenant beneficiary visibility *enforcement*, per-tenant quotas/noisy-neighbor, cross-tenant admin/control plane, per-tenant key rotation tooling. Trigger: "a second operator or multi-forker hosted deployment exists" (`multi_tenant.md` §6.2).

---

## 4. Open decisions — REQUIRE THE OPERATOR (surface, don't guess)

From `multi_tenant.md` §7. Flag these explicitly in your plan with a recommendation each; do not silently pick:

1. **Tenant slug scheme** — opaque UUID vs. human slug (`acme`, readable in `monedula@acme-swarm`). Recommendation leans slug-with-immutable-UUID-backing. Operator's call.
2. **Tenant from `sub` vs. separate `match_tenant`** — affects every key already minted (`<name>@ateles-swarm`). **Decide before minting more keys.**
3. **Within-tenant default visibility** — private-first ("my book only") vs. shared-first. Product/RGPD posture, shapes the §5 soft wall.
4. **Single hosted Neotoma vs. per-forker Neotoma** — confirms whether row-level isolation is load-bearing (shared) or belt-and-suspenders (per-instance fork). Goal 6 currently says per-instance.
5. **Operator vs. agent capability ceiling within a tenant** — can a non-owner operator mint agent keys / edit `agent_definition` / change `priority_rubric`? Record the *intended* ceiling now even if enforcement is deferred.

---

## 5. Reference & re-verification

### Re-run the live schema check (Neotoma MCP)
```
get_session_identity()                      # identity model; look for tenant_id (expect absent)
list_entity_types(keyword:"tenant")         # expect total:0
list_entity_types()                          # full entity-type surface
get_entity_type_counts()                     # row counts per type → migration scope/volume
describe_entity_type("task")                 # confirm no tenant_id; beneficiary_* present
describe_entity_type("agent_grant")          # confirm no match_tenant
describe_entity_type("<config types>")       # operator_profile, swarm_roster, channel_config, priority_rubric, agent_definition
retrieve_entities(entity_type:"…", limit:N) # sample real rows to see stamped fields
```
> Note: the MCP session can return `Service Unavailable: MCP session is unknown` after a server restart (stale session). Fix by reconnecting/re-initializing the MCP client; retrying the same handle won't clear it.

### Ateles repo files to read (the design + the seams)
- `docs/multi_tenant.md` — **the canonical design. Read in full.** (§1 shapes, §2 data model/`tenant_id`, §3 identity/grants, §4 routing, §5 beneficiary, §6 phased rollout, §7 open decisions.)
- `docs/aauth.md` — per-agent identity, grant admission, "the boundary lives in Neotoma, not in agent code."
- `docs/durable_execution_substrate.md` — the "partition field now, isolation later" precedent this generalizes.
- `docs/architecture.md` — operator-specific config sourced from context entities at runtime (the fork precondition).
- `README.md` — the "multi-operator requires multi-tenant Neotoma" statement; `ateles#18` ("Make Ateles installable") lists "multi-operator path" among its blockers.
- `execution/daemons/apis/review_panel.py`, `review_learning.py`, `test_review_learning.py` — the **`tenant-isolation` review category**. Its test fixture flags `server/tools/fetch.py` as "entity lookup … not [tenant-scoped]" — your strongest pointer to the read-path gap. **Extend this from a review heuristic into enforced tests.**
- `lib/daemon_runtime/sse_client.py` — the SSE consumer; tells you what the server stream must filter.
- `lib/notify/` — per-human routing target (§3 item 8).

### Plan linkage (Neotoma side of the work)
Per `multi_tenant.md`, this is **task #6 of plan `ent_aff87747b49e338790568af6`** ("Task-spine loop + cloud-hosted swarm"), not the swarm-architecture plan. Link new tasks `PART_OF` that plan.

---

## 6. Acceptance criteria for this handoff

Done when you have produced:
1. **`docs/neotoma_multitenancy_audit.md`** — storage model determined; every read/write/identity/grant/SSE/API site mapped with `file:line` and an isolation verdict; a flat list of change sites.
2. **A phased change plan** — the §3 change set grounded in real code sites, with migration plan, test strategy, effort/risk, and critical-path ordering (the §6.3 trio first).
3. **A decision memo** — the §4 open decisions, each with a recommendation, for operator sign-off.

**Guardrails:** Neotoma is a public product repo — no operator PII in any artifact (`user_id 0` and `neotoma.markmhendrickson.com` are already-public, fine). Don't collapse `user_id` and `tenant_id`. Don't plan the deferred (§6.2) items in detail. Where the audit contradicts this brief's *design-intent* claims, trust the code and say so.

---

*Provenance: current-state facts in §1 verified live against Neotoma prod via MCP; design in §3–§4 from Ateles `docs/multi_tenant.md` (Status: Design). The code-level audit (§2) is unstarted and is your task.*
