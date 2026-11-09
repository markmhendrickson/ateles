# `standing_rule` PII audit — 2026-09-18

Audit for ateles#1100 / PR #1098. Records ids and payload categories only —
never the payload values themselves (per #1100's ids-and-categories-only
rule).

## Scope and count reconciliation

`retrieve_entities` (`entity_type: standing_rule`) was paged to exhaustion.
Its own `total` field reports 53 on every call, including a call at
`offset: 48` that returns zero rows, and `include_merged: true` still returns
exactly 48 unique entities with no `merged_to_entity_id` set on any of them.
Cross-checked against `get_entity_type_counts`, which independently reports
`standing_rule: 48`.

Two independent instruments (`get_entity_type_counts`, exhaustive paged
retrieval) agree on **48** live entities; `retrieve_entities.total` (53) is
the outlier. Audited all 48 that actually exist. This discrepancy is a
Neotoma-side instrument defect, out of scope for this PR — not reconciled
here beyond flagging it (see "validate the instrument before believing the
measurement", CLAUDE.md).

## Classification

- **CLEAN** — 37 entities. Generic instructions, safe to render anywhere.
- **MIXED, split in this PR** — 7 entities. A generic rule with operator
  specifics inlined, where a runtime consumer already exists to resolve the
  specifics once split out.
- **NEEDS A CONSUMER FIRST — left intact** — 2 entities. The generic
  instruction cannot be separated from the specifics without breaking
  behavior, because no consumer resolves the would-be context entity.

## Split entities (7)

| Entity id | Payload category | Consumer verified |
|---|---|---|
| `ent_1a13dc4154f8f3e4a8005131` | crypto_address, amount, contact_field (first name) | `execution/daemons/monedula/handlers/btc_transfer.py` (already payment_profile-driven) |
| `ent_c49f086bb79a9198a37478ce` | contact_field (first name), amount | same |
| `ent_e1cabc46354555b419472dce` | payment_profile_field (vendor business name) | same |
| `ent_228599d38a75f195bc4e35f8` | contact_field (vendor first name) | same |
| `ent_6c6e4147e73f7db5430715fa` | payment_profile_field (vendor business name) | same |
| `ent_a23201e13dc57002084aa3ff` | location_field (gym/location name) | `operator_profile:default` (`default_gym` field, already populated); `.claude/skills/gorilla/SKILL.md` |
| `ent_0235f006414d9d63762733e0` | contact_field (third-party first+last name, in `content` provenance field only, not the operative `instruction`) | n/a — citation field, not runtime-resolved |

Each split was verified by reading the corrected `standing_rule` entity back
immediately after the `correct()` call, confirming the specific value is gone
and a generic instruction plus a context-entity reference (by id or field
name, never by inlined value) is present.

## Deferred — no consumer exists (2)

- `ent_30c1ab298382e22f35fb9b35` — category: health/regimen field (brand
  names). The entire `rule` field IS the specific regimen; there is no
  separable generic instruction, and no consumer resolves anything like it.
  Splitting would leave an empty shell.
- `ent_4bde5596a479e3e69cddb883` — category: financial-accounts registry
  (counts + a private file path). The `rule` field is entirely specific
  counts and a private path fragment with jurisdiction-specific filing
  category codes; same "no generic instruction exists" problem.

Both are left intact and reported here rather than silently split — a real
gap, deferred pending a consumer.

## Gate findings during testing

Running the new gate (`scripts/linters/check_agent_mirror_pii.py`) against
the live repo surfaced one **pre-existing, out-of-scope** finding: a
crypto_address in `.claude/skills/loop-start/SKILL.md`, filed separately as
ateles#1099. Not part of this audit's 48 entities; left for that issue to
adjudicate.

## Out of scope (per #1100)

- The `retrieve_entities.total` overcount — a Neotoma-side instrument defect.
- Remediating the pre-existing `loop-start/SKILL.md` finding (#1099).
- Creating consumers or context entities so the deferred entities can be
  split later.
- Narrowing the `.gitleaks.toml` allowlist of `.claude/`.
