# Multi-Tenancy Design

**Status: Design** (task #6 of plan `ent_aff87747b49e338790568af6` — "Task-spine loop + cloud-hosted swarm")

Realizes Goal 6 of the swarm-architecture plan: *public ateles can be forked by a third party and deployed against their own Neotoma instance.* This document specifies how the Ateles swarm goes from one operator to many operators under one tenant (org/team), and what to build **now** vs **defer**.

The governing decision is `multi_tenant_org_readiness`: design for org/team tenancy now (tenant partition, per-operator + per-agent AAuth scoping, per-human routing, owner/beneficiary refs) even though single-operator launches first — because retrofitting multi-tenancy after data and trust boundaries have set is the expensive path. Build full team **UX** only when a second operator actually exists.

This mirrors the discipline already adopted for durable execution (`docs/durable_execution_substrate.md`): pay the cheap schema/specification hedges now; defer every expensive operational mechanism behind a documented trigger. Premature sharding is as much a failure as a missing seam.

---

## 1. The three deployment shapes

The swarm must support a spectrum, not a binary. Three shapes matter:

| Shape | Humans | Tenants | What it requires |
|---|---|---|---|
| **Single-operator** (launch) | one | one | Current state. One `operator_profile`, one pager (Ateles), one `swarm_roster`. No isolation work needed beyond what already exists. |
| **Fork** (Goal 6) | one (a *different* operator) | one (theirs) | Zero hardcoded operator identity. Everything operator-specific resolved from context entities at runtime: `operator_profile`, `locale_profile`, `swarm_roster`, `channel_config`, `payment_profile`, etc. A forker stands up their own Neotoma, supplies their own context entities, mints their own AAuth keys. **Mostly already true** — agent prompts are operator-agnostic by policy. |
| **Org / team** | many | one (shared) | Multiple humans collaborate under one tenant: shared entity graph, but per-human routing, per-human identity, per-human capability scope, and per-customer ("for whom") visibility. This is the shape that, if not designed for now, forces a painful retrofit. |

**Single-operator** and **fork** are the same code path with different context entities — the fork case is the validation that nothing operator-specific is baked into code (enforced today by `scripts/linters/check_hardcoded_config.py`). The genuinely new axis is **org/team**: more than one human acting inside one tenant.

The critical framing: *fork* multiplies **tenants** (each forker is isolated by running their own Neotoma); *org* multiplies **humans within a tenant** (sharing one Neotoma). A single-Neotoma SaaS deployment serving multiple forkers is the case where both axes are live at once, and the one that makes tenant isolation a hard requirement rather than a deployment convenience.

---

## 2. Data model

### 2.1 The actors

```
tenant ─┬─ operator(s)        (humans who direct the swarm)
        ├─ agents              (the swarm: T1–T4, per-tenant roster)
        └─ beneficiaries /     (people/orgs the swarm acts FOR:
           customers              the operator's customers, contacts, payees)
```

- **tenant** — the isolation boundary. One org, one team, or one solo operator. Today: exactly one, implicit.
- **operator** — a human principal who can direct agents and receive pages. Today: one (`operator_profile`). Org case: many, each with their own channels and capability scope.
- **agent** — a swarm member with an AAuth identity. Today scoped per-role (`<name>@ateles-swarm`). Multi-tenant: scoped per-role **within a tenant**.
- **beneficiary / customer** — the "for whom" of an action. The task schema already carries `beneficiary_entity_id` / `beneficiary_name` / `beneficiary_kind` (added by task `ent_8a832fc695c09479354a1614`, done) precisely so an agent can record *who it executed for* distinct from *who owns/executes the task*. In an org this becomes a visibility boundary: operator A's customers must not surface to operator B.

### 2.2 The `tenant_id` partition

Add a `tenant_id` field to Neotoma entities, exactly as `docs/durable_execution_substrate.md` §"Do now" item 5 prescribes for run/step/wake entities ("*even if always `operator`*"). The principle generalizes from execution entities to **all** domain entities.

- **Type:** opaque string (UUID or slug). Single sentinel value for the existing single-operator deployment.
- **Default for all existing data:** the current single-tenant user is `user_id 00000000-0000-0000-0000-000000000000`. The migration is: **`tenant_id` defaults to a single well-known sentinel derived from that user_id** (e.g. `tenant_00000000`). No data moves; a column/field is populated with one constant. This is a backfill, not a reshape — the same property that made it cheap in the durable-execution design.
- **Relationship to `user_id`:** `user_id` identifies the *human/principal* who authenticated; `tenant_id` identifies the *isolation boundary*. In single-operator they are 1:1 (one user, one tenant). In an org they diverge: many `user_id`s (operators) share one `tenant_id`. Keeping them as **separate fields from day one** is the whole hedge — collapsing them now is what forces the retrofit later. Today's `user_id 0` becomes operator-0 inside `tenant_00000000`.

### 2.3 Where `tenant_id` lives

Every domain entity carries `tenant_id`. The query default is **scope every retrieve to the caller's tenant** — the "absent = denied" instinct from the AAuth grant model (`docs/aauth.md`) applied to rows: a query without a tenant scope is a bug, not a wildcard. Cross-tenant reads require an explicit, audited capability that no normal agent holds.

Configuration entities that are inherently per-tenant — `operator_profile`, `locale_profile`, `swarm_roster`, `channel_config`, `priority_rubric`, `payment_profile`, `agent_definition`, `agent_grant` — are partitioned the same way. A forker's `swarm_roster` is theirs; an org's is shared across its operators.

---

## 3. Identity & isolation

The isolation story is a product of three scoping layers, all of which AAuth already supports in shape (`docs/aauth.md`):

### 3.1 Three scoping dimensions

1. **Per-tenant** — the outermost boundary. Every AAuth-admitted request resolves to a `tenant_id`, and every Neotoma operation is implicitly filtered to it. Cross-tenant access is not a capability any standard grant carries.
2. **Per-operator** — within a tenant, each human is a distinct `(sub, iss)`. The hardware-tier roadmap already plans an operator subject (`mark@markmhendrickson.com`, see `docs/aauth.md` §6); the org case generalizes this to N operator subjects per tenant.
3. **Per-agent** — within a tenant, each agent role is `<name>@<tenant>-swarm` (today `<name>@ateles-swarm`, where `ateles` is effectively the sole tenant slug). The `agent_grant` capability model (operation × entity-type × tool × param) is unchanged; grants simply also match `tenant_id`.

### 3.2 How grants scope to a tenant

`agent_grant` entities today match on `(sub, iss)` and declare `capabilities`. Multi-tenant adds **`match_tenant`** (or derives tenant from `sub` when the subject encodes it, e.g. `monedula@acme-swarm`). Admission then requires: signature valid **and** `(sub, iss)` matches a grant **and** that grant's tenant equals the entity's `tenant_id`. The existing rule "absent = denied" extends cleanly: a write to a tenant the grant does not name fails at admission, before any side effect — the same boundary that already stops Monedula from writing `agent_definition`.

### 3.3 Cross-tenant isolation guarantees

The target guarantees, in order of how cheap they are to establish:

- **No implicit cross-tenant read.** Default query scope is the caller's tenant. (Cheap now: enforce in the Neotoma access layer; the daemon-side test category `tenant-isolation` already exists in `execution/daemons/apis/` review tooling.)
- **No cross-tenant write.** Grant admission checks `tenant_id`. (Cheap now: extend the existing grant match.)
- **No cross-tenant routing.** A page for tenant A never reaches a tenant-B channel (see §4).
- **No cross-tenant key reuse.** Each tenant's agents sign with keys minted under that tenant's namespace; one tenant's compromised key cannot impersonate another tenant's agent. (Cheap now: namespace the `sub`.)

These are *correctness* boundaries and belong in the data layer (Neotoma), not in agent code — exactly as `docs/aauth.md` argues for capability containment: "the boundary lives in Neotoma, not in agent code."

---

## 4. Per-human routing

Today **Ateles is the sole pager to one operator.** All notifications flow through `lib/notify/`, which reads a single `priority_rubric` and delivers via Apprise (Telegram-primary) to one destination. In an org, "page the operator" is ambiguous — *which* human?

### 4.1 The routing model

Routing becomes a function of **(tenant, role-or-person, channel)**, resolved from per-tenant config rather than a single global destination:

- **Roster per tenant.** `swarm_roster` (already a context entity, resolved by role not hardcoded name) gains, per tenant, the set of operator humans and their roles (e.g. owner, finance approver, on-call).
- **Per-human channels.** `channel_config` (already an established context entity) holds each operator's delivery endpoints (Telegram chat id, email, etc.). One operator, one set of channels; an org has many.
- **Routing keyed by who, not just severity.** `priority_rubric` already governs *whether/when* to deliver (silence windows, digest collapse, escalation ladder). Multi-human adds *to whom*: a notification carries a target — a specific person, or a role that resolves to a person via the roster. A finance approval pages the finance approver; a health nudge pages the beneficiary/owner; a generic system alert pages the tenant owner or on-call.

### 4.2 What this preserves

The escalation chain (agent → domain agent → Columba → operator) is unchanged in shape; "operator" simply resolves through the per-tenant roster to the right human instead of a hardcoded single destination. `lib/notify/` keeps reading config from Neotoma — the change is that the destination is *resolved per (tenant, target)* rather than read as one global recipient. No agent prompt changes: agents already say "notify the operator," and the routing layer decides who that is.

**Cross-tenant routing isolation** (§3.3) is enforced here: the notify layer resolves channels only within the notification's `tenant_id`, so a misrouted target can never deliver into another tenant's Telegram/email.

---

## 5. "For whom" / beneficiary model

The beneficiary fields on `task` (`beneficiary_entity_id`, `beneficiary_name`, `beneficiary_kind`) separate **owner** (the operator/agent who executes) from **beneficiary** (who the work is *for*). In single-operator this is mostly informational. In an org it becomes a **second visibility axis** layered on top of tenant isolation:

- **Tenant isolation is the hard wall:** operator A and operator B in *different* tenants share nothing. A customer of tenant A is invisible to tenant B because the customer entity's `tenant_id` differs. This is the §3 guarantee and needs no beneficiary logic.
- **Beneficiary scoping is the soft wall *within* a tenant:** in an org where operators A and B share one tenant, a customer "owned by" operator A should not necessarily be visible to operator B. This is **book-of-business** segmentation, not tenant isolation.

### 5.1 Recommended model

Represent beneficiaries/customers as entities carrying both `tenant_id` (hard wall) and an **owner ref** (the operator whose relationship it is). The beneficiary fields already on `task` link an action to its beneficiary entity; the beneficiary entity links to its owning operator.

- **Now (cheap):** ensure beneficiary/customer entities can carry an owner ref. Most relationship entities can already be linked via the graph (`PART_OF` / ownership relationships), so this may be a relationship pattern rather than a new field. Document the convention: *a customer/contact has exactly one owning operator within a tenant.*
- **Later (org-only):** enforce the soft wall — default a query to "beneficiaries owned by me, plus tenant-shared," and gate cross-operator visibility behind an explicit team-sharing capability. This is UX-shaped (who-sees-whose-book) and should be built only when a second operator exists.

Concretely: **a customer of operator A must not be visible to operator B.** Across tenants this is free (§3). Within a tenant it is the owner-ref soft wall — designed now (the ref exists), enforced when the team UX is built.

This also respects the RGPD legitimate-interest discipline in `CLAUDE.md`: beneficiary data is purpose-bound to the relationship that owns it; an owner ref makes "whose relationship is this, and what is it for" explicit and auditable, and makes an Art. 21 objection actionable against the right book of business.

---

## 6. Phased rollout

The split follows the durable-execution discipline: **cheap design-time hedges now, expensive operational mechanisms deferred behind a trigger.** The trigger here is unambiguous: *a second operator or a multi-forker hosted deployment exists.*

### 6.1 Do NOW (cheap — schema/specification, ~no new infra)

These are the items that are impossible or painful to backfill once data and trust boundaries have set.

1. **`tenant_id` field on all domain + config entities**, defaulting to one sentinel (`tenant_00000000`, derived from `user_id 0`). Backfill is a constant write, not a reshape. *(Highest-leverage hedge — schema migrations across a populated graph are the expensive thing to avoid.)*
2. **Keep `user_id` and `tenant_id` as distinct fields.** Do not collapse them. One sentence in the schema; saves the retrofit.
3. **Default-deny tenant scoping in the Neotoma access layer.** Every retrieve filtered to the caller's tenant; cross-tenant read is an explicit, unheld capability. (The `tenant-isolation` review category in `execution/daemons/apis/` already exists to catch lookups that miss this.)
4. **`match_tenant` on `agent_grant` admission** (or tenant-encoding in `sub`). Extends the existing grant match; no new mechanism.
5. **Namespace AAuth subjects per tenant** (`<name>@<tenant>-swarm`), with `ateles` as the launch tenant slug. Cheap now; renaming live keys later is not.
6. **Owner ref convention on beneficiary/customer entities.** Document "one owning operator per customer within a tenant"; lean on existing graph relationships where possible.
7. **Routing target on notifications + per-human `channel_config` shape.** Make `lib/notify/` resolve destination per (tenant, target) even while there is exactly one target — so adding humans is config, not a code change.
8. **Fork validation as the now-deliverable for Goal 6.** Prove a clean fork stands up against a fresh Neotoma with only context entities supplied and no code edits. This is the single-tenant proof that nothing operator-specific is hardcoded — and it is shippable before any org work.

### 6.2 DEFER until a second operator/org exists (expensive — operational)

- **Team UX**: invite flow, per-operator onboarding, book-of-business assignment screens, shared-vs-private toggles.
- **Soft-wall enforcement** of within-tenant beneficiary visibility (the owner-ref *gate*, as opposed to the owner-ref *field*).
- **Per-tenant quotas, fairness, noisy-neighbor protection** (mirrors the deferred list in `docs/durable_execution_substrate.md`).
- **Cross-tenant admin / multi-forker hosted control plane** (one Neotoma serving many tenants with per-tenant dashboards, bulk operations).
- **Per-tenant key lifecycle tooling** (rotation, revocation at tenant granularity) beyond the per-agent minting that exists today.

### 6.3 The minimal now-work that avoids a painful retrofit

If only the cheapest possible subset is done, it must be: **(1) `tenant_id` partition with `user_id` kept separate, (2) default-deny tenant scoping on reads/writes including grant admission, and (3) per-tenant AAuth subject namespacing.** Everything else — routing resolution, owner refs, team UX — can be layered on without a data migration once these three structural decisions are locked. Skipping any of these three is what turns "add multi-tenancy" from a feature flag into a migration project.

---

## 7. Open decisions (require the operator)

1. **Tenant slug scheme.** Is `tenant_id` a UUID (opaque, stable) or a human slug (`acme`, readable in `sub` like `monedula@acme-swarm`)? Slug reads better in AAuth subjects and logs; UUID avoids rename pain. Recommendation leans slug-with-immutable-UUID-backing, but this is the operator's call.

2. **Does tenant derive from `sub`, or is it a separate `match_tenant` on the grant?** Encoding tenant in the subject (`<name>@<tenant>-swarm`) is self-describing and needs no extra field; a separate field is more flexible if one identity ever spans tenants. Affects every key already minted (`<name>@ateles-swarm`) — decide before more keys are minted.

3. **Within-tenant default visibility.** When the org case arrives, is the default "see only my own book" (private-first) or "see everything in the tenant" (shared-first)? This is a product/RGPD posture decision, not a technical one, and shapes the soft-wall §5.

4. **Single hosted Neotoma vs. per-forker Neotoma for the fork case.** Goal 6 says "their own Neotoma instance" (per-forker isolation = free tenant isolation). If a hosted multi-forker offering ever ships, tenant isolation moves from deployment-level to row-level and §3 becomes load-bearing rather than belt-and-suspenders. Confirm Goal 6 stays per-instance for launch.

5. **Operator vs. agent capability ceiling within a tenant.** Should a non-owner operator be able to mint agent keys, edit `agent_definition`, or change `priority_rubric` for the whole tenant — or are those owner-only? Defines the org admin model; defer the *enforcement* but the *intended* ceiling should be recorded now so grants are shaped consistently.

---

## Related

- `docs/durable_execution_substrate.md` — the "tenant/partition field now, isolation later" precedent this design generalizes.
- `docs/aauth.md` — per-agent identity, grant admission, "the boundary lives in Neotoma, not in agent code."
- `docs/architecture.md` — operator-specific config sourced from context entities at runtime (the fork precondition).
- `CLAUDE.md` standing constraints — agents describe a role generically; specifics come from context entities; RGPD legitimate-interest discipline for people-data.
