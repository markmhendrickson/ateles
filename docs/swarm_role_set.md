# The reference implementation's role set

## Purpose

Record the role set the Ateles swarm uses, so the information the foundation no longer carries is kept in the place implementation state belongs.

## Scope

This file covers the reference implementation's role names and what each claims. It does not define the design's constraints on roles; those are the foundation's.

## The role set

This is the implementation's role set: the slot names the Ateles swarm uses in its workflow declarations, and
what each slot claims. It is **not** foundation. The foundation defines no fixed list of roles (operator ruling,
2026-10-13, register decision 142): a step declaration's `owner_role` names a slot each implementation fills from its
own role set, and the only things the foundation fixes are the constraints on filling the slots
([`foundation/workflows.md#roles-are-slots-and-the-design-fixes-only-the-constraints-on-filling-them`](foundation/workflows.md#roles-are-slots-and-the-design-fixes-only-the-constraints-on-filling-them)).
A fork may rename, merge, or split any of these roles, so long as those constraints hold.

The table below is the table that stood in `docs/foundation/workflows.md` under "Roles named in this document" before
that ruling, moved here, with the release steward's row split in two by decision 143. This file is a hand-kept
reference copy and is descriptive: the `swarm_roster` and `workflow` declaration entities on the record govern, and
where this file differs from them, they are right and this file is stale. Which agent fills each role for a given
declaration scope is `swarm_roster` on the record, never this file; the agent definitions are in
[`agents/`](agents/README.md).

| Role | Claims |
|---|---|
| `pm` step owner | the `pm` step of every code workflow and every step of intake |
| `ux`, `arch`, `pr_review`, `qa`, `legal` step owners | the review step of the same name (`foundation/vocabulary.md#review-step`) |
| implementer | the `impl` step |
| steward | the `merge` step, whose work is the merge action (`foundation/vocabulary.md#steward`) |
| release steward | the `criteria` and `release` steps of the release workflow |
| deployment verifier | the `verify_deployed` step of the release workflow, a role distinct from the release steward's |
| copywriter | the `copy` step |
| content author | the drafting and posting steps of social content and outreach |
| lint runner | a deterministic step whose work is a script and whose verdict is the script's result |
| operator-facing agent | every step that carries a checkpoint or a task to the operator (`foundation/vocabulary.md#operator-facing-agent`) |
| payer, verifier | the two disjoint roles of the payment workflow |
| researcher, analyst | the working steps of research and of meeting processing |
| planner | every step of the planning workflow, declared per declaration scope and once for the levels above a project (`foundation/workflows.md#planning`) |

## How this set meets the foundation's constraints

- **Separation of duties:**
  - `payer` and `verifier` are distinct roles that must resolve to distinct principals.
  - Every checkpoint's raiser and resolver are distinct, save the operator's marked self-resolution.
  - The swarm's approval of a declaration is never its proposer's.
  - An agent never signs a review step on an artifact it built, whether the roster seats it in one slot or two.
    The review steps include `ux`, `arch`, `pr_review`, `qa`, `legal`, and the release workflow's `verify_deployed`;
    the `pm` step, the implementer, the steward, the lint runner's steps, and digestion's `verify` are not review steps
    ([`foundation/gates_and_workflows.md#the-builder-of-a-change-never-signs-its-review`](foundation/gates_and_workflows.md#the-builder-of-a-change-never-signs-its-review)).
    The check is on the agent itself, so two agents acting for the one operator may review each other's work.
- **A review step is signed by its own step owner, or waived by the operator:** the `ux`, `arch`, `pr_review`, `qa`, and `legal` step owners
  sign their own steps, and the deployment verifier signs `verify_deployed`; an approval from a credential not bound to the step owner is an observation, never a verdict.
- **`operator_only`:** the operator-facing agent carries such work, and every checkpoint awaiting the operator, to the operator; the operator takes the action.
- **Deterministic steps:** the lint runner is filled by a principal whose work is a script, and it is still an agent that resolves through `swarm_roster` like any other role; a blocking conclusion on its result names the mechanism and what it read.
- **Every slot resolves:** each role above must resolve through `swarm_roster` to an agent that can be run,
  or a declaration naming it raises an `unspawnable_assignee` checkpoint; the `pm` step owner (intake's step
  owners) and the operator-facing agent must exist.
