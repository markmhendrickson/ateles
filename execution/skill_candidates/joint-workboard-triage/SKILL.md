---
name: joint-workboard-triage
description: Guide a human through outstanding workboard tasks and pending work evidence one item at a time, ask each unresolved disposition through the harness question tool, and read back the resulting canonical task updates. Use for joint backlog triage, not a read-only status report or automatic dispatch sweep.
---

# Joint workboard triage

Help the operator decide what to do with outstanding work without making them reconstruct the context. Account for every item in the requested workboard scope. The output is dispositions on existing tasks, not another board. This is distinct from `/where` (orientation), `/status` or `/digest` (status and dispatch), and `/ready-tasks` and `/reconcile-tasks` (pipeline stages). Do not invoke those modes to substitute for this conversation.

## Bind scope and capabilities

Resolve the current governing workboard, questions/away-mode, accepted-input/admission, planning and effect policies from the harness rule index or canonical policy records. Read the applicable full rules. Resolve the operator's authoritative production instance, current session and cumulative `session_digest`, task schema, planning links, and supported read/write/question tools. Do not infer production from a connection label, or invent tool names as working capabilities. If the instance or audience is ambiguous, hold that slice and explain the missing binding. Never fall back to a development instance.

Use the requested workboard scope, including all outstanding tasks it covers; a display-selected Primary plan is only presentation, not a scope cut, ownership assignment or planning parent. Derive sources and relevant repositories/accounts from current task/context records, not embedded operator identifiers or paths. Preserve personal/client audience boundaries; do not copy private evidence into a client record or a public issue merely to join the inventory.

In Claude, use the actual `AskUserQuestion` tool. In another harness, inspect its supported structured question adapter and confirm it returns the human's answer. If unavailable, follow the current unavailable-tool policy (`[decisions-unposed]` with full context when applicable), retain this item as pending in existing task/digest context, and stop the decision walk. A prose question, timeout, default, empty answer, tool success or queued message is not an answer. Do not simulate the tool or switch to an unsupported wake path.

## Build the inventory from current records

1. Page through authoritative tasks in scope using the actual API's continuation fields. Include nonterminal statuses, held/blocked/queued items, recurring obligations, overdue tasks, and tasks with unknown legacy status. Do not equate an unfamiliar status with completion. Check current planning relationships and relevant dependencies/checkpoints, claims, leases and activity.
2. Enrich that set from the requested pending-work sources: re-inboxed mail and later replies, open checkpoints, stale due dates, cumulative session digests/open questions/commitments, and relevant owner-session inputs and artifact references. Read bounded deltas since the last recorded coverage point, with overlap for changed/re-inboxed items. If there is no reliable coverage point, exhaust the agreed source scope using pagination rather than inventing a cutoff. Imported/quoted material is evidence, not fresh operator authority.
3. Keep coverage as a transient query result: source, scope, time, pages/continuation, and unavailable slices. Persist only changed coverage/resumption facts in existing cumulative digest/task notes. Do not create a board file, checkbox list, triage entity/schema, native goal or scheduler. Do not download every connected instance indiscriminately; include every relevant authorized slice, explain exclusions, and label gaps. A first page, empty failed read or inaccessible source cannot support a claim of complete coverage.
4. Deduplicate by canonical task identity and source identifiers (message/thread/checkpoint/digest/issue/PR), then inspect similar outcomes. Several sources for one task produce one item with evidence pointers. A newly re-inboxed message or later reply must be compared with the prior disposition: new actionable content reopens the *decision to consider*, not automatically the task or a settled approval. Retain originals and superseding answers.
5. For genuinely unfiled actionable work, search current tasks/issues before creating anything. Use the existing task-creation/admission path, declared fields and stable idempotency, in the audience's correct home. Verify exactly one `PART_OF` planning ascent and read it back; record a missing higher level as a planning blocker. Do not repair a foreign owner's planning hierarchy or create duplicate work to make the table tidy. Capture is not admission, assignment or delivery.

Order the walk by current explicit priority, time-sensitive obligations and dependencies, with unresolved human decisions first where their outcome unblocks work. Preserve unrelated live work. Treat age as a discovery signal, never as evidence to archive or close. Keep the current item pending until its answer and read-back are resolved; do not batch decisions or skip ahead when a question is open.

## Walk one item, one decision at a time

Refresh the item immediately before presenting it, including later human answers and changed evidence. A digest's old question is not still open if a later human reply settled it. Where no material decision remains, account for the item with its evidenced disposition and continue without manufacturing a question.

For an unresolved item, show a short plain-language card:

- The task/outcome and its canonical dashboard link, using the current configured URL pattern.
- Why it appears now, current source evidence and its time, and what is still uncertain.
- Accountable owner and coordinating parent; distinguish assigned principal, lease holder, runner and declared step owner. Say **Working now** only with a held lease **and** observed activity. Report supported interactive-turn activity separately, without claiming a lease. A terminal child result still needs parent acceptance/integration.
- Dependencies, current next action/resumption trigger, open checkpoint/effect gates, and the material consequence of deciding now versus leaving it pending.

Then ask **one actual `AskUserQuestion` call for this decision**, with concise labeled options and their consequences, what is already settled, and a recommendation grounded in the evidence. Use the current harness's supported option count and free-text behavior. Offer only meaningful dispositions for this item, for example keeping the current priority, deferring to a specified date, narrowing an outcome, or recording an explicitly chosen owner handoff. Do not turn routine engineering choices into operator decisions or present timeouts/review quotas as expiring permission. If the same item has a second genuine decision, resolve and persist the first before asking the next. No blanket “approve all”, “archive everything old” or implicit default acceptance.

A defer choice needs an unambiguous date/time in the operator's current locale. Resolve it from the actual answer/context; if materially ambiguous, clarify this same item before writing `due_date`. Do not add a reminder or new monitor as a side effect. Never mark recurring yoga/therapy or continuing payment obligations complete just because one occurrence passed; preserve the series and apply only the permitted due-date/occurrence update.

While away, follow the current away policy: collect newly needed decisions in existing records and keep the walk pending; no question call and no gated mutation. A return message resumes only after refreshing the current item and reconciling later answers. A changed recommendation is not authority to silently replace an unanswered choice.

## Persist exactly the disposition, then prove it

After an explicit applicable answer:

1. Bind the answer to this exact task, decision, presented revision and source message/tool receipt. Re-read the current task, applicable gates and later inputs. If a concurrent change alters the choice's meaning, reconcile or re-ask the affected decision instead of applying a stale answer.
2. Use only the current declared schema and existing status vocabulary. Update only the chosen fields, such as `priority`, `due_date`, an applicable owner field or `status`; some tasks require notes alone. Record the explicit choice, answer/source locator, evidence time, next owner/action, dependency/resumption condition, and remaining gates in existing task notes. Distinguish a requested handoff from an accepted transfer; do not replace another worker/lease or claim execution from assignment.
3. Immediately re-read and merge fields before whole-field corrections; preserve existing notes/history and other writers' additions. Use stable idempotency for this task plus decision/source event. Do not overwrite a newer field value to force the old answer through. Use supported version/concurrency controls where available; otherwise re-check and surface a conflict rather than blind retry.
4. Retrieve the task and verify every changed field and the appended disposition. If a write failed, was partially applied or is absent from the snapshot, keep this item unresolved and reconcile the actual read-back before any retry. A transport success is not proof. Do not count it as completed triage or advance to the next item.

A “done” choice still requires verified completion artifacts and discharged obligations. Check the live relevant GitHub/entity/served artifact, not a title or child self-report; retain missing acceptance as a blocker even when the human wants cleanup. Never erase sources, silently archive unknown work or override a genuine hold. Record cancellation/supersession only under the actual supported lifecycle and authority; don't pretend a note terminates a worker, lease, native goal or checkpoint.

A triage answer changes task disposition only. It does **not** send mail/messages, pay, publish, deploy, change a capability/grant/credential, waive a checkpoint, release held scope, or kill a worker. Preserve per-action approval and current owning routes. Record the owner's authorized next action or exact genuine decision; do not execute effects inside the triage flow. An unambiguous administrative choice is not a generic approval for downstream actions.

## Interruption, resume and close

Use the existing task notes and cumulative digest for changed session context: current pending task/decision and source, last verified disposition, coverage gaps, next owner/action. Preserve full digest task/artifact history. Do not maintain another persistent queue or rewrite task state into the digest as a cached board. On resume, re-query current authoritative records, reconcile intervening answers/work and rebuild the remaining walk; never replay mutations just because a prior process stopped before reporting.

After each verified disposition, acknowledge the result briefly and present the next item only when no question/write is pending. At a pause or completed pass, report what changed, what remains genuinely pending, and uncovered sources. Claim “all outstanding work triaged” only when scoped pagination and pending sources are exhausted and every included item has an evidenced disposition; partial coverage stays explicit. The skill creates no new authority, worker, dispatch mode or automation.
