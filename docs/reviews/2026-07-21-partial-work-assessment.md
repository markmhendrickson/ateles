# Partially Implemented Work — Holistic Assessment (2026-07-21)

Session-produced review of the ateles repo + swarm: what functionality is partially
implemented, what remains to finish it, and how well that remaining work is already
captured by issues, PRs, and the plan (via its `docs/phases.md` / `docs/taxonomy.md`
mirrors) versus needing new capture as tasks/issues with plan linkage.

Method: three parallel deep dives — (1) code-level completeness sweep across daemons,
libs, scripts, MCP servers, hooks, and tests; (2) full review of open GitHub issues
(47), open PRs (26), and remote branches (80); (3) design-doc-vs-implementation audit
across 23 docs. Cross-referenced against the plan mirrors and CLAUDE.md.

> Note: this session ran remotely without Neotoma MCP access, so plan/task entities
> could not be read or written directly. Plan-facing corrections below are stated as
> ready-to-apply actions for a Neotoma-connected session.

---

## 1. Executive summary

1. **The single biggest blocker to finishing anything is the swarm's own merge
   pipeline.** 26 open PRs (median age 12+ days) are queued behind gate bugs the
   repo has already diagnosed: #211 (arch pre-impl gate blocks every non-security PR),
   #187 (gate-waive persistence), #240 (gate state split across duplicate issue
   entities), #239 (stale findings block current head), #235 (CONFLICTING PRs stall
   silently). Several fixes exist *as open PRs* (#187, #233, #244, #245) — the queue
   is deadlocked on itself. Unblocking this multiplies everything else.
2. **Most in-flight bug work is well captured** — the review/gating pipeline and
   daemon-notification themes have issue↔PR pairs. The capture problem there is
   *hygiene*: two duplicate PR pairs (#223 vs #227, #226 vs #228), an overlap pair
   (#218 vs #219, #210 vs #228), and two stale PRs (#163, #166).
3. **Several significant code gaps are captured nowhere** (no issue, no plan
   checkbox): Tyto's OCR leg (screenshots accumulate as `pending_ocr` with no
   consumer), Turdus's real LLM triage classifier, the disabled parquet MCP audit
   log + embeddings, the flagged-off A2A gateway (and its unmerged
   `feat/apis-a2a-gateway` branch + missing plist), the dormant session-integrity
   BLOCK rollout, and monedula's untested money-movement handlers. §4 proposes
   issues for each.
4. **The plan mirrors and CLAUDE.md are materially stale.** Taxonomy misdescribes
   Cyphorhinus (now a deprecated reply-router, not audio import), omits Piculet and
   Riparia entirely, lists Aquila/Gorilla/Morning-brief wrongly, and marks as
   "planned" many agents that ship as skills. CLAUDE.md claims "30 unique open
   issues (#368–#416)" — the ateles tracker actually holds 47 open issues numbered
   #1–#246. Phase 5–6 checklists are accurate but incomplete relative to code.
5. **Four docs describe systems that don't exist in the repo** (phantom or dead
   references): `agent_execution_architecture.md` (entire OpenClaw stack absent),
   `credential_management.md` (documents a `scripts/credentials.py` API that isn't
   there), `aauth.md` (five referenced scripts + JWKS tree missing),
   `mcp_resource_implementation_checklist.md` (marked COMPLETE; no server implements
   resources). `documentation_plan.md` wrongly certifies the first two as current.
6. **53 of 80 remote branches have no open PR** and the repo squash-merges, so each
   needs case-by-case triage; the named `feat/*` ones (apis-a2a-gateway,
   release-publish-daemon, aauth-per-agent-signed-requests, aauth-keypair-revocation,
   confidence-blast-radius-gating, 26-mcp-tool-grants, skill-sync-mirror,
   secrets/relocate-to-private) most likely hold real unfinished work.

---

## 2. State of partially implemented functionality (ranked)

### A. Swarm orchestration core
| Item | State | Evidence |
|---|---|---|
| Anthus swarm coordinator | **Skeleton** — daemon logs events only; "full swarm-coordinator logic deferred to Phase 6" | `execution/daemons/anthus/anthus.py:80-82` |
| Orchestrator canonical-header gating | Falls back to soft author-name heuristic; `require_canonical_header` "not yet schema-supported (Phase 6)" | `anthus/orchestrator.py:170,231-232` |
| Smoke test tiers 2–5 | Not passed; Tier-2 dispatch blocked by Anthus hydrate bug (fix in PR #172, depends on neotoma#1749) | `docs/swarm_smoke_test_plan.md`, PR #172 |
| Apis dispatcher | Mature + well-tested; `/reject` training signal not wired into review_learning | `apis/swarm_dispatch.py:2724` |
| A2A gateway | Implemented + tested but **defaults off** (`APIS_A2A_ENABLE=0`); `com.ateles.apis-a2a.plist` not in repo; work sits on unmerged `feat/apis-a2a-gateway` | `apis/a2a_gateway.py:50` |
| HITL checkpoints | H1 only (`/approve` `/reject` `/hold`, pre_merge). H2–H5 (pre_impl/pre_release boundaries, condition DSL, GitHub assignment) design-only | `docs/swarm_hitl_checkpoints_design.md` |
| Contract-based emergent orchestration | Zero implementation (`participant_contract`, `quality_signal`) | issue #4, `docs/swarm_orchestration.md` |

### B. Daemons with stubbed legs
| Item | State | Evidence |
|---|---|---|
| Tyto OCR | Screenshots stored `status:"pending_ocr"`; **no consumer exists** — OCR dispatch, entity extraction, task linking all unbuilt | `tyto.py:9,428-430,470` |
| Turdus classification | Gmail poll + entity creation real; the LLM triage classifier ("Phase 7") is a stub | `turdus.py:19-20` |
| Formica | Python daemon is a Phase-3 skeleton; approval flow + PR pipeline still live in legacy Node.js | `formica.py:11-17`, issue #1 |
| neotoma-agent | Runs, but issue #3's wire-up checklist (SSE, AAuth signer, notifications) unchecked | issue #3 |
| Cyphorhinus | Deprecated (E6) — stand down once email loop live; taxonomy still lists it as the audio importer | `cyphorhinus.py:5-11` |

### C. Identity, integrity, and provenance
| Item | State | Evidence |
|---|---|---|
| AAuth signing | Runs in **stub/unsigned mode** wherever keypairs aren't minted; JWKS publishes only `sw-cursor-1`; grants missing for 6 daemons; rotation unbuilt | `lib/daemon_runtime/aauth_signer.py:151`, phases.md Phase 5 |
| `aauth.md` referenced layer | Five scripts (`aauth_provision_identity.py`, `mcp_identity_proxy.py`, …) + website JWKS tree absent from repo | design-doc audit |
| Session integrity | Layer 1 hooks shipped but **WARN-mode default** (BLOCK coded, dormant); layer 2 (grant proxy) observe-only; layer 3 absent | `.claude/hooks/stop_finalizer.py:36,100-102` |
| Input attribution | Umbrella #19; parts #23/#24/#25 open | issues #19,#23–25 |
| Retrieve-scoped grants, tier-conditional caps, R3 conditionals | Designed, unstarted | issues #30–#35 |

### D. Productization & platform
| Item | State | Evidence |
|---|---|---|
| `ateles` CLI | `init`/`doctor`/`provision` (dry-run) shipped; **`provision --commit`, `run`, `deploy`, `mirror` are stubs** (exit 3); W5–W9 open. `installability.md` says "State: none" — stale in the pessimistic direction | `ateles/cli.py:56,84-93`, `ateles/provision.py:74-80` |
| Cloud hosting | Scaffolding (`deploy/cloud/`) landed; migration M2–M6 not executed; bootstrap skips launch pending age-key provisioning; no systemd units | `deploy/cloud/bootstrap-host.sh:8,96`, issue #50 |
| Multi-tenant "do-now" hedges | Not done (no `tenant_id`, `match_tenant`, subject namespacing) — exactly the cheap-now items the doc warns against deferring | `docs/multi_tenant.md` |
| Durable execution substrate | Keystone primitives (`scheduled_wake`, `run` entity, `ExecutionBackend`) unbuilt — position paper only | `docs/durable_execution_substrate.md` |
| Loxia GHA | Script + tests exist; inactive pending `ANTHROPIC_API_KEY` repo secret (manual operator step) | `loxia_review.py:9`, CLAUDE.md |
| GitHub interaction Layer C | No formal PR Reviews from panel (issue #241), no `.github/` issue/PR templates, designed `gate:*/phase:*` label taxonomy differs from `lib/issue_labels.py` | design-doc audit |

### E. Quality / test debt
- Well-tested: apis, lib/daemon_runtime, lib/notify, grant proxy, ateles CLI, asana MCP, riparia, aquila, orchestrator.
- **Zero tests**: apus, cotinga, formica, gorilla, cyphorhinus, morning-brief,
  neotoma-agent, piculet, strix, sylvia, tyto, anthus daemon-main, lib/activity,
  lib/issue_labels, and 8 of 10 MCP servers.
- **Monedula money-movement handlers untested** (only reply parsing) — highest-risk
  code, lowest coverage.
- 53 `test_*.py` files outside `ateles/` never run in CI (issue #229; partial
  per-daemon fixes in PRs #210/#228 — which themselves overlap).
- Parquet MCP: audit logging and auto-embeddings **disabled in code**
  ("temporarily… to isolate error", `parquet_mcp_server.py:875,2103-2121`).

---

## 3. Capture assessment

### Well captured (issue and/or plan checkbox exists; work is "just do it / merge it")
- Review/gating pipeline bugs: #241, #240, #239, #236, #235, #234→PR #233, #211,
  #130, #128, #95, #94, #191, #209.
- Daemon notification reliability: #243→#244, #225→(#226|#228), #224→(#223|#227),
  #205→#206, #221, #202, #192, #212→#213.
- Session-integrity source-of-truth model: #214→PR #219 (with interim #218).
- Sibling-repo guard: #246→PR #245.
- Renames: #170, #171, #128.
- Formica/neotoma-agent wire-up: #1, #3. Emergent orchestration: #4.
- AAuth backlog: #30–#35, #44. Attribution: #19/#23–25. Security evals: #38–#40.
- Installability umbrella: #18. Cloud hosting: #50.
- Phase 5/6 unchecked plan items (grants, JWKS, grant tightening, rotation, smoke
  tiers 2–5, webhook key rotation, Menura instance) — captured in `phases.md`.

### Captured but degraded (artifact exists; needs reconciliation to be actionable)
- **Duplicate PR pairs**: #223 vs #227 (Turdus), #226 vs #228 (Piculet) — one of
  each must close before merge. #210's CI workflow overlaps #228's; #218 is
  arguably superseded by #219.
- **Stale PRs**: #163 (/release skill, 26 days), #166 (docs/shared relocation, 26
  days) — refresh or close.
- **Operator-blocked PRs**: #173 (AppleScript verification), #174 (Monedula email
  approval — *undeployed, payments held*, was stuck on the #187 gate bug), #217
  (Mimus daemon never installed). Each needs an explicit owner/next-step note.
- **Umbrellas without live child tasks**: #18, #50, #44 — fine as trackers, but the
  next concrete increment isn't broken out anywhere.

### Not captured anywhere (proposed new issues in §4)
1. Tyto OCR consumer (pending_ocr backlog).
2. Turdus real LLM triage classifier.
3. Parquet MCP: re-enable audit log + embeddings on `add`.
4. A2A gateway activation: merge `feat/apis-a2a-gateway`, add plist, decide flag default.
5. Session-integrity enforcement rollout: BLOCK-mode flip criteria + layer-2 enforce + layer-3.
6. Monedula payment-handler test coverage.
7. `/reject` reasons → review_learning feedback loop.
8. Phantom/stale docs reconciliation (agent_execution_architecture, credential_management, aauth script refs, mcp_resource checklist claim, documentation_plan certifications, installability status column, forking.md installer line).
9. Missing repo artifacts cited by runbooks: `com.ateles.riparia.plist`, `com.ateles.apis-a2a.plist`.
10. Orphan-branch triage sweep (53 branches; 8 named-feature candidates).
11. `.github/` issue/PR templates + label-taxonomy reconciliation (Layer C).
12. Merge-queue drain: reconcile duplicates, land the gate-fix PRs, re-run panel.

### Plan-side corrections needed (Neotoma writes; blocked in this session — no MCP access)
- **taxonomy_markdown**: add Piculet (audio import — took over from Cyphorhinus) and
  Riparia (email reply routing, E3); mark Cyphorhinus deprecated/break-glass;
  correct Aquila (live monthly cofounder-report daemon, not "planned quarterly
  portfolio review"); reflect Gorilla + Morning-brief daemons; reconcile the many
  "planned" T4 agents that already ship as skills (pavo, manucode, waxwing,
  accipiter, hirundo, aythya, buteo, ciconia, columba, robin, regulus, sturnus,
  corvus, …) with an honest status (skill-only vs wired).
- **phases_markdown**: check off / annotate items landed since last render (Tier-3
  grant-proxy work, a2a inbound); add the uncaptured items from §4 to their phases
  (Tyto OCR → Phase 3 leftovers, Turdus classifier → Phase 7, session-integrity
  rollout → Phase 6).
- **CLAUDE.md**: replace the stale "30 unique open issues (#368–#416)" note (actual:
  47 open, #1–#246); refresh the Phase 5–6 blocker list (several ✅ items can move
  to history).
- Create `task` entities PART_OF plan `ent_99ace4dd6673aa36ed08b1fe` for §4 items
  once issues are filed, per the update-tasks skill.

---

## 4. Proposed new issues (uncaptured work)

| # | Proposed title | Why / anchor |
|---|---|---|
| 1 | Tyto: build the OCR consumer for `pending_ocr` screenshots (dispatch, entity extraction, task linking) | `tyto.py:428-470`; silent backlog growth |
| 2 | Turdus: implement the real LLM triage classifier (replace Phase-4 stub) | `turdus.py:19-20`; classifier precision issue #205 only patches the stub |
| 3 | Parquet MCP: re-enable audit logging + auto-embeddings on `add`; remove disabled schema path | `parquet_mcp_server.py:875,2103-2121`; writes currently unaudited |
| 4 | Apis A2A: merge `feat/apis-a2a-gateway`, commit `com.ateles.apis-a2a.plist`, decide `APIS_A2A_ENABLE` default | gateway shipped dark |
| 5 | Session integrity: define BLOCK-mode rollout criteria; enforce layer 2; scope layer 3 | `stop_finalizer.py:36`; enforcement built but dormant |
| 6 | Monedula: unit tests for wise_transfer / btc_transfer / payment_profile | financial code, zero coverage |
| 7 | Apis: wire `/reject` reasons into review_learning.propose_skill_updates | `swarm_dispatch.py:2724` "future TODO" |
| 8 | Docs: reconcile phantom/stale docs (agent_execution_architecture, credential_management, aauth script refs, mcp_resource "COMPLETE" claim, documentation_plan certifications, installability status) | design-doc audit; operator-misleading |
| 9 | Repo: add missing launchd plists cited by runbooks (riparia, apis-a2a) | email-loop + a2a runbooks `cp` nonexistent files |
| 10 | Repo hygiene: triage 53 no-PR remote branches (8 named feat/* candidates first) | squash-merge style makes these invisible |
| 11 | GitHub Layer C: issue/PR templates + reconcile label taxonomy with `lib/issue_labels.py` | `swarm_github_interaction_design.md` |
| 12 | Merge-queue drain: close duplicate PRs (#223/#227, #226/#228, #218-vs-#219, #210-vs-#228), land gate fixes, re-review queue | 26 open PRs, self-deadlocked |

---

## 5. Recommended sequence

1. **Drain the merge queue** (proposed issue 12): land #187 + #233 + #244 + #245,
   fix or waive #211's arch-gate config, reconcile duplicate pairs, then sweep the
   remaining 12-day-old PRs. Everything downstream moves faster.
2. **Deploy what's built but dark**: Monedula email approval (#174 — payments are
   held), Loxia GHA (`ANTHROPIC_API_KEY` secret — operator), A2A gateway (issue 4),
   riparia plist (issue 9).
3. **File the §4 issues + create matching plan tasks** in a Neotoma-connected
   session; apply the §3 plan-mirror corrections and re-render.
4. **Close the Phase-5/6 identity gap**: mint remaining keypairs, provision grants,
   publish JWKS — unblocks AAuth stub-mode everywhere and smoke Tier 3.
5. **Finish the skeletons in dependency order**: Anthus coordinator (needs #172 +
   neotoma#1749) → smoke tiers 2→5; Formica Node→Python migration (#1); Turdus
   classifier; Tyto OCR.

---

## 6. Follow-through (applied 2026-07-28, once Neotoma access became available)

Before writing any corrections, live state was re-checked against the 07-21 data
collection — a full week had passed and the swarm had kept working. Confirmed
resolved in the interim: issues **#241** (formal GitHub Reviews) and **#246**
(sibling-repo guard) closed; PRs **#245, #233, #242, #218** no longer open. Open
issues actually grew **47→54** and open PRs **26→29** over that week despite this
activity — a throughput problem, not a stalled queue (§5 point 1's framing still
holds, but see the plan decision `merge_queue_is_primary_bottleneck` for the
current numbers). Issues **#211, #239, #235, #240** were re-checked live and are
still open, each already carrying a full swarm-authored spec (PM/UX/Eng/QA/Arch
sections) — spec'd but not built. In-flight attempts at the same problems: PR
**#296** (pr_review entities anchored to head_sha) and PR **#268** (stale-SHA
fix-round guard), neither merged yet.

Also caught and corrected: the plan's `next_steps` field carried a 2026-06-23 claim
that PR #172 (commit `f4b9581`) had landed and produced "the first live autonomous
swarm dispatch." Git confirms `f4b9581` is **not** an ancestor of `origin/main`
(re-verified 2026-07-28: still not merged, PR #172 still open at 25 days) — that
claim described a pre-merge dev verification, not a shipped fact. Corrected in the
plan's `decisions` (`anthus_hydrate_fix_pr172_unmerged`) and `next_steps`.

**Neotoma plan `ent_99ace4dd6673aa36ed08b1fe` corrected** (merged, not overwritten —
prior fields preserved): `todos`, `decisions`, `next_steps`, `taxonomy_markdown`,
`phases_markdown`. Taxonomy fixes: Pavo/Waxwing/Sturnus/Corvus flipped
planned→active to match their real `agent_definition` status (verified against
`docs/agents/*.md`); Sturnus's one-liner corrected from "feedback digester" to its
actual current scope (full relationship-management/CRM agent — a substantive
redefinition, not just a status flip); Corvus corrected from "outbound poster" to
"content writer & social voice"; added missing Piculet + Riparia T3 rows; marked
Cyphorhinus deprecated (break-glass only); moved Aquila from a stale T4-planned
"quarterly portfolio review" line to a corrected T3/T4 dual-mode active "monthly
cofounder report + on-demand strategic consult" row (`quarterly-portfolio-review`
is a distinct, unrelated skill). `docs/taxonomy.md` and `docs/phases.md` updated
to match (the render script needs live `NEOTOMA_BASE_URL`/REST access this session
didn't have, so the mirrors were hand-synced to the exact corrected field content
and diffed to confirm an exact match).

**12 `task` entities created** PART_OF the plan for §4's uncaptured work, and **12
GitHub issues filed** (#302–#313), one per task, each task's `related_url`
corrected to point at its issue:

| Task | Issue |
|---|---|
| Tyto OCR consumer | [#302](https://github.com/markmhendrickson/ateles/issues/302) |
| Turdus LLM classifier | [#303](https://github.com/markmhendrickson/ateles/issues/303) |
| Parquet MCP audit/embeddings re-enable | [#304](https://github.com/markmhendrickson/ateles/issues/304) |
| A2A gateway activation | [#305](https://github.com/markmhendrickson/ateles/issues/305) |
| Session-integrity BLOCK-mode rollout | [#306](https://github.com/markmhendrickson/ateles/issues/306) |
| Monedula payment-handler tests | [#307](https://github.com/markmhendrickson/ateles/issues/307) |
| `/reject` → review_learning wiring | [#308](https://github.com/markmhendrickson/ateles/issues/308) |
| Phantom/stale-doc reconciliation | [#309](https://github.com/markmhendrickson/ateles/issues/309) |
| Missing launchd plists (riparia, apis-a2a) | [#310](https://github.com/markmhendrickson/ateles/issues/310) |
| Orphan-branch triage | [#311](https://github.com/markmhendrickson/ateles/issues/311) |
| GitHub Layer C templates/labels | [#312](https://github.com/markmhendrickson/ateles/issues/312) |
| Merge-queue drain | [#313](https://github.com/markmhendrickson/ateles/issues/313) |

One correction to this process: the first `store()` call for the 12 tasks omitted
`entity_type` inside each entity payload and landed as `generic` instead of
`task` (schema mismatch, 4 unknown fields each). Caught immediately from the
store warnings, all 12 were deleted (`delete_entity`, reversible) and recreated
correctly with zero unknown-field warnings before the GitHub issues were filed
against them.
6. **Test debt**: monedula handlers first (risk), then the CI-dark suites (#229).
