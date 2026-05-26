---
name: deploy-website
description: "Deploy the markmhendrickson website: sync markdown edits to Neotoma, export website data, regenerate cache, update the CI export secret, push the website repo, and verify GitHub Actions."
triggers:
  - deploy website
  - deploy the site
  - push and deploy
  - deploy markmhendrickson
user_invocable: true
entity_id: ent_91dfe541e486b0aa9a9c75ee
---

# Deploy Website

**Scope: markmhendrickson website only.** The website is a git submodule at `execution/website/markmhendrickson/` and deploys from the `markmhendrickson/markmhendrickson` repo.

## Required documents (load first)

- `docs/development_workflows_rules.mdc` (Post markdown priority and deploy sync, Deployment verification, GitHub Actions re-run)
- `execution/website/markmhendrickson/.github/workflows/deploy.yml`

## Workflow

### 1. Pre-deploy: sync → export → cache (from ateles repo root)

Run:

```bash
python3 execution/scripts/sync_posts_to_neotoma.py
execution/scripts/export_neotoma_website_data.sh
python3 execution/scripts/generate_website_cache.py
```

Outputs:

- Export (do not commit): `data/tmp/neotoma_website_export.json`
- Cache (commit; deploy artifact):
  - `execution/website/markmhendrickson/react-app/cache/*.json`
  - `execution/website/markmhendrickson/react-app/cache/api/*.json`

### 2. Update CI export secret (required)

GitHub Actions requires `NEOTOMA_WEBSITE_EXPORT_JSON` (in `markmhendrickson/markmhendrickson`) to be the **base64-encoded** contents of `data/tmp/neotoma_website_export.json`.

Update the secret whenever the export changes so CI can deterministically rebuild cache before deploy.

### 3. Deploy: commit + push the website repo (not ateles)

```bash
cd execution/website/markmhendrickson
# stage website changes (including react-app/cache and deploy.yml changes)
git status
git add -A
git commit -m "chore: deploy"
git push origin main
```

### 4. Verify deployment

- Use the verify-deployment workflow against `markmhendrickson/markmhendrickson`.
- Identify the deploy run and iterate fix → rerun until it succeeds.

## Constraints

- Do not push `ateles` to deploy the site; push the `execution/website/markmhendrickson` submodule repo.
- Do not commit the export JSON; commit only cache outputs.
- `posts.private.json` is dev-only (production code paths do not load it).
