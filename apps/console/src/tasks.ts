/** Shape of the rows Neotoma returns from POST /entities/query. */
export interface TaskEntity {
  entity_id: string;
  canonical_name?: string;
  snapshot?: Record<string, unknown> | null;
  last_observation_at?: string | null;
  computed_at?: string | null;
}

export interface Task {
  id: string;
  title: string;
  /** Raw status string as stored, e.g. "awaiting_input". Never normalized away. */
  status: string;
  /** Bucket the raw status maps into, for grouping and filtering. */
  bucket: Bucket;
  priority: string | null;
  assignedTo: string | null;
  updatedAt: Date | null;
  /** True when no agent owns this task: filed, but never dispatched. */
  undispatched: boolean;
  /**
   * True when this task is an open question addressed to the operator rather
   * than a unit of work. See `isQuestion` for how that is decided.
   */
  question: boolean;
  /** The situation the agent recorded alongside the question. */
  context: string | null;
  /** Supplementary detail recorded with the question. */
  description: string | null;
  /** The operator's answer, once given. Null while the question is open. */
  answer: string | null;
  /**
   * Stable human reference for a question ("#2"), so the operator can answer by
   * number out loud. Assigned once in Neotoma and never derived from list
   * position — reordering by priority, or a question becoming answered, must
   * not renumber anything the operator has already spoken about.
   */
  ref: number | null;
  /** The agent's recommended resolution, when one has been recorded. */
  recommendation: string | null;
  /**
   * Raw `category`. `"topic"` marks a workstream rather than a unit of work;
   * the Sessions view surfaces those separately. Note this is NOT a session
   * link — no task entity carries one. See the header of Sessions.tsx.
   */
  category: string | null;
  /**
   * When Neotoma last recomputed the entity's snapshot.
   *
   * NOTE: this is the closest thing to a creation time the query exposes —
   * `/entities/query` returns no `created_at`, and the task schema has no such
   * field, so the detail view labels this "First computed" rather than
   * "Created". Naming it "Created" would assert something the data does not
   * say: a snapshot can be recomputed long after the entity was filed.
   */
  computedAt: Date | null;
  /** The `updated_date` the author recorded on the task, when present. */
  updatedDate: string | null;
}

/** Parse an ISO timestamp, returning null for absent or unparseable values. */
function date(value: string | null | undefined): Date | null {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * QUESTION MARKER
 * ---------------
 * Open questions are ordinary `task` entities distinguished by the DECLARED
 * `category` field carrying `open_question`. `category` is part of the task
 * schema and was unused across production tasks, so it separates questions
 * from work without a naming convention that a retitle would silently break.
 *
 * The "QUESTION:" title prefix is honoured too, but only as a fallback for
 * anything filed before the field convention existed.
 */
export const QUESTION_CATEGORY = "open_question";

function isQuestion(snap: Record<string, unknown>, title: string): boolean {
  const category = str(snap.category)?.toLowerCase();
  if (category === QUESTION_CATEGORY) return true;
  return /^question:/i.test(title);
}

export const BUCKETS = ["in_progress", "pending", "blocked", "done", "other"] as const;
export type Bucket = (typeof BUCKETS)[number];

export const BUCKET_LABELS: Record<Bucket, string> = {
  in_progress: "In progress",
  pending: "Pending",
  blocked: "Blocked",
  done: "Done",
  other: "Other",
};

/**
 * Production uses more status values than the four canonical ones — `open`,
 * `todo`, `completed`, `awaiting_input`, `canceled`, `awaiting_release_confirmation`
 * all occur. Map them onto buckets so nothing is silently dropped from the view;
 * anything unrecognized lands in `other` rather than disappearing.
 */
export function toBucket(status: string): Bucket {
  switch (status) {
    case "in_progress":
    case "active":
      return "in_progress";
    case "pending":
    case "open":
    case "todo":
      return "pending";
    case "blocked":
    case "awaiting_input":
    case "awaiting_release_confirmation":
      return "blocked";
    case "done":
    case "completed":
      return "done";
    default:
      return "other";
  }
}

function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

/**
 * REFERENCE NUMBER
 * ----------------
 * Carried on the DECLARED `task_id` field, which is otherwise unused on these
 * question entities and is distinct from `entity_id`. The number is stored, not
 * computed, precisely so it is stable: the sidebar sorts by priority and moves
 * answered questions to their own group, and a position-derived number would
 * change under the operator mid-sentence.
 *
 * Neotoma coerces a numeric correction to a number and a quoted one to a
 * string, so accept both rather than depending on how it happened to be written.
 */
function refNumber(snap: Record<string, unknown>): number | null {
  const raw = snap.task_id;
  const n = typeof raw === "number" ? raw : Number(str(raw));
  return Number.isInteger(n) && n > 0 ? n : null;
}

/**
 * RECOMMENDATION
 * --------------
 * Carried on the DECLARED `details` field — the schema's home for longer
 * supplementary prose about a task. Deliberately NOT `result`: that field holds
 * the operator's own answer, and an agent's suggestion written there would read
 * downstream as a decision the operator had made.
 *
 * The stored text is prefixed "RECOMMENDATION:"; strip that, since the UI
 * already labels the block.
 */
function recommendation(snap: Record<string, unknown>): string | null {
  const raw = str(snap.details);
  return raw ? raw.replace(/^\s*RECOMMENDATION:\s*/i, "") || null : null;
}

/** Neotoma sometimes double-nests the snapshot; tolerate both shapes. */
function unwrap(row: TaskEntity): Record<string, unknown> {
  const snap = row.snapshot;
  if (snap && typeof snap === "object") {
    const inner = (snap as Record<string, unknown>).snapshot;
    if (inner && typeof inner === "object") return inner as Record<string, unknown>;
    return snap as Record<string, unknown>;
  }
  return {};
}

/**
 * Not every task carries a `title` — many have only a `description`, and their
 * `canonical_name` is a punctuation-stripped copy of that whole description
 * (often thousands of characters). Rendering either raw produces a wall of text,
 * so fall back to the description's first sentence/line, truncated.
 */
function deriveTitle(snap: Record<string, unknown>, row: TaskEntity): string {
  const title = str(snap.title);
  if (title) return title;

  const source = str(snap.description) ?? str(row.canonical_name);
  if (!source) return row.entity_id;

  const firstLine = source.split("\n").find((l) => l.trim())?.trim() ?? source;
  // Prefer a sentence boundary when one lands within a sensible headline length.
  const stop = firstLine.search(/[.:]\s/);
  const head = stop > 20 && stop < 120 ? firstLine.slice(0, stop) : firstLine;
  return head.length > 120 ? `${head.slice(0, 117)}…` : head;
}

export function parseTask(row: TaskEntity): Task {
  const snap = unwrap(row);
  const status = str(snap.status) ?? "unknown";
  const assignedTo = str(snap.assigned_to);
  const parsed = date(row.last_observation_at ?? row.computed_at);
  const title = deriveTitle(snap, row);

  return {
    id: row.entity_id,
    title,
    status,
    bucket: toBucket(status),
    priority: str(snap.priority),
    assignedTo,
    updatedAt: parsed,
    computedAt: date(row.computed_at),
    updatedDate: str(snap.updated_date),
    undispatched: !assignedTo,
    question: isQuestion(snap, title),
    context: str(snap.context),
    description: str(snap.description),
    answer: str(snap.result),
    ref: refNumber(snap),
    recommendation: recommendation(snap),
    category: str(snap.category),
  };
}

/**
 * Rank a priority for sorting, highest urgency first.
 *
 * Production uses several spellings for the top band (`critical`, `urgent`,
 * `high`); treat them as one rather than scattering them through the order.
 * Anything unrecognized or unset sorts below the named bands but above nothing
 * — an unlabelled question is still a question.
 */
export function priorityRank(priority: string | null): number {
  switch ((priority ?? "").toLowerCase()) {
    case "critical":
    case "urgent":
      return 0;
    case "high":
      return 1;
    case "medium":
      return 2;
    case "low":
      return 4;
    default:
      return 3;
  }
}

/** An answered question is one that carries an answer, or has been closed out. */
export function isAnswered(t: Task): boolean {
  return Boolean(t.answer) || t.bucket === "done";
}

export function entityUrl(id: string): string {
  // /entities/<id> — NOT /inspector/entities/, which 308-redirects.
  return `https://neotoma.markmhendrickson.com/entities/${id}`;
}

/**
 * Absolute local timestamp, for the detail view. The sidebar's relative time
 * ("2h ago") is right for scanning a queue, but a question being read in full
 * is often being placed against a meeting or a commit, and "2h ago" cannot be
 * cross-referenced against anything.
 */
export function absoluteTime(d: Date | null): string | null {
  if (!d) return null;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function relativeTime(d: Date | null): string {
  if (!d) return "unknown";
  const secs = Math.round((Date.now() - d.getTime()) / 1000);
  if (secs < 60) return `${Math.max(secs, 0)}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86_400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86_400)}d ago`;
}
