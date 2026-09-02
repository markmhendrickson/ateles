/**
 * IS THE QUESTION QUEUE THE WHOLE QUEUE?
 * --------------------------------------
 * The open-questions rail has no query of its own. It filters the task page's
 * `limit=200` newest-first response for `category: "open_question"` and prints
 * what survives as "N awaiting you", plus a counter badge that is the ONLY
 * thing visible when the rail is collapsed.
 *
 * Every one of those numbers describes the newest 200 tasks. It is right today
 * because all 11 question entities were filed recently — a property of the
 * data, not of the code. The failure it sets up is specific and bad:
 *
 *   - the tasks page is `last_observation_at desc`, so the FIRST question to
 *     fall out of the window is the one that has gone longest unanswered;
 *   - a question outside the window is not merely uncounted, it is unreachable
 *     — the rail is the only surface that lists questions;
 *   - a collapsed rail with no badge is indistinguishable from an empty queue,
 *     so the disappearance produces no signal at all.
 *
 * A queue whose whole purpose is that nothing gets dropped must not be able to
 * drop things quietly. `/api/questions` answers "how many exist" independently
 * of the page, and this module reconciles that against what the rail holds.
 *
 * WHY NOT REUSE `Coverage` FROM `listTotal.ts`: that models rows against a
 * total for ONE query. Here the count and the rows come from two different
 * queries, and the interesting quantity is the DISCREPANCY between them —
 * questions that exist but are not on this page. Same honesty rule, different
 * arithmetic, so it gets its own small type rather than bending the other.
 */
import type { Task } from "./tasks";
import { isAnswered } from "./tasks";

/** What `/api/questions` reported, or that it could not be read. */
export type QuestionCoverage =
  | { kind: "measured"; total: unknown; done: unknown }
  | { kind: "unknown" };

/** What the rail may state about its own completeness. */
export interface QuestionTally {
  /** Unanswered questions ON THIS PAGE — always safe to show; they are in hand. */
  loadedOpen: number;
  /**
   * Unanswered questions that EXIST, or null when that could not be measured.
   * Null is not zero and must never render as a figure.
   */
  totalOpen: number | null;
  /**
   * Unanswered questions that exist but are NOT on this page.
   *
   * Zero in the healthy case. Positive means the rail is showing a subset and
   * has to say so. Null when the total is unmeasured — the shortfall is then
   * unknown rather than absent, and claiming either would be a guess.
   */
  missing: number | null;
}

function finite(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/**
 * Reconcile the server's question counts against the loaded rail.
 *
 * `totalOpen` is derived as total-minus-done rather than queried directly,
 * because "open" for a question is not a single stored status: `isAnswered()`
 * treats a recorded answer OR a closed status as answered, and no server-side
 * filter expresses that. Subtraction over the two figures the server CAN count
 * exactly is the closest honest approximation, and it is used only for the
 * shortfall — never to overrule the rows in hand.
 *
 * The shortfall is clamped at zero. A negative would mean the page holds more
 * open questions than exist, which can happen transiently: the two requests
 * are independent, so a question answered between them shows in one and not
 * the other. Printing "-1 not shown" from a race would discredit a warning
 * that is right the rest of the time.
 */
export function questionCoverage(coverage: QuestionCoverage, loaded: Task[]): QuestionTally {
  const loadedOpen = loaded.filter((q) => !isAnswered(q)).length;

  if (coverage.kind === "unknown") {
    return { loadedOpen, totalOpen: null, missing: null };
  }

  const total = finite(coverage.total);
  const done = finite(coverage.done);
  if (total === null || done === null) {
    return { loadedOpen, totalOpen: null, missing: null };
  }

  const totalOpen = Math.max(0, total - done);
  return { loadedOpen, totalOpen, missing: Math.max(0, totalOpen - loadedOpen) };
}
