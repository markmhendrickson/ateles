# Operator rules a session actually reads

## Scope

What a session sees when rules 1, 2, and 6 bind or do not, and how an open decision is posed.

Out of scope: the `standing_rule` schema, and the six rule bodies.

## Tokens

| Token | When |
|---|---|
| `[rules-unbound]` | Rules 1, 2, and/or 6 did not resolve. `missing=` lists the numbers. |
| `[rules-incomplete]` | A related entity loaded, but one of 1, 2, or 6 has no text. |
| `[decisions-unposed]` | Open decisions exist and the harness questions tool is unavailable. |
| `[skill-sync]` | A skill mirror is missing or drifted. Not a rule missing inside a loaded skill. |
| `skill_rule_missing` | A skill loaded, and rule 3, 4, or 5 is not in the returned body. |

`[rules-unbound] missing=1,2,6 hint=resolve the related entity on the agent; do not paste rule text into prompt_markdown or CLAUDE.md — docs/operator_rules.md`

## Decisions

Zero decisions produce no questions-tool call.

When two decisions are open, one questions-tool call carries both. Each item has labeled options, not a numbered prose list:

- Ship on this branch — the review already names the resolver; waiting opens a second pull request for the same defect.
- Hold — the session keeps printing the unbound line until the related entities exist.

Say nothing when there is nothing to decide.
