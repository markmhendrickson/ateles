---
name: create-website
description: "Create a new website as a git submodule in execution/website. Use when user says \"create new website\" or \"add website for [domain/name]\". Can be invoked via /create-website."
triggers:
  - create new website
  - add website
  - create-website
user_invocable: true
entity_id: ent_ee8e38f35410f2dec7caf166
---

# Create Website

Create a new website in its own repository and add it as a submodule under `execution/website/[name]/`. Never create website files directly in the parent repository.

## When to Use

Use this skill when:
- User says "create new website", "add website for [domain/name]"
- User wants a new site (e.g. for a new domain) as a separate repo and submodule

## Required Documents (load first)

1. **Website development:** [docs/development_workflows_rules.mdc](docs/development_workflows_rules.mdc) (Website Development)

## Workflow

1. **Create new private GitHub repository** for the website. Use a simple domain-based name (e.g. `dionysiandesigns` for dionysiandesigns.com).
2. **Initialize repository** with website files: HTML, CSS, assets, README.md with deployment instructions and contact information.
3. **Add as submodule** in `execution/website/[name]/` directory (e.g. `execution/website/dionysiandesigns/`).
4. **Commit and push** website changes within the submodule repository.
5. **Update parent repository** if the submodule reference changed (commit submodule pointer in ateles).

## Constraints

- Always create websites as their own repositories; add as submodule in `execution/website/[name]/`.
- Never create website files directly in the parent repo or in a root `websites/` directory.
- Location: `execution/website/` — websites are execution artifacts per three-layer architecture.
- Submodule structure: website files in submodule root; README.md with deployment instructions; parent tracks submodule at `execution/website/[name]/`.

## Related Rules

- [docs/development_workflows_rules.mdc](docs/development_workflows_rules.mdc) — Website Development
- Examples: `execution/website/dionysiandesigns/`, `execution/website/hendricksonserrano/`
