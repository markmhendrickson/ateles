# Task `action_type` vocabulary

Task entities carry `action_type` and `confidence` (0..1) so Apis can classify
blast radius and apply the execution gate without inferring from the assigned
agent alone.

Values **must** match the active `execution_policy`'s
`high_blast_action_types` / `low_blast_action_types` (default:
`ent_dfce6edecefe3eb7fc9e0337`). Unknown types fall through to the policy's
`blast_radius_default` (low on the default policy).

## Low blast (typical read-only / local work)

Set these when the task does not mutate shared git, send external comms, pay, or
publish. Pair with `confidence` ≥ the policy threshold (default 0.85) when the
task should auto-dispatch.

| `action_type` | Use when |
|---|---|
| `compute_only_analysis` | Read-only analysis, spec authoring, code review without PR |
| `local_edit` | Local file edits that do not push or open a PR |
| `draft` | Draft content not yet posted externally |
| `neotoma_internal_entity_update` | Neotoma graph writes with no external side effect |

## High blast (operator checkpoint expected)

Set these explicitly when the task will perform the named outward action.
`confidence` below threshold (default 0.85) correctly checkpoints.

| `action_type` | Use when |
|---|---|
| `open_or_merge_pr` | Open or merge a pull request |
| `git_push` | Push commits to a remote |
| `payment` / `transfer` / `wage` / `invoice_pay` | Financial execution |
| `send_external_comms` | Email, social post, or other external send |
| `publish` / `release` | Publish docs, npm package, or release |
| `delete_entity_or_data` | Irreversible deletion |
| `external_api_write` | Mutating call to an external API |

## Generalist default

When `action_type` is omitted, **specialist** agents infer their typical maximum
(`monedula` → `payment`, etc.). **Generalist** agents (`cicada`) infer
`compute_only_analysis` — escalate to a high-blast type only when the task
genuinely requires it (e.g. `open_or_merge_pr` for implementation work).

## `confidence`

Agent self-score per the confidence rubric (`ent_22fd6f25159f1f2689726780`).
Absent → 0.0 (fail closed). Task creators should set this at filing time.
