# Merge path for any app repository (bootstrap mode)

## Purpose

Give a session the exact steps to review and approve a pull request in ANY repository, ateles or an app repository, in bootstrap mode, with the lens panel chosen by what the PR changes. The steps run from the session; nothing here needs a dispatcher, Apis or a gate daemon, and no daemon has to be running or healthy.

## Scope

Covers the session-run path only: select the panel, run the lenses as subagents, post each verdict, approve. It does not cover the swarm pipeline's own dispatch (the canary lane keeps that), release approval, or deploys. The rule it implements is the operator ruling of 2026-10-27 in agent_policy `ent_d0f1a840e549b3b299f62397`; that entity governs if this page and it ever disagree.

## The rule

- Lens selection is by changed path. The full five-role panel (pm, arch, ux, qa, security) reviews changes that touch external sending, payment, sign-in or authorization, data writes, schema, or the gate, approval and review machinery itself.
- Any other change needs green CI plus the one lens whose scope it touches: ux for user-facing copy and UI, arch otherwise. A reviewer may still request another lens with a stated reason; a non-required lens that objects on the current head blocks the approval.
- A lens's approval carries to a new head when the diff since its approved head does not touch that lens's scope. It is re-run only when its scope changed.

## The repo declares its own scope

Each repository declares what is risky for it in `.github/review_scope.json`:

```json
{
  "version": 1,
  "full_panel_paths": ["globs of risky paths"],
  "ux_paths": ["globs of user-facing copy and UI"],
  "lens_scopes": {"security": ["globs"], "arch": ["globs"], "ux": ["globs"], "qa": ["globs"]}
}
```

- The file is read from the PR's **base branch**, never its head, so a PR cannot downgrade its own panel. A change to the file is reviewed under the previous version, and the file lists itself in `full_panel_paths` so that review is the full panel.
- A missing, unreadable, malformed or empty-`full_panel_paths` file, an unrecognised key, and an empty changed-file list all select the **full panel**, and the tool says why. A new app repository therefore gets the full panel until it commits a scope file to its default branch.
- `lens_scopes` is optional per lens. Where a lens has a declared scope, its earlier verdict carries only if no file in the delta since that head matches those globs. A lens with no declared scope keeps the default rule (every file counts as in scope, except a short allowlist of docs and tests). A declared scope can only narrow what re-runs, so list every path a lens would want to see again.
- Globs match the whole repo-relative path: `*` stays within a directory, `**` crosses directories, a trailing `/` means everything below, and there is no basename-only matching. The format is documented in `execution/daemons/apis/review_scope.py`.
- Ateles's own file is `.github/review_scope.json`. For an app repository, write one with the same shape; err on the side of putting a path in `full_panel_paths`.

## Steps

All commands run from an ateles checkout, with the environment the swarm tools use (App id and private key for the swarm GitHub App; see `docs/swarm-trigger-layer.md`). Replace `<repo>` and `<n>`.

1. **Dry run the gate to see the panel.**
   `python3 execution/scripts/approve_pr_as_app.py --repo <repo> --pr <n>`
   The default is `--panel path`. It prints the mode, the file that triggered the full panel (or the lens chosen and why), and where the scope file was read from. `--panel all` and `--panel required` still exist.
2. **Load the lens prompts** for the lenses it names from their `agent_definition` entities in Neotoma (Pavo pm, Waxwing arch, Falco security, Phoenicurus qa, Accipiter ux). Do not paraphrase them.
3. **Run each lens as an independent subagent** on a read-only checkout of the exact head (`git worktree add --detach <path> <head sha>`, no write access, no credentials in its environment). One lens per subagent, no shared context between lenses, and none gets the author's reasoning. Each writes a reply whose first line is its attribution header and second line its verdict (`**SIGNED_OFF**`, `**BLOCKED**`, `**REQUEST_CHANGES**`, `**COMMENT**`).
4. **Post each verdict:**
   `python3 execution/scripts/post_lens_verdict.py --repo <repo> --pr <n> --lens <key> --body-file <reply file> --head <sha the lens reviewed>`
   By default it posts as the swarm GitHub App using an installation token minted for that repository, so it works on a private repository the App is installed on. `--token-env NAME` posts with a PAT instead. It refuses a head that moved, a verdict the approval tool cannot read, a sign-off that contains a blocking token, and an identity the approval tool would not admit as a lens author. A second post for the same head edits the first.
5. **Approve:**
   `python3 execution/scripts/approve_pr_as_app.py --repo <repo> --pr <n> --apply`
   It approves as the App only when every required lens has cleared the current head (or carries from an earlier head), no lens objects on the current head, and every required check is green. Otherwise it exits non-zero and submits nothing.
6. **Merge only after step 5 reports `APPROVED`.** If the head moves, repeat from step 3 for the lenses whose scope changed; the others carry.

## No daemon is involved

Every step above is a script that reads GitHub and, in steps 4 and 5, writes one comment or one review. The panel is chosen by `approve_pr_as_app.py` from the scope file; the verdicts are posted by the session; the approval is checked by the tool itself. The swarm dispatcher, Apis and the gate daemons are not called, are not consulted, and need not be running.

## A repository without branch protection

On a repository with no branch protection (for example a private repository on a free plan), GitHub enforces nothing: it will merge a PR with no approval, a failing check, or a stale review. There the approval tool's refusal is the **only** gate. Merge only after `approve_pr_as_app.py --apply` has printed `APPROVED`, and never as a way around a refusal. Without protection there is also no "approval of the most recent reviewable push" rule: re-run the tool after every push, since a later push is not covered by an earlier approval.

## Identity notes

- The App can be both the verdict author and the approving reviewer on one PR. The tool's distinctness checks are about the PR's author and the PR-opener App, not about who commented, and a comment is not a push. The only refused case is a PR the approving App itself opened.
- A lens that has an App of its own (`ATELES_APP_REVIEWER_<LENS>_*`) posts and approves as that App, and the shared App is then not read for that lens. A declared but unusable lens App refuses; it never falls back to the shared App.
- Installation tokens cannot call `GET /user`. The poster reads the App's login from the App itself.
- Neither tool prints a token or key.
