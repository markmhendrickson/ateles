---
name: release
description: "Cut a neotoma GitHub + npm + sandbox release following the canonical process. Use when the user says 'release', 'cut a release', 'ship vX.Y.Z', 'prepare a release', or invokes /release. Encodes the gates that were nearly skipped cutting v0.17.0. Can be invoked via /release."
triggers:
  - release
  - /release
  - cut a release
  - ship release
  - prepare a release
  - prep release
  - new release
  - create release
  - publish
  - /publish
user_invocable: true
---

# Release

Cut a neotoma GitHub + npm + sandbox release. This skill front-loads the gates. The **full step-by-step process** lives in the neotoma repo — read those documents at runtime before proceeding.

## Source of Truth (read at runtime)

| Document | Role |
|----------|------|
| `neotoma:.cursor/skills/release/SKILL.md` | Canonical step-by-step workflow: preflight → preview → security lane → test-coverage lane → RC PR → execute → post-release |
| `neotoma:docs/developer/github_release_process.md` | Template layout, supplement structure, Highlights drafting rule, validation-tightening policy, render commands |
| `neotoma:docs/developer/github_release_supplement.example.md` | Section pattern for the human-readable supplement |

**Always read those three documents before starting.** This skill does not duplicate them; it front-loads the gates that are easy to skip when improvising.

---

## Gates (mandatory — never skip)

### Gate 1: Security classify-diff

Run **before** any merge, tag, or publish:

```bash
npm run -s security:classify-diff -- --base <last-tag> --head origin/main
```

- If `sensitive=true`: a **`security_review.md`** (Step 3.5 of the source skill) and a **non-empty `Security hardening` section** in the supplement are MANDATORY. The release is blocked until both exist with non-placeholder content.
- If `sensitive=false`: Step 3.5 still runs; write `No security-sensitive surfaces touched.` in the `Security hardening` section so the trail is explicit.

Do not advance past the security lane (to merge, tag, push, `gh release create`, `npm publish`, or sandbox deploy) until the gate clears.

### Gate 2: Supplement section requirements

Every release supplement MUST contain:

- **Highlights** — 3–5 bullets, benefit-led (what the reader can now *do* or *know*), naming the specific hook (endpoint, MCP tool, CLI flag, schema field). Cut aggressively; single bug fixes belong in body sections, not Highlights.
- **Breaking changes** — explicit section required. Validation tightening (`additionalProperties: false`, narrowed enums, promoted-required fields, removed resolver tolerance) is ALWAYS breaking, even if never formally in `openapi.yaml`. If none: write `No breaking changes.`
- **Security hardening** — required regardless of sensitivity (write `No security-sensitive surfaces touched.` when not sensitive).
- **Upgrade notes** — what callers must change.

### Gate 3: Execution order

Run **every** step in this order — stopping early is a failed release:

1. **Preflight**: `git fetch`, `git log origin/main..origin/dev --oneline`, `git status --short`, submodule status, previous tag, current `package.json` version, existing GitHub Release check.
2. **Resolve version** and confirm with user.
3. **Preview**: draft supplement → render exact GitHub Release body via `npm run -s release-notes:render -- --tag vX.Y.Z --head-ref <release-ref>` → STOP and confirm with user.
4. **Security review lane (Step 3.5)**: classify-diff → `security:lint` → `security:manifest:check` + `test:security:auth-matrix` → `security:ai-review` → fill `security_review.md` → add `Security hardening` section → re-render + re-confirm if supplement changed materially.
5. **Test-coverage review lane (Step 3.6)**: walk user-facing surfaces → classify coverage → write `test_coverage_review.md` → resolve BLOCKING gaps before proceeding.
6. **RC PR (Step 3.7)**: push `release/vX.Y.Z` branch → open PR targeting `main` → post `@claude review` → STOP for human approval → merge only after user confirms execute.
7. **Execute (Step 4)**: version bump → `SECURITY.md` update (new minor) → `git checkout main && git pull` → tag → push tag + main → render final notes → **create GitHub Release as DRAFT** (`gh release create --draft`) → **`npm publish`** (irreversible; LAST before verification) → **deploy + verify sandbox** → **publish draft** (`gh release edit --draft=false`).
8. **Post-release (Step 5)**: deployed security probes → advisory publication (when referenced) → verify GitHub Actions CI passes → close resolved issues → move supplement to `completed/` → report summary.

### Gate 4: npm auth

- **Prefer a granular token with "Bypass 2FA" enabled** stored in 1Password at `~/.npmrc`. npm now recommends **Trusted Publishing** for CI — wire that when the operator's npm and GitHub accounts support it.
- **One-time OTP** (`npm publish --otp=<code>`) is the fallback.
- `op signin` does NOT propagate to non-interactive shells. The token MUST live in `~/.npmrc` before running `npm publish` in a non-interactive agent shell.
- After any browser web-login, run `npm whoami` in the **same shell** that will run `npm publish` before proceeding. If `npm whoami` fails, stop and give the operator copy-paste steps plus an explicit `ready` reply contract — do not end the turn without that handoff.
- Dry-run-scan the tarball for secret files before publishing:
  ```bash
  npm pack --dry-run 2>&1 | grep -E '\.(env|key|pem|p12|pfx|secret)' && echo "SECRET FILES DETECTED — abort" || echo "Clean"
  ```
- The build and publish MUST run from a **clean checkout pinned to the reviewed release commit**. Do not publish from a dirty tree.

### Gate 5: Sandbox verification

After `npm publish`, deploy and verify before publishing the GitHub Release draft:

```bash
flyctl deploy -c fly.sandbox.toml --remote-only \
  --build-arg NEOTOMA_GIT_SHA="$(git rev-parse HEAD)"
```

Verify all three:
```bash
curl -fsS -H "Accept: application/json" https://sandbox.neotoma.io/ | \
  node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const j=JSON.parse(s); const sha="'"$(git rev-parse HEAD)"'"; if (j.version !== "X.Y.Z" || j.mode !== "sandbox" || j.git_sha !== sha) { console.error(j); process.exit(1); } console.log(j.version, j.git_sha); })'
curl -fsSI https://sandbox.neotoma.io/health | grep -i '^x-neotoma-sandbox: 1'
```

The root JSON must report `version: X.Y.Z`, `mode: sandbox`, and `git_sha` equal to the released commit (40-char hex SHA, not a 26-char ULID). Only then run `gh release edit "vX.Y.Z" --draft=false`.

---

## Hub-nav convention

Any rendered_page (`/draft-rendered-page` or `/publish`) organized under a hub MUST carry a common "back to hub" navigation link so readers can return to the hub index. Check the hub's rendered_page entity for the canonical hub URL and link text before publishing child pages.

---

## Forbidden Patterns

- Skipping Gate 1 (`security:classify-diff`) before merge, tag, or publish.
- Omitting `Highlights`, `Breaking changes`, or `Security hardening` sections from the supplement.
- Treating validation tightening as non-breaking.
- Publishing the GitHub Release without `--draft` before sandbox is verified.
- Running `npm publish` before the GitHub Release draft exists and the RC PR is merged.
- Declaring the release complete without sandbox verification, deployed probes, and CI checks passing.
- Using `op signin` as a substitute for a persistent `~/.npmrc` token in non-interactive shells.
- Skipping the RC PR step and proceeding directly to tagging after review lanes pass.
- Publishing a GHSA before the fix tag is pushed and live on `main`.

---

## Constraints

- Always read the three source-of-truth documents at runtime before starting.
- Always STOP at Gate 3 Step 3 (preview), Step 3.7 (RC PR), and again when the user confirms execute — three explicit human checkpoints.
- Always run npm publish from a clean checkout pinned to the reviewed release commit.
- For a full release: GitHub Release creation, npm publish, and sandbox deployment are all mandatory unless the user explicitly scopes one out.
