# Installability & multi-tenancy

The concrete changes required to take Ateles from **adopt-by-fork reference infrastructure**
to **installable** (a new operator can stand up their own swarm) and **multi-tenant** (many
operators share one installation). These are two different problems: installable is mostly an
ateles + packaging problem; multi-tenant is mostly a Neotoma tenancy problem. Installable is a
prerequisite for multi-tenant.

Tracked upstream by the umbrella issues [ateles#18 (make installable)](https://github.com/markmhendrickson/ateles/issues/18)
and [ateles#19 (input attribution)](https://github.com/markmhendrickson/ateles/issues/19); see
also [multi_tenant.md](multi_tenant.md).

---

## Why it isn't installable today

Behaviour is coupled to operator-specific Neotoma entities (operator_profile, swarm_roster,
channels, locale, payment profiles), keypairs are minted by hand into a sibling `ateles-private`
repo, config is ~40 raw env vars with no schema, and daemons are registered implicitly via
hand-written launchd plists. A new operator has none of these and no flow to create them.

### Key changes to make it installable

1. **Packaging & entrypoint.** No `pyproject.toml`, no `ateles` CLI; dependencies are split
   across three `requirements.txt` + a Node `package.json` + external CLIs. → A package with a
   console entrypoint (`ateles init / doctor / provision / run / deploy`) and one pinned/locked
   dependency manifest.
2. **Config schema + preflight.** → A typed schema for every env var (see
   [configuration.md](configuration.md)), plus `ateles doctor`
   ([execution/scripts/ateles_doctor.py](../execution/scripts/ateles_doctor.py), already present)
   and an `ateles init` wizard that writes `.env`.
3. **Provisioning flow (the biggest missing piece).** `ateles provision`: register the required
   Neotoma schemas, seed the operator's context entities (operator_profile, locale_profile,
   channel_config, swarm_roster) from wizard answers, mint keypairs, create agent_grants.
4. **Identity unification.** Keypairs are minted ad hoc in mixed JWK/PEM formats. → One canonical
   keypair format, documented JWKS publication, revocation primitives.
5. **Secrets abstraction.** Code assumes a sibling `ateles-private` repo + SOPS+age. → A pluggable
   secret backend (env / SOPS+age / 1Password) so that layout is one option, not a requirement.
6. **Scheduler generation + a daemon registry.** launchd plists are hand-written and there is no
   explicit daemon manifest. → Introduce a daemon registry, then generate launchd / systemd /
   compose units from it.
7. **Decouple operator-specific defaults.** Some entity IDs (plan, company) and channel IDs are
   baked as env defaults. → Provision them; extend `check_hardcoded_config.py` to flag defaulted
   operator entity IDs.
8. **Versioning.** Schemas, agent_definitions, and workflow_definitions aren't version-pinned. →
   Version the schema slate + a migration path so a correction can't silently invalidate a fork.

---

## Why it isn't multi-operator today

Exactly one operator identity is threaded everywhere, and Neotoma is single-tenant in use. The
encouraging part: the architecture already resolves operator-specifics from context entities
(identity from operator_profile, channels from channel_config, roster from swarm_roster) — which
is precisely the seam tenancy needs. So multi-operator is "thread a tenant through the runtime +
make Neotoma multi-tenant + move the last env-baked values into entities."

### Key changes to make it multi-operator

1. **Tenant model in Neotoma (foundational, mostly not in this repo).** Every entity scoped by
   `tenant_id`; retrieval and SSE filtered by tenant. Nothing else matters until this exists.
2. **Identity namespacing.** `agent_sub` is `<name>@ateles-swarm`; needs `<name>@<tenant>`, JWKS
   per tenant, grants scoped per tenant.
3. **Tenant context through the runtime.** `lib/daemon_runtime` (AgentLoader, SSEClient
   subscription filters, Notifier routing) must carry a tenant and resolve *that* operator's
   entities per dispatch.
4. **Channel isolation.** Telegram chat/topic, the swarm Gmail inbox, and calendars are read from
   single env values. → Resolve all channels from per-tenant `channel_config` entities, not env.
5. **Secrets per tenant.** PATs, AAuth keys, Wise tokens isolated and keyed by tenant.
6. **Dispatch isolation.** `claude --print` subprocesses must load the right tenant's keypair +
   context, in process/worktree isolation so one tenant can't read another's state.
7. **Authorization boundary.** The MCP grant proxy must enforce the tenant boundary (operator X's
   agent can't act on operator Y's repos), not just per-tool scope.
8. **Audit partitioning + data protection.** Observations queryable per tenant only; per-tenant
   data-isolation boundaries.

---

## Sequencing

Installable first — it forces the provisioning flow, config schema, identity unification, and
secret abstraction that multi-operator also needs. Then multi-operator's critical path is:

1. Neotoma tenancy →
2. identity namespacing →
3. tenant-threaded runtime →
4. channel- and secret-per-tenant.

Items 6–8 (dispatch isolation, authorization boundary, audit partitioning) are isolation hardening
on top. The work that lives in *this* repo is small relative to the Neotoma-side tenancy change,
which is the real gate.

> This is a design/roadmap document describing capability the repo does not yet ship. It is not a
> description of the system as installed today.
