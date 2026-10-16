/**
 * REFERENCE NUMBERS — "answer question 3"
 * ---------------------------------------
 * The operator works by voice. A question needs a short spoken handle, and an
 * entity id is unsayable, so each question gets a small ordinal shown on the
 * card and on the detail view.
 *
 * WHAT WAS ACTUALLY WRONG (measured, not guessed)
 * -----------------------------------------------
 * The stored numbers had not regressed. Of the eight live questions, five carry
 * a `task_id` (1–5) and render their number correctly. The four most recent
 * ones carry NO `task_id` at all, so they rendered with no number — which is
 * what "we lost the numbers" looks like from the outside. The bug is missing
 * DATA on new questions, not broken rendering of old ones.
 *
 * This app cannot fix that by writing: it is read-only by construction, and
 * assigning `task_id` here would be exactly the fabricated-write the sidebar's
 * header comment forbids. So a number is DERIVED for those questions instead,
 * and the derivation has to be stable or it is worse than useless — a number
 * that moves while the operator is mid-sentence sends his answer to the wrong
 * question.
 *
 * HOW STABILITY IS ACHIEVED
 * -------------------------
 * A stored `task_id` always wins. It is authoritative and never recomputed.
 *
 * For the rest, the ordinal is derived from CREATION ORDER — `computedAt`,
 * ascending, tie-broken by entity id — and numbering starts above the highest
 * stored number in play. Creation order is a fact about the past: it does not
 * change when a question is answered, when priority is edited, or when the
 * sidebar re-sorts. That is the whole reason it is used rather than list
 * position, which the panel reorders on every poll.
 *
 * The consequences, stated plainly because they are the interesting part:
 *
 *   - A NEW question always takes the NEXT number. It sorts last by creation
 *     time, so it appends and never renumbers anything the operator has
 *     already spoken about.
 *   - An ANSWERED question KEEPS its number. Answered questions stay in the
 *     numbering (they merely move to their own group in the sidebar), so the
 *     numbers behind them never shift up. "Question 3" means the same question
 *     tomorrow, answered or not.
 *   - A DELETED question is the one case that DOES shift numbers, and only
 *     among the derived ones above it: with nothing stored to anchor them,
 *     removing a question from the middle of the derived run slides the rest
 *     down by one. Verified, not assumed — deleting the 7th of eight renumbers
 *     the 8th to 7.
 *
 *     This is a real limit rather than an accepted trade-off, and it is why the
 *     fix belongs at the WRITE side: once a question carries a stored
 *     `task_id`, its number is authoritative here and nothing can move it.
 *     Questions are rarely deleted, and the alternative — persisting a derived
 *     number locally — would invent an identifier this read-only app has no
 *     business minting.
 *
 * A derived number is marked as such (`stored: false`) so the UI can show it
 * differently: it is a display aid this app computed, not a value Neotoma
 * holds, and the two should not be indistinguishable to someone reading the
 * entity in the Inspector.
 */
import type { Task } from "./tasks";

export interface QuestionRef {
  /** The number to say out loud. */
  n: number;
  /** True when Neotoma stores it (`task_id`); false when derived here. */
  stored: boolean;
}

/**
 * Assign every question a stable reference number, keyed by entity id.
 *
 * Pure and deterministic: the same set of questions always yields the same
 * mapping, so a poll that returns identical data cannot move a number.
 */
export function questionRefs(questions: Task[]): Map<string, QuestionRef> {
  const refs = new Map<string, QuestionRef>();

  // Stored numbers are authoritative. Collect them first so derivation can
  // start above them and never collide with one.
  let highest = 0;
  for (const q of questions) {
    if (q.ref !== null) {
      refs.set(q.id, { n: q.ref, stored: true });
      highest = Math.max(highest, q.ref);
    }
  }

  // Creation order — stable across sorting, answering, and re-polling. The id
  // tie-break keeps two questions computed in the same millisecond from
  // swapping places between renders.
  const underived = questions
    .filter((q) => q.ref === null)
    .sort(
      (a, b) =>
        (a.computedAt?.getTime() ?? 0) - (b.computedAt?.getTime() ?? 0) ||
        a.id.localeCompare(b.id),
    );

  let next = highest + 1;
  for (const q of underived) {
    refs.set(q.id, { n: next++, stored: false });
  }

  return refs;
}
