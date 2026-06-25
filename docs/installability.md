# Making Ateles installable — reconciled plan

> Status: planning. Supersedes the flat ten-blocker list in
> [ateles#18](https://github.com/markmhendrickson/ateles/issues/18) by reconciling it with a
> "coupling → change" architectural reading and a completeness pass. Scope is **install-by-package**
> (a new operator runs `ateles …` against their own Neotoma + AAuth identities), not the
> adopt-by-forking path that works today.

## How this was reconciled

Three inputs were merged:

1. **Issue #18** — the canonical umbrella, ten blockers, priority-ordered but flat.
2. **A "coupling → change" reading** — for each thing the code couples to today, the change that
   removes the coupling. This surfaced root causes the checklist missed (a daemon *registry* behind
   plist generation; a *pluggable* secret backend behind `ateles-private`; *entity provisioning* as
   the real critical path).
3. **A completeness pass** — three items the coupling reading dropped were recovered: the SKILL.md
   mirror-pipeline external dependency (#18 #5), the *release-signal* half of versioning (#18 #9),
   and the stale `docs/setup.md`.

**Organizing principle:** every workstream is stated as *coupling today (verified) → change required
→ which CLI verb surfaces it → #18 blocker(s) it subsumes → current state.* The spine is a single
`ateles` package/CLI; the **keystone is `ateles provision`** — without seeded Neotoma entities a
fresh operator's swarm is dead on arrival, because the architecture resolves all
operator/locale/vendor/swarm specifics from runtime context entities by design.

## The CLI surface (the spine)

| Verb | Responsibility |
|---|---|
| `ateles init` | Interactive wizard: collect operator domain, channels, locale; write a validated `ateles.config` + `.env`. |
| `ateles doctor` | Preflight / "rung detector": Neotoma reachable, keys present + valid, external CLIs installed, required schemas registered, context entities seeded. Reports the next missing rung. |
| `ateles provision` | **Keystone.** Register required schemas; seed operator context entities; mint keypairs + publish JWKS; create `agent_grant`s; validate round-trip. |
| `ateles run` | Run a daemon (or all) in the foreground from the daemon registry. |
| `ateles deploy` | Render + install scheduler units (launchd / systemd / compose) from the daemon registry. |
| `ateles mirror` | Regenerate SKILL.md from Neotoma on demand (pull-mode alternative to the Apus tunnel/webhook). |

## Reconciled workstreams

| # | Workstream | Coupling today (verified) | Change required | Verb | #18 | State |
|---|---|---|---|---|---|---|
| **W0** | **Package + CLI + manifest** | No `pyproject.toml`/`setup.py`/`Brewfile`; deps split across **12** `requirements.txt` + a Node `package.json` + external CLIs. | One installable package with a console entrypoint and one pinned/locked manifest (+ Brewfile/install script for `op`/`gh`/`gws`/`claude`/Node/Python). | all | #7 | none |
| **W1** | **Config schema + `doctor`** | ~42 ad-hoc env vars; only `.env.example`; no validation. | Typed config schema; `ateles doctor` preflight; `ateles init` wizard writes config. | init, doctor | #3 | none |
| **W2** | **Provisioning (entity seeding)** | Fresh Neotoma is empty → nothing resolves. Keypair minting is loose scripts (`mint_daemon_keypair.py`). | Register schemas; seed `operator_profile`, `locale_profile`, `channel_config`, `swarm_roster` + the plan entity from wizard answers; mint keys; create `agent_grant`s; validate. | provision | #1 (+#2) | none (scripts only) |
| **W3** | **Identity / keypair unification** | JWK (`.creds/*.private.jwk`) vs PEM/JSON (`ateles-private/keys/`) coexist in `lib/daemon_runtime/aauth_signer.py`. | One canonical keypair format; documented JWKS publication; revocation primitives (partly done in #41). Feeds W2's mint step. | provision | #4 | mixed |
| **W4** | **Pluggable secret backend** | Code hard-assumes a sibling `ateles-private` repo + SOPS+age. | Secret resolution behind an interface: `{env, SOPS+age, 1Password}`; sibling-repo is one backend. Scaffold generator for `ateles-private` when chosen. | init, provision | #8 | partial (SOPS+age done; `ATELES_SECRETS_DIR` default) |
| **W5** | **Decouple operator-specific defaults** | Operator entity-IDs baked as env defaults: `cotinga.py:113` (`ATELES_PLAN_ENTITY_ID`), `lib/daemon_runtime/session_finalize.py:38` (`END_SKILL_ID`), `render_plan_docs.py:38` (literal), `aquila.py:197` (in a prompt). Only **2** `config-source-ok` suppressions repo-wide → linter blind to these. AAuth issuer fields hardcode the operator domain. | Resolve these from provisioned entities/config; parameterize issuer over `ATELES_OPERATOR_DOMAIN`; **extend `check_hardcoded_config.py`** to flag defaulted operator entity-IDs. | provision | #2 | baked |
| **W6** | **Daemon registry → scheduler generation** | No registry/manifest — daemons enumerated by directory convention (18 dirs under `execution/daemons/`). Per-daemon plists hand-written; only `rc-autodeploy` has a `.plist.template`. | Introduce an explicit daemon registry; render launchd plists / systemd units / compose services from it (unifies with `deploy/cloud`). | run, deploy | #6 | none |
| **W7** | **SKILL.md mirror replication** | Apus mirror needs a Cloudflare tunnel (`apus.markmhendrickson.com`) + a Neotoma webhook + a running daemon; no scripted replication path. | `ateles mirror` pull-mode (no tunnel/webhook) and/or a documented Apus setup. Depends on ateles#17. | mirror | #5 | bespoke infra only |
| **W8** | **Versioning (both halves)** | 0 git tags, no `CHANGELOG`, no semver. Schemas/`agent_definition`/`workflow_definition` not version-pinned → a `correct()` can silently invalidate a fork. | (a) Release signal: tags + semver + CHANGELOG. (b) Schema-slate versioning + migration path. | — | #9 | none |
| **W9** | **Docs: setup rewrite** | `docs/setup.md` is stale (documents a defunct `mcp/parquet` + `.cursor/mcp.json` + `personal`-dir + Py3.10 + `venv/` layout). | Replace with an install/init/provision walkthrough generated against the real structure. | — | (adjacent) | stale |
| **W10** | **Multi-operator path** | Neotoma single-tenant. | Multi-tenant Neotoma (neotoma repo) **or** documented per-operator self-hosting isolation. | — | #10 | **out of scope** for this milestone |

## Sequencing

```
Phase 0  W0  Package + CLI skeleton + manifest        ── everything hangs off it
Phase 1  W1  Config schema + doctor      ┐
         W3  Keypair unification         ├─ parallel; all three unblock provision
         W4  Secret backend interface    ┘
Phase 2  W2  PROVISION  (keystone)       ── depends on W1 + W3 + W4
Phase 3  W5  Decouple operator defaults  ┐
         W6  Daemon registry + scheduler ├─ depend on W2 (entities exist) / W0
         W7  Mirror replication          ┘
Phase 4  W8  Versioning                  ┐
         W9  Setup-doc rewrite           ┘─ release readiness
─────────
Separate W10 Multi-operator              ── out of scope for install-by-package
```

## What changed vs. the original #18

- **Deepened (root cause found):** #1 → provisioning as *entity seeding* (W2, keystone); #6 → a *daemon registry* behind plist generation (W6); #8 → a *pluggable* secret backend, not just a scaffold (W4); #2 → concrete *baked entity-ID env defaults* + extend the existing linter (W5); #9 → kept *both* halves, release signal *and* schema-pinning (W8).
- **Recovered (were being dropped):** #5 SKILL.md mirror pipeline (W7); the release-signal half of #9 (W8); stale `docs/setup.md` (W9).
- **Added as the spine:** the `ateles` package + CLI verb surface, which #18 implied but never named.
- **Unchanged / out of scope:** #10 multi-operator (W10) — matches #18's own out-of-scope note.

## Out of scope

Multi-tenant Neotoma; a hosted/SaaS version; changing the reference-architecture design itself.
