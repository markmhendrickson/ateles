# Runbook — scrub PII from git history (make it undiscoverable on GitHub)

## Purpose

Finish removing third-party PII and operator-personal content from the **public** `markmhendrickson/ateles`
repo so it is **no longer discoverable via GitHub** — not just absent from the latest commit. Working-tree
removal (already done on the feature branch) is necessary but **not sufficient**: the data stays fully
browsable in old commits until `main`'s history is rewritten, force-pushed, and GitHub's cache is purged.

## Scope

Covers the irreversible, operator-run steps: rewriting history with `git filter-repo` (or `filter-branch`),
force-pushing all refs, purging GitHub's cached commit views, handling PRs and forks, and verifying. The
prior, in-mandate steps (removing the files from the working tree, gitignoring the paths) are already
committed; this runbook is what makes the removal *complete*.

> ⚠️ **This rewrites every commit SHA from the first affected commit onward.** All existing clones, open PRs,
> and the `claude/magical-johnson-t8vnhw` branch become stale and must be re-cloned/recreated. Coordinate
> before running, and prefer a quiet moment with no other work in flight.

---

## What was found and already removed

A first-principles audit of the working tree and all 51 commits found the exposure was **bounded and
shallow**:

| File | What it is | Status |
| --- | --- | --- |
| `docs/outreach/LINKEDIN_ICP_PRIORITY_LIST.md` | **814 third-party people** (names + LinkedIn profiles) — RGPD exposure. Entered in one commit (`94aa438`). | Removed from tree; `docs/outreach/` gitignored |
| `docs/health/lean_bulk_8_week_plan_2026.md` | Personal health notes | Removed from tree; `docs/health/` gitignored |
| `docs/health/facial_puffiness_mitigation_checklist.md` | Personal health notes | Removed from tree; `docs/health/` gitignored |

**No secrets, tokens, IBANs, national IDs, or real third-party emails/phones** were found, so **no credential
rotation is required.** (The 2 Bitcoin matches are a public "onboarding buddy" tip address in `loop-start` —
intentionally public, not PII.) If a future scrub ever includes a secret, **rotate it first** — a history
rewrite does not un-expose a leaked secret.

---

## Step 1 — Rewrite history with `git filter-repo` (recommended)

```bash
# 0) Coordinate: everyone will re-clone afterward. Make sure no PRs are mid-merge.

# 1) Fresh MIRROR clone (filter-repo requires a clean clone; --mirror gets all refs/tags)
cd /tmp
git clone --mirror https://github.com/markmhendrickson/ateles.git ateles-scrub.git
cd ateles-scrub.git

# 2) Install git-filter-repo if needed
#    macOS:   brew install git-filter-repo
#    pipx:    pipx install git-filter-repo
#    pip:     python3 -m pip install --user git-filter-repo

# 3) Purge the paths from ALL history (every branch + tag in the mirror)
git filter-repo --force --invert-paths \
  --path docs/outreach/LINKEDIN_ICP_PRIORITY_LIST.md \
  --path docs/health/lean_bulk_8_week_plan_2026.md \
  --path docs/health/facial_puffiness_mitigation_checklist.md
# Equivalent, broader: replace the three --path lines with
#   --path docs/outreach/ --path docs/health/

# 4) filter-repo removes 'origin' for safety — re-add it
git remote add origin https://github.com/markmhendrickson/ateles.git

# 5) Force-push the rewritten history for every ref and tag
git push --force --mirror origin
```

If `main` is protected, temporarily allow force-pushes (Settings → Branches → branch protection rule →
enable "Allow force pushes" for the run, or push from an admin), then re-enable protection.

### Step 1 (alternative) — `git filter-branch`, if filter-repo is unavailable

```bash
git clone --mirror https://github.com/markmhendrickson/ateles.git ateles-scrub.git
cd ateles-scrub.git
git filter-branch --force --index-filter '
  git rm -r --cached --ignore-unmatch \
    docs/outreach/LINKEDIN_ICP_PRIORITY_LIST.md \
    docs/health/lean_bulk_8_week_plan_2026.md \
    docs/health/facial_puffiness_mitigation_checklist.md
' --prune-empty --tag-name-filter cat -- --all

git for-each-ref --format='delete %(refname)' refs/original | git update-ref --stdin
git reflog expire --expire=now --all
git gc --prune=now --aggressive
git push --force --all origin
git push --force --tags origin
```

---

## Step 2 — Purge GitHub's cached commit views

After a force-push, GitHub still serves the old, now-unreachable commits **by SHA** (e.g.
`https://github.com/markmhendrickson/ateles/commit/94aa438`) until it garbage-collects. To force it, open a
private request to **GitHub Support** (https://support.github.com/contact):

> **Subject:** Purge cached commits after sensitive-data removal — markmhendrickson/ateles
>
> I removed sensitive data (a third-party contact list and personal notes) from the public repo
> `markmhendrickson/ateles` by rewriting history with `git filter-repo` and force-pushing all refs. Please
> purge any cached views of the now-unreachable commits and run garbage collection so the old blobs can no
> longer be retrieved by SHA. One of the rewritten commits is `94aa438`. Thank you.

Reference: GitHub Docs → "Removing sensitive data from a repository."

---

## Step 3 — PRs that contain the affected commits

GitHub keeps an immutable `refs/pull/<N>/head` for every PR, which can keep a removed blob reachable even
after the rewrite. For any PR whose history included the removed file:

- Identify them (the PR that introduced `94aa438`, and any branch PR built on top).
- **Close** them, and if they still expose the data, ask GitHub Support (Step 2) to purge those pull refs.
- Recreate the PR from a fresh, post-rewrite branch if the work is still needed.
- The current `claude/magical-johnson-t8vnhw` branch/PR is rewritten by Step 1's `--mirror` push; re-clone
  before doing further work on it.

## Step 4 — Forks and existing clones

- Any **fork** created before the scrub still contains the data. Check the repo's fork list (Insights →
  Forks). Ask fork owners to delete and re-fork, or delete forks that are yours.
- Anyone who **cloned** before the scrub still has it locally — unavoidable; the goal is to stop *new*
  discovery via GitHub.

---

## Step 5 — Verify

```bash
# Fresh clone AFTER the force-push:
git clone https://github.com/markmhendrickson/ateles.git verify && cd verify

git log --all --full-history --oneline -- docs/outreach/LINKEDIN_ICP_PRIORITY_LIST.md   # expect: empty
git log --all --full-history --oneline -- docs/health/                                   # expect: empty
git grep -iI 'linkedin\.com/in/' $(git rev-list --all) | head                            # expect: empty
```

- The old commit URL `https://github.com/markmhendrickson/ateles/commit/94aa438` should eventually **404**
  (after GC / Support purge).
- GitHub code search for a sample name from the old list should return nothing for this repo.

## Recurrence prevention (already in place)

`docs/outreach/` and `docs/health/` are now in `.gitignore`, so the files cannot be re-committed. Keep this
kind of content in `ateles-private` or Neotoma. Consider also adding a `gitleaks` rule that flags bulk
`linkedin.com/in/` URLs, and routing person-data through Neotoma context entities per
[forking.md](../forking.md).
