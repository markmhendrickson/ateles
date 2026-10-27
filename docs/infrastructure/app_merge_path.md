# Merge path for any app repository (bootstrap mode)

## Purpose

Give a session the exact steps to review and approve a pull request in ANY repository, ateles or an app repository, in bootstrap mode, with the lens panel chosen by what the PR changes. The steps run from the session; nothing here needs a dispatcher, Apis or a gate daemon, and no daemon has to be running or healthy.

## Scope

Covers the session-run path only: select the panel, run the lenses as subagents, post each verdict, approve. It does not cover the swarm pipeline's own dispatch (the canary lane keeps that), release approval, or deploys. The rule it implements is the operator ruling of 2026-10-27 in agent_policy `ent_d0f1a840e549b3b299f62397`; that entity governs if this page and it ever disagree.

## The rule

- Lens selection is by changed path. The full panel (pm, arch, ux, qa, security) reviews any change touching external sending, payment, sign-in or authorization; credentials or secrets handling, data writes, schema or migrations; deployment or release configuration, publication; CI workflow definitions, dependency manifests or lockfiles, hooks; the gate, approval and review machinery; or rule and policy text (agent_policy, CLAUDE.md, AGENTS.md, execution_policy). Security reviews every change that touches any of those paths, without exception.
- The reduced route (green CI plus one lens) applies only to a change confined to application code, tests, copy, UI or docs outside those paths. When a change's classification is unclear, it takes the full panel.
- Any other change needs green CI plus the one lens whose scope it touches: ux when every changed file is user-facing copy or UI, arch otherwise. A reviewer may still request another lens with a stated reason: either run the approval tool with `--lenses <lens>` (it adds that lens to the required set), or have that lens post an objection on the current head, which blocks the approval until a clearing verdict retires it.
- A lens's approval carries to a new head when the diff since its approved head does not touch that lens's scope. It is re-run only when its scope changed.

## The repo declares its own scope

Each repository declares what is risky for it in `.github/review_scope.json`:

```json
{
  "version": 1,
  "full_panel_paths": [".github/review_scope.json", "globs of every risky path, per the list above"],
  "reduced_paths": ["globs of application code, tests, copy, UI and docs outside the risky paths"],
  "ux_paths": ["globs of user-facing copy and UI"],
  "lens_scopes": {"security": ["globs"], "arch": ["globs"], "ux": ["globs"], "qa": ["globs"]}
}
```

- The file is read from the PR's **base branch**, never its head, so a PR cannot downgrade its own panel. The tool re-checks, just before it approves, that the PR still targets the branch the file was read from, and refuses if it was retargeted. `--panel path` also requires the PR's base to be the repository's default branch; any other base, or a default branch it cannot read, takes the full panel. A change to the file itself always selects the full panel, whether or not the file lists itself (list it anyway, as above, so the rule is visible).
- `reduced_paths` is the positive list of what the reduced route may cover. Keep it explicit: a brand-new file that no list names is unclear and takes the full panel, which is the point. Do not list a whole directory of scripts that includes anything that writes data. A changed file on `full_panel_paths` takes the full panel; a file on neither `full_panel_paths` nor `reduced_paths` (or `ux_paths`) is unclear and also takes the full panel, so a scope that classifies nothing never selects one lens. List the paths for hooks, dependency manifests and lockfiles, Dockerfiles and `fly.toml`, release and deploy scripts, migrations and schema code, credential and sending code, and rule and policy text (`CLAUDE.md`, `AGENTS.md`, anything that writes `agent_policy` or `execution_policy`) in `full_panel_paths`; the `**/` forms catch them in subdirectories.
- A missing, unreadable, malformed or empty-`full_panel_paths`/`reduced_paths` file, an unrecognised key, and an empty changed-file list all select the **full panel**, and the tool says why. So does a file list the tool cannot trust: the GitHub files API stops at 3000 files, and a list that reaches that cap, or whose length differs from the PR's own `changed_files` count, selects the full panel. A new app repository therefore gets the full panel until it commits a scope file to its default branch.
- A rename counts both its new and its old path, so moving a file out of a full-panel path still triggers the full panel. One lens is chosen only when the choice is unambiguous: ux when **every** changed file is on `ux_paths`, arch otherwise.
- `lens_scopes` is optional per lens. Where a lens has a declared scope, its earlier verdict carries only if no file in the delta since that head matches those globs. A lens with no declared scope keeps the default rule (every file counts as in scope, except a short allowlist of docs and tests). **A declared scope replaces the default rule for its lens, so it can widen what re-runs as well as narrow it.** With Ateles's broad scopes, a test-only delta re-runs arch and security as well as qa, where the default rule would have carried arch and security. List every path a lens should see again, and nothing it should not.
- Globs match the whole repo-relative path, ignoring case and Unicode composition (the checkouts are case-insensitive, so `Execution/daemons/x.py` is `execution/daemons/x.py`): `*` stays within a directory, `**` crosses directories, a trailing `/` means everything below, and there is no basename-only matching. The format is documented in `execution/daemons/apis/review_scope.py`.
- Ateles's own file is `.github/review_scope.json`. For an app repository, write one with the same shape; err on the side of putting a path in `full_panel_paths`.

## Steps

All commands run from an ateles checkout, with the environment the swarm tools use: the swarm GitHub App must be configured through `ATELES_REVIEWER_APP_ID` and `ATELES_REVIEWER_APP_PRIVATE_KEY` (or `ATELES_REVIEWER_APP_PRIVATE_KEY_PATH`), and `ATELES_REVIEWER_APP_INSTALLATION_ID` if you want to skip the lookup; a lens with its own App uses `ATELES_APP_REVIEWER_<LENS>_ID` and `_PRIVATE_KEY(_PATH)`. `python3 lib/github_app_token.py status` lists which are set, by name only. See `docs/swarm-trigger-layer.md`. Replace `<repo>` and `<n>`.

1. **Dry run the gate to see the panel.**
   `python3 execution/scripts/approve_pr_as_app.py --repo <repo> --pr <n>`
   The default is `--panel path`. It prints the mode, the file that triggered the full panel (or the lens chosen and why), and where the scope file was read from. `--panel all` and `--panel required` still exist.
2. **Load the lens prompts** for the lenses it names from their `agent_definition` entities in Neotoma (Pavo pm, Waxwing arch, Falco security, Phoenicurus qa, Accipiter ux). Do not paraphrase them.
3. **Run each lens as an independent subagent** on a read-only checkout of the exact head (run `git worktree add --detach <path> <head sha>` inside the **app repository's own clone**, not the ateles checkout, then give the subagent only that path, read-only, with no credentials in its environment). One lens per subagent, no shared context between lenses, and none gets the author's reasoning. Each writes a reply whose first line is its attribution header and second line its verdict (`**SIGNED_OFF**`, `**BLOCKED**`, `**REQUEST_CHANGES**`, `**COMMENT**`).
4. **Post each verdict:**
   `python3 execution/scripts/post_lens_verdict.py --repo <repo> --pr <n> --lens <key> --body-file <reply file> --head <sha the lens reviewed>` (`--head` is required)
   By default it posts as the swarm GitHub App using an installation token minted for that repository, so it works on a private repository the App is installed on. `--token-env NAME` posts with a PAT instead. It refuses a head that moved, a verdict the approval tool cannot read, a sign-off that contains a blocking token, and an identity the approval tool would not admit as a lens author. A second post for the same head edits the first.
5. **Approve:**
   `python3 execution/scripts/approve_pr_as_app.py --repo <repo> --pr <n> --apply`
   It approves as the App only when every required lens has cleared the current head (or carries from an earlier head), no lens objects on the current head, and every required check is green. Otherwise it exits non-zero and submits nothing.
   **Three rounds, then split.** A repair unit that has not merged after three build, review and correct rounds is split or cut in scope before another round runs (operator ruling 2026-10-27). The tool counts a round as one head of the PR that received a lens verdict, read from the swarm's own verdict comments and their `commit=` markers. When the head in front of it would be a fourth round or later, it prints `round cap reached: split or cut scope` and refuses, even if every lens cleared. If the operator grants an overrun for that PR, re-run with `--allow-round-overrun '<reason>'`; the reason is required (10 to 300 characters; angle brackets, `@`, square brackets, backticks and control characters are dropped) and is recorded in the approval review. If the PR is not over the cap the flag is reported as not needed and nothing is recorded. There is no environment setting for it, and it does not lift a lens objection or a red check.
6. **Merge only after step 5 reports `APPROVED`.** If the head moves, repeat from step 3 for the lenses whose scope changed; the others carry.

## No daemon is involved

Every step above is a script that reads GitHub and, in steps 4 and 5, writes one comment or one review. The panel is chosen by `approve_pr_as_app.py` from the scope file; the verdicts are posted by the session; the approval is checked by the tool itself. The swarm dispatcher, Apis and the gate daemons are not called, are not consulted, and need not be running.

## Same-named jobs in different workflows

A repository can have a job of the same name in more than one workflow (Ateles has `pytest` in two). The approval tool treats a check as the same only when its name, App and Actions workflow all match; a re-run of a workflow (closed and reopened, or CI re-triggered) replaces the older run of that workflow only, and the older run is listed as superseded, naming the newer run's workflow. A failure in one workflow is never cancelled by a pass in another. A run the tool cannot place in a workflow, or whose start time is in the future, is never superseded and never supersedes.

## A repository with no CI cannot be approved

The tool requires green required checks, and a head with no check runs and no commit statuses reads as not green: nothing has run, so nothing has passed. A repository with no CI therefore never reaches `APPROVED`; add at least one workflow (even a lint or build job) before using this path there.

## A repository without branch protection

On a repository with no branch protection (for example a private repository on a free plan), GitHub enforces nothing: it will merge a PR with no approval, a failing check, or a stale review. There the approval tool's refusal is the **only** gate. Merge only after `approve_pr_as_app.py --apply` has printed `APPROVED`, and never as a way around a refusal. Without protection there is also no "approval of the most recent reviewable push" rule: re-run the tool after every push, since a later push is not covered by an earlier approval.

## Identity notes

- The App can be both the verdict author and the approving reviewer on one PR. The tool's distinctness checks are about the PR's author and the PR-opener App, not about who commented, and a comment is not a push. The only refused case is a PR the approving App itself opened.
- A lens that has an App of its own (`ATELES_APP_REVIEWER_<LENS>_*`) posts and approves as that App, and the shared App is then not read for that lens. A declared but unusable lens App refuses; it never falls back to the shared App.
- Installation tokens cannot call `GET /user`. The poster reads the App's login from the App itself.
- Neither tool prints a token or key.
