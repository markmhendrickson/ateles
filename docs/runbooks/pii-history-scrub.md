# Runbook — scrub PII from git history (make it undiscoverable on GitHub)

## Purpose

Make the third-party LinkedIn list and operator health notes **undiscoverable on the public
`markmhendrickson/ateles` repo** — not merely absent from the latest commit. The working-tree removal is
already on `main`; this runbook covers the irreversible history rewrite, the **GitHub Support request that is
the actual linchpin**, and verification.

## Scope

The operator-run, irreversible remediation: rewriting history across every branch that contains the data,
and filing the GitHub Support request to drop the immutable PR refs and purge GitHub's commit cache. The
already-done steps (removal from `main`'s tip, gitignore guards) are noted under Status.

---

## Status — what is already done

- ✅ **Removed from `main`'s tip** (commit `233f65a`): `docs/outreach/LINKEDIN_ICP_PRIORITY_LIST.md` and
  `docs/health/*` are gone from the default branch, so **GitHub code search and the browsable tree no longer
  surface them.** This already closes the realistic discovery vectors.
- ✅ **Gitignore guards** on `main`: `docs/outreach/` and `docs/health/` cannot be re-committed.
- ⏳ **Pending (this runbook):** the data still exists in older commits anchored by other branches + PR refs.

## The facts that shape this remediation

A verified audit (filter-repo dry run over a full mirror — 473 commits across 146 refs, rewritten in <1s)
established:

| Fact | Number | Consequence |
| --- | --- | --- |
| Commit that introduced all 3 files | `94aa438` (a big `.claude/` import) | Strip the **paths**, not the commit — the commit also added hundreds of legit files that must survive |
| Branches containing `94aa438` | **21** (incl. `main` + **19 other agents'/feature branches**) | Every one must be rewritten, or the blob stays reachable |
| Open PR refs (`refs/pull/*/head`) | **98** | **Cannot be force-pushed** — GitHub-managed; only Support drops them |
| Secrets / IBANs / national IDs in removed content | **0** | **No credential rotation required** |
| Real third-party profiles outside the 3 files | **0** | The companion generator script embeds no people (see note) — scope is exactly the 3 files |

### Why removal-from-main is not enough

GitHub serves a blob if it is reachable from **any** ref. After `main` is rewritten, the file is still
reachable via the other 20 branches, the 98 PR refs, and GitHub's by-SHA cache. Therefore:

1. **Every** branch containing `94aa438` must be rewritten and force-pushed — *not just `main`.*
2. The **98 PR refs cannot be rewritten by push.** Only a **GitHub Support request** drops them.
3. GitHub only garbage-collects unreachable blobs (and purges cached commit views) **after** Support acts.

A partial rewrite (e.g. `main` only) leaves live refs anchoring the blob, so a later Support GC **won't
remove it.** The rewrite must be comprehensive *before* Support runs the purge.

> ⚠️ **Coordination prerequisite.** Rewriting the 19 non-`main` branches changes their SHAs and will clobber
> any unmerged commits sitting on them. **Quiesce those branches first** (merge or pause the agents/sessions
> working on them); everyone re-clones afterward.

---

## Step 1 — Rewrite history on every affected branch (`git filter-repo`, verified)

```bash
# 0) Quiesce the 19 other affected branches first (see warning above).

# 1) Fresh MIRROR clone (filter-repo needs a clean clone; --mirror gets all refs)
cd /tmp
git clone --mirror https://github.com/markmhendrickson/ateles.git ateles-scrub.git
cd ateles-scrub.git

# 2) Install git-filter-repo (verified working via pip; brew also fine)
#    brew install git-filter-repo   |   python3 -m pip install --user git-filter-repo

# 3) Strip the PII paths from ALL history, on ALL refs
git filter-repo --force --invert-paths \
  --path docs/outreach/LINKEDIN_ICP_PRIORITY_LIST.md \
  --path docs/health/lean_bulk_8_week_plan_2026.md \
  --path docs/health/facial_puffiness_mitigation_checklist.md
# (broader equivalent: --path docs/outreach/ --path docs/health/)

# 4) Re-add origin (filter-repo drops it) and force-push every branch + tag.
#    Use --all (NOT --mirror): pushes branch heads + tags, skips the un-pushable
#    refs/pull/* and won't delete server refs.
git remote add origin https://github.com/markmhendrickson/ateles.git
git push --force --all origin
git push --force --tags origin
```

If `main` (or others) is protection-locked, temporarily allow force-pushes for the run, then re-enable.

### Step 1 fallback — `git filter-branch` (no install needed)

```bash
git clone --mirror https://github.com/markmhendrickson/ateles.git ateles-scrub.git && cd ateles-scrub.git
git filter-branch --force --index-filter '
  git rm -r --cached --ignore-unmatch \
    docs/outreach/LINKEDIN_ICP_PRIORITY_LIST.md \
    docs/health/lean_bulk_8_week_plan_2026.md \
    docs/health/facial_puffiness_mitigation_checklist.md
' --prune-empty --tag-name-filter cat -- --all
git for-each-ref --format='delete %(refname)' refs/original | git update-ref --stdin
git reflog expire --expire=now --all && git gc --prune=now --aggressive
git push --force --all origin && git push --force --tags origin
```

---

## Step 2 — GitHub Support request (the linchpin — only this removes the PR refs + cache)

After the rewrite, open a private request at https://support.github.com/contact:

> **Subject:** Purge cached commits + PR refs after sensitive-data removal — markmhendrickson/ateles
>
> I removed sensitive third-party data and personal notes from the public repo `markmhendrickson/ateles` by
> rewriting history with `git filter-repo` and force-pushing all branches. Please (1) garbage-collect the now
> unreachable objects, (2) purge cached views of the rewritten commits so they cannot be retrieved by SHA,
> and (3) drop or purge the `refs/pull/*` refs for pull requests whose history included the removed files —
> these still reference the removed content and cannot be rewritten by push. One of the rewritten commits is
> `94aa438`. Thank you.

Give Support the **specific 45 PR numbers** from the [Appendix](#appendix--affected-refs-snapshot-at-233f65a)
so they can target the exact `refs/pull/*` refs. Reference: GitHub Docs → "Removing sensitive data from a
repository."

## Step 3 — Forks and existing clones

Any fork or clone made before the scrub still has the data. Check Insights → Forks; ask fork owners to delete
and re-fork (or delete forks you own). Pre-scrub local clones are unavoidable — the goal is to stop new
discovery via GitHub.

## Step 4 — Verify

```bash
git clone https://github.com/markmhendrickson/ateles.git verify && cd verify
git log --all --full-history --oneline -- docs/outreach docs/health      # expect: empty
git grep -iI 'linkedin\.com/in/[a-z0-9]' $(git rev-list --all)           # expect: empty (placeholders aside)
```
- The old commit URL `https://github.com/markmhendrickson/ateles/commit/94aa438` should 404 after Support's GC.
- GitHub code search for a sample name from the old list should return nothing.

---

## Companion script — optional cleanup (NOT PII, not urgent)

`execution/scripts/linkedin_icp_priority_list.py` is the generator for the removed list. It reads the
operator's **local** LinkedIn export (`/Users/.../linkedin.zip`, never in the repo) and embeds **no real
people** (its only profile URL is an `XXX` placeholder), so it carries no third-party PII and is out of scope
for the scrub. It is now orphaned (its output dir is gitignored) and reveals the outreach methodology + target
company keywords. **Optional:** relocate it to `ateles-private` or delete it from the public repo. Decide
separately from the PII remediation.

## Recurrence prevention

- ✅ `docs/outreach/` + `docs/health/` gitignored on `main`.
- ➕ A `gitleaks` rule flags bulk `linkedin.com/in/` profile URLs (see `.gitleaks.toml`), so a contact dump
  trips the PII scan before commit. Keep person-data in Neotoma context entities per [forking.md](../forking.md).

---

## Appendix — affected refs (snapshot at 233f65a)

Computed from a full mirror. Commit `94aa438` is contained by these **21 branches** — all rewritten in one
pass by Step 1's `git push --force --all` (the 25 other branches are untouched):

```
chore/secrets-pipeline-live-doc          claude/cloud-hosting-scaffolding
claude/determined-bhabha-618850          claude/epic-mccarthy-0mkhw8
claude/fix-gitleaks-pii-email            claude/home-market-value-ot1mk7
claude/magical-johnson-t8vnhw            claude/neotoma-cofounder-agent-xm1ebo
claude/nostalgic-hertz-64ce81            claude/relaxed-almeida-7f6fcd
claude/repo-audit-readme-oag9ly          claude/taskspine-integration-tests
claude/youthful-chebyshev-7b51e1         feat/aauth-per-agent-signed-requests
feat/anthropic-key-via-sops              feat/chatgpt-workout-parser
feat/skill-sync-mirror                   fix/daemon-open-mode-auth
fix/intake-relationship-pii              main
secrets/relocate-to-private
```

> ⚠️ 19 of these are other sessions'/agents' branches — quiesce them before the rewrite (see Step 1).

…and it anchors **45 of 99 PR refs** that **only GitHub Support can drop** (Step 2):

```
68 73 111 113 114 115 116 117 118 119 121 122 123 124 125 126 129 131 132 133 134
135 138 139 140 141 142 143 144 145 146 147 148 149 150 151 152 153 154 155 156
157 158 159 160
```

A guarded one-shot script (`pii-scrub-kit.sh`) that runs Step 1 end-to-end — quiesce confirmation, mirror
clone, `filter-repo`, `push --force --all`, then prints the Step 2 Support text — is provided out-of-band. It
is deliberately **not committed to this public repo** (a runnable force-push-all script shouldn't live in
history). Review it before running.
