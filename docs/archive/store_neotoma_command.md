# Review Chat and Store in Neotoma

## Purpose

Direct users and agents to the canonical store-neotoma workflow (review chat, preview payloads, store in Neotoma) in the store-neotoma skill.

## Scope

Pointer document only; applies when invoking `/store_neotoma` or when persisting conversation to Neotoma. Full workflow lives in the skill.

---

Canonical workflow lives in the `store-neotoma` skill in the **foundation** submodule:

`foundation/.cursor/skills/store-neotoma/SKILL.md`

In this repo, `.cursor/skills/store-neotoma` is a symlink into that path so Cursor resolves the same file. After `git submodule update` or foundation changes, the symlink keeps the installed path current.

Invoke with `/store_neotoma` (or equivalent natural-language trigger) to load and execute the skill workflow.
