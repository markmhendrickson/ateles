# QE3 — Eval-authoring affordance (Phoenicurus can write + run evals)

**Status:** design (2026-06-19) · **Scope:** neotoma PRs first (operator-chosen) · **Depends on:** QE1 (merged #133)

**Goal:** let a dispatched Phoenicurus (qa-lens) child actually **author an eval fixture, run it, commit it, and push it to the PR branch** — so the `agentic_evals` CI lane becomes the qa-gate evidence (QE2). Today the child cannot: it runs diff-only with no working tree.

---

## The gap (verified 2026-06-19)

- **No `cwd`.** `skill_runner.run_skill` → `create_subprocess_exec(...)` passes **no `cwd=`**, so every dispatched child inherits the daemon's directory (`~/ateles-rc-src`). It has no checkout of the PR's code, let alone the PR branch.
- **Diff-only review.** `swarm_dispatch._changed_files` returns only filenames via the GitHub API. The review child works from diff text + `gh`. Fine for *commenting*; insufficient for *writing+running* an eval.
- **The harness lives in neotoma, not ateles.** `eval:tier1`, `tests/integration/agentic_eval_matrix.test.ts`, the `agentic_evals` CI lane, and the 7 `tests/fixtures/agentic_eval/*.json` fixtures are all in `~/repos/neotoma` (TypeScript). Ateles (Python) has no eval harness — so QE3 targets **neotoma PRs first**; ateles functional changes record `no agentic eval (Python; harness pending)` until an ateles harness exists (separate track).
- **Remotes are SSH.** `~/neotoma-rc-src` origin is an SSH remote (`ssh://...github...`). A per-agent push needs the agent's **HTTPS** `<AGENT>_AGENT_PAT`, so the worktree must push via an HTTPS-with-token URL, not the inherited SSH remote.

---

## The affordance — a PR-branch worktree as the qa child's `cwd`

When the swarm dispatches the **qa lens on a neotoma PR**, prepare a writable checkout of the PR branch and run the child there:

1. **Thread a `cwd` through `run_skill`.** Add `cwd: str | None = None`; pass it to `create_subprocess_exec(cwd=cwd)`. `None` = today's behavior (inherit daemon dir) — zero change for every other call site.
2. **Prepare a PR-branch worktree (neotoma only, qa lens only).** A new helper `prepare_pr_worktree(repo, pr_number, agent) -> path | None`:
   - Base it on the local clone (`~/neotoma-rc-src`), `git fetch origin pull/<n>/head` then `git worktree add <tmp> FETCH_HEAD` (detached) — or fetch the PR's head branch when same-repo.
   - Set the worktree's `origin` push URL to `https://x-access-token:<AGENT_PAT>@github.com/<repo>.git` so the child's `git push` authenticates as the agent (the #109 identity), independent of the SSH base remote.
   - Return the path; `None` on any failure (best-effort — the gate still works, the child just falls back to diff-only + records the harness as unavailable).
3. **Pass the worktree as the qa child's `cwd`.** Only for `lens.agent == "phoenicurus"` on a neotoma PR. All other lenses keep `cwd=None` (diff-only, unchanged).
4. **Cleanup.** `git worktree remove --force <tmp>` in a finally, like the MCP temp-file pattern already in `run_skill`.

The child, now sitting in a real neotoma PR-branch checkout, can: read the changed code, write a fixture into `tests/fixtures/agentic_eval/`, run `npm run eval:tier1` (or `eval:tier1:update` for the new fixture only), `git add` + `git commit` + `git push` to the PR branch. The pushed fixture then runs in the `agentic_evals` CI lane — the reproducible QA report (QE1's deliverable) and the QE2 gate evidence.

### Tools the qa child needs (tool_allowlist)
Phoenicurus's `agent_definition.tools` must permit `Bash`, `Read`, `Write`, `Edit` (to author the fixture + run npm + git). If it's `["*"]` already, nothing to do; if restricted, add them. (The QE1 prompt already tells it it MAY write under eval paths + run `eval:tier1` — QE3 makes that physically possible.)

---

## Why a worktree (not a fresh clone, not editing in place)

- **Not a fresh clone** — cloning neotoma per PR is slow + heavy; a `git worktree` off the existing `~/neotoma-rc-src` is cheap and shares the object store.
- **Not editing `neotoma-rc-src` in place** — that's the live prod-server's checkout; a parallel worktree isolates the child's writes from the running server (the [[shared-layer-isolation-hazard]] + [[daemon-deployment-fragility]] lessons — never mutate the running checkout).
- **Detached/PR-branch worktree** — the child commits to the PR branch and pushes; the base checkout is untouched.

---

## Build steps

- **QE3-a** — add `cwd` param to `run_skill` + `create_subprocess_exec(cwd=cwd)`. Pure plumbing, default `None` = no-op. Unit-testable.
- **QE3-b** — `prepare_pr_worktree` helper in `swarm_dispatch` (fetch PR head → `git worktree add` → set HTTPS-token push URL → return path; best-effort/None). + cleanup in finally.
- **QE3-c** — wire it: in the review panel, for the qa lens on a neotoma PR, prepare the worktree and pass it as `cwd`; skip for all other lenses/repos.
- **QE3-d** — ensure Phoenicurus `tools` includes Bash/Read/Write/Edit (correct the agent_definition if restricted).

Each step is independently shippable; QE3-a/b are safe no-ops until QE3-c wires them.

## Risks / notes
- **Push permission** — the agent must be a neotoma collaborator with push (the #109 provisioning). Until its `<AGENT>_AGENT_PAT` exists, the worktree prep can still run read-only (write fixture + run eval locally for the QA report) but cannot push — record that as a degraded-but-functional state, don't fail the gate.
- **Concurrency** — two PRs reviewed at once need distinct worktree paths (use the PR number + a temp suffix); `git worktree` supports multiple.
- **Don't block the panel** — worktree prep is best-effort + time-bounded; on failure the qa child falls back to diff-only and records `eval harness unavailable` rather than stalling.
- **Snapshot churn** — the child runs `eval:tier1:update` for its NEW fixture only; never a blanket snapshot regen (QE1 already instructs this).
- **Ateles PRs** — out of scope here; they record `no agentic eval (Python; harness pending)`. An ateles Python harness is a separate future track.
