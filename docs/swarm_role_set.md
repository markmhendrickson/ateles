# The reference implementation's role set

## Purpose

Record the role set the Ateles swarm uses, so the information the foundation no longer carries is kept in the place implementation state belongs.

## Scope

This file covers the reference implementation's role names and what each claims. It does not define the design's constraints on roles; those are the foundation's.

## The role set

This is the implementation's role set: the slot names the Ateles swarm uses in its workflow declarations, and
what each slot claims. It is **not** foundation. The foundation defines no fixed list of roles (operator ruling,
2026-10-13, register decision 142): a step declaration's `owner_role` is a slot each implementation fills from its
own role set, and the only things the foundation fixes are the constraints on filling the slots
([`foundation/workflows.md#roles-are-slots-and-the-design-fixes-only-the-constraints-on-filling-them`](foundation/workflows.md#roles-are-slots-and-the-design-fixes-only-the-constraints-on-filling-them)).
A fork may rename, merge, or split any of these roles, so long as those constraints hold.

The table below is the table that stood in `docs/foundation/workflows.md` under "Roles named in this document"
before that ruling, moved here unchanged in content. Which agent fills each role for a given declaration scope
is `swarm_roster` on the record, never this file; the agent definitions are in [`agents/`](agents/README.md).

| Role | Claims |
|---|---|
| `pm` step owner | the `pm` step of every code workflow and every step of intake |
| `ux`, `arch`, `pr_review`, `qa`, `legal` step owners | the review step of the same name (`foundation/vocabulary.md#review-step`) |
| implementer | the `impl` step |
| steward | the `merge` step, whose work is the merge action (`foundation/vocabulary.md#steward`) |
| release steward | every step of the release workflow |
| copywriter | the `copy` step |
| content author | the drafting and posting steps of social content and outreach |
| lint runner | a deterministic step whose work is a script and whose verdict is the script's result |
| operator-facing agent | every step that carries a checkpoint or a task to the operator (`foundation/vocabulary.md#operator-facing-agent`) |
| payer, verifier | the two disjoint roles of the payment workflow |
| researcher, analyst | the working steps of research and of meeting processing |
| planner | every step of the planning workflow, declared per declaration scope and once for the levels above a project (`foundation/workflows.md#planning`) |

## How this set meets the foundation's constraints

- **Separation of duties:** `payer` and `verifier` are distinct roles that must resolve to distinct principals;
  every checkpoint's raiser and resolver are distinct, save the operator's marked self-resolution.
- **A review step is signed by its own step owner:** the `ux`, `arch`, `pr_review`, `qa`, and `legal` step owners
  sign their own steps; an automated account's approval is an observation, never a verdict.
- **`operator_only`:** the operator-facing agent carries such work to the operator; the operator takes the action.
- **Deterministic steps:** the lint runner runs a script and no model.
- **Every slot resolves:** each role above must resolve through `swarm_roster` to an agent with a credential
  before a declaration naming it is made.
