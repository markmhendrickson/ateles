/**
 * WHERE ONE TASK SITS — its lifecycle position, and which workflow it belongs to.
 *
 * The operator's question, 2026-09-02: "I want to see what workflows relate to
 * a given task on the task page in the app, and also where the task is along
 * the lifecycle."
 *
 * Those are two questions with very different answers, and this module keeps
 * them apart rather than presenting them as one feature at one confidence.
 *
 *   LIFECYCLE POSITION is answerable today. `task.status` places the task on
 *   the state machine in `lib/daemon_runtime/task_lifecycle.py`, which the
 *   Lifecycle tab already transcribes. See `placeOnLifecycle`.
 *
 *   WORKFLOW LINKAGE is mostly NOT STORED, and that is the finding rather than
 *   a gap to paper over. See `WORKFLOW_LINKAGE_FACTS` and `resolveWorkflowLink`.
 *
 * THE RULE THAT SHAPES BOTH: never infer a relationship to make the page look
 * complete. A workflow guessed from routing heuristics would render a confident
 * answer that nothing durably backs. Where the data is absent this module
 * returns an explicit "absent" variant carrying the REASON, and the view states
 * it in words.
 */

import { PATH_ORDER, STAGES, type Stage } from "./lifecycleData";

/* ------------------------------------------------------------------ *
 * PART 1 — LIFECYCLE POSITION
 * ------------------------------------------------------------------ */

/**
 * Where a task's stored `status` puts it on the lifecycle.
 *
 * `off_vocabulary` is NOT an error case and is not rare — it is the MAJORITY.
 * Measured against prod on 2026-09-02: of 21,285 task entities, only 4,034
 * carry one of the eleven declared statuses. The other 17,251 (81%) carry
 * values the state machine never declared — `completed`, `open`, `todo`,
 * `in_progress`, `canceled`, `queued`, `awaiting_release_confirmation`, and
 * null — mostly from imported Asana tasks.
 *
 * So the view MUST render those honestly: show the raw status, mark it as
 * outside the lifecycle, and decline to place it. Guessing a position (mapping
 * `completed` onto `done`, say) would fabricate a lifecycle history the task
 * never had, and would hide the very inconsistency that task
 * ent_46362b73e436ab7838705c2c exists to reconcile.
 */
export type LifecyclePosition =
  | {
      kind: "on_lifecycle";
      stage: Stage;
      /** Index into PATH_ORDER, or null for holds and exits (not on the path). */
      pathIndex: number | null;
      /** Legal successors from `_TRANSITIONS`, as declared. */
      next: string[];
    }
  | {
      kind: "off_vocabulary";
      /** The status as actually stored, shown verbatim. */
      raw: string;
      /**
       * Why the transition graph cannot govern this task.
       *
       * `is_valid_transition` FAILS OPEN on an unknown origin: an origin state
       * absent from `_TRANSITIONS` returns True, so a task sitting at `todo`
       * can move anywhere, unchecked. An ungoverned state is exactly what a
       * lifecycle view should make visible, so it is surfaced rather than
       * merely noted in a docstring.
       */
      ungoverned: true;
    }
  | { kind: "no_status" };

/** The eleven declared statuses, keyed for lookup. */
const BY_KEY = new Map(STAGES.map((s) => [s.key, s]));

export function placeOnLifecycle(status: unknown): LifecyclePosition {
  const raw = typeof status === "string" ? status.trim() : "";
  if (!raw) return { kind: "no_status" };

  const stage = BY_KEY.get(raw);
  if (!stage) return { kind: "off_vocabulary", raw, ungoverned: true };

  const idx = (PATH_ORDER as readonly string[]).indexOf(raw);
  return {
    kind: "on_lifecycle",
    stage,
    pathIndex: idx >= 0 ? idx : null,
    next: stage.next,
  };
}

/* ------------------------------------------------------------------ *
 * PART 2 — WORKFLOW LINKAGE
 * ------------------------------------------------------------------ */

/**
 * WHAT WAS MEASURED, so the view can state the gap with a number behind it.
 *
 * Every figure here was counted against Neotoma prod on 2026-09-02 while
 * building this view. They are held as data so the UI renders them uniformly
 * and a future reader can re-verify each one.
 */
export const WORKFLOW_LINKAGE_FACTS = {
  measuredOn: "2026-09-02",
  /** participation_record is the per-gate instance type — keyed (work_entity_id, gate_name). */
  participationRecords: 136,
  distinctWorkEntities: 135,
  /**
   * THE HEADLINE. Not one participation_record points at a task: they are 132
   * `issue` entities and 3 `pull_request` entities. The type that would answer
   * "which gate is this task at" does not address tasks at all — by
   * construction, not by omission.
   */
  workEntitiesThatAreTasks: 0,
  workEntitiesThatAreIssues: 132,
  workEntitiesThatArePullRequests: 3,
  /**
   * The known two-writer split, re-confirmed. participation_record status is
   * `dispatched` on 135 of 136 and `satisfied` on exactly 1, while `issue`
   * entities carry an INDEPENDENT `gate_status` map. Two engines write gate
   * state and neither reads the other's, so any UI showing gate state must
   * show BOTH sources and their disagreement rather than picking one.
   */
  participationDispatched: 135,
  participationSatisfied: 1,
  /** Sampled: 67 of 400 issue entities carry a `gate_status` map. */
  issuesWithGateStatusSampled: 67,
  issuesSampled: 400,
  /** No task in a 40-task sample had ANY edge to an issue. */
  taskToIssueRelationshipsFound: 0,
  taskRelationshipSampleSize: 40,
  /**
   * The one real bridge, and why it currently yields nothing. Of 10 sampled
   * tasks carrying a github `source_url`, 0 resolved to an existing issue
   * entity. This is NOT a lookup bug — the resolver was proven working by
   * resolving neotoma#2266 to ent_d47986c982259c8a37f8bf92, which carries a
   * full gate_status map. 4,415 issue entities exist. The specific issues
   * these tasks point at were simply never stored.
   */
  sourceUrlsSampled: 10,
  sourceUrlsResolved: 0,
  issueEntitiesTotal: 4415,
} as const;

/** A parsed `owner/repo#number` reference. */
export interface IssueRef {
  repo: string;
  number: number;
  url: string;
  /** True when the URL pointed at a pull request rather than an issue. */
  isPullRequest: boolean;
}

/**
 * Parse a task's stored `source_url` into a repo + number.
 *
 * THIS IS STORED DATA, NOT AN INFERENCE — which is the whole reason it is
 * legitimate to render. Issue-derived tasks carry a real `source_url` field
 * (e.g. `https://github.com/markmhendrickson/neotoma/issues/410`) written at
 * creation. Reading it is not guessing at a relationship; it is displaying one
 * the task already asserts.
 *
 * Returns null for anything that is not a github issue/PR URL, rather than
 * attempting to salvage a reference from arbitrary text.
 */
export function parseIssueRef(sourceUrl: unknown): IssueRef | null {
  if (typeof sourceUrl !== "string") return null;
  const m = /github\.com\/([^/\s]+\/[^/\s]+)\/(issues|pull)\/(\d+)/.exec(sourceUrl);
  if (!m) return null;
  const number = Number(m[3]);
  if (!Number.isInteger(number) || number <= 0) return null;
  return {
    repo: m[1],
    number,
    url: sourceUrl,
    isPullRequest: m[2] === "pull",
  };
}

/** Per-gate state as stored on an `issue` entity's `gate_status` map. */
export interface GateState {
  gateName: string;
  /** As stored: `pending`, `passed`, `not_required`, … Rendered verbatim. */
  status: string;
}

/**
 * What the app could establish about a task's workflow.
 *
 * The variants are ordered by how much is actually known, and each absent case
 * names WHY rather than collapsing into one empty state — "this task never
 * referenced an issue" and "this task references an issue that was never
 * stored" are different findings with different fixes.
 */
export type WorkflowLink =
  /** No `source_url`, no issue reference — nothing links this task to a workflow. */
  | { kind: "none" }
  /** The task names an issue, but no issue entity exists for it. */
  | { kind: "dangling"; ref: IssueRef }
  /** The issue entity exists but declares no gates. */
  | { kind: "issue_without_gates"; ref: IssueRef; issueId: string }
  /** Fully resolved: an issue with gate state. */
  | {
      kind: "resolved";
      ref: IssueRef;
      issueId: string;
      workflowType: string | null;
      currentOwner: string | null;
      /** From the issue's own `gate_status` map — one of the two writers. */
      gates: GateState[];
      /**
       * From `participation_record` entities keyed on this work entity — the
       * OTHER writer. Held separately and NEVER merged: the two disagree
       * systematically (135 `dispatched` vs 1 `satisfied` across the whole
       * graph), and picking one would hide that.
       */
      participation: GateState[];
    };

/** Shape of `/api/task-workflow`'s response. */
export interface WorkflowLinkPayload {
  link?: WorkflowLink;
  error?: string;
}

/**
 * Do the two gate-state sources disagree about any gate they share?
 *
 * Returns the gate names where both sources have an opinion and the opinions
 * differ. The view shows these explicitly, because a page that displayed only
 * one source would present a settled answer where the graph holds two.
 */
export function gateDisagreements(link: WorkflowLink): string[] {
  if (link.kind !== "resolved") return [];
  const byName = new Map(link.participation.map((g) => [g.gateName, g.status]));
  return link.gates
    .filter((g) => byName.has(g.gateName) && byName.get(g.gateName) !== g.status)
    .map((g) => g.gateName);
}
