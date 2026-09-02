/**
 * THE TASK LIFECYCLE — the generic state machine EVERY task moves through.
 *
 * THE ORTHOGONAL AXIS TO WORKFLOWS. A workflow_definition declares gates for a
 * KIND of work ("who signs off on a neotoma feature"). The lifecycle is how ANY
 * task moves, whatever it is. The two used to be explained side by side on the
 * Workflows page, in a box whose whole job was to say they are different; they
 * now live on separate tabs, and the pointer on Workflows is the only thing
 * that remains of that box.
 *
 * WHERE THIS DEFINITION COMES FROM — AND WHY IT IS NOT IN THE GRAPH.
 * -----------------------------------------------------------------
 * The app's rule is that all DATA comes from Neotoma, and the per-stage counts
 * below honour it: they are live `/entities/query` totals, not constants.
 *
 * The lifecycle VOCABULARY, though, is not a Neotoma entity. It is a Python
 * state machine — `lib/daemon_runtime/task_lifecycle.py` — and that is not an
 * oversight to route around but the actual architecture: `TaskStatus` is the
 * enum Apis WRITES onto `task.status`, and `_TRANSITIONS` is the graph it
 * validates against. Neotoma's `task` schema declares `status` as a bare
 * `string` with NO enum, so the graph stores the values without constraining
 * them. Searching the graph for a stored definition finds nothing: the
 * `lifecycle_stage` entity type exists but holds a Neotoma USER-ONBOARDING
 * lifecycle (pre_install → activation → sustained_use), which is a different
 * subject entirely, and no `task_policy` entity defines task states.
 *
 * So this module transcribes the state machine, and the view says plainly that
 * the definition is code-sourced while the counts are graph-sourced. Inventing
 * a six-stage lifecycle to fit a nicer number, or silently presenting this as
 * stored data, would both be the failure to avoid. Every claim below is
 * verified against the named file at the named symbol — see `LIFECYCLE_SOURCE`.
 *
 * THE COUNTS ARE THE POINT. A page that only recites eleven definitions is
 * documentation. The same page showing that 3,544 tasks sit in `pending` and
 * exactly zero have ever been observed in `routed`, `executing`, or `verified`
 * is a diagnostic: it says the dispatcher's own middle states are unpopulated.
 */

/** Where the definition was read from, shown in the view rather than asserted. */
export const LIFECYCLE_SOURCE = {
  file: "lib/daemon_runtime/task_lifecycle.py",
  symbols: "TaskStatus, _TRANSITIONS, TERMINAL, ACTIVE",
  verifiedOn: "2026-08-31",
} as const;

/**
 * Who writes a transition. These are the ONLY writers — every `set_task_status`
 * call site in the repo is in one of these two daemons, plus the operator
 * acting out of band.
 */
export type Actor = "apis" | "watchdog" | "operator" | "none";

export const ACTOR_LABELS: Record<Actor, string> = {
  apis: "Apis (dispatcher)",
  watchdog: "Apis watchdog",
  operator: "Operator",
  none: "Nothing",
};

/** Coarse grouping, used only for colour and ordering. */
export type Phase = "queued" | "running" | "terminal" | "held";

/**
 * THE DISTINCTION THIS WHOLE MODULE EXISTS TO CARRY.
 *
 * The eleven states are NOT one sequence. They are two different KINDS of
 * thing, and rendering them as a flat list is what made the page unreadable —
 * it invited the question "does `done` come before or after `awaiting_approval`?",
 * which has no answer because the two are not on the same axis.
 *
 *   `path`  — a POSITION in the forward progression. Ordered. A task advances
 *             through these: pending → routed → executing → (verified) → done.
 *   `hold`  — a MODE a task enters FROM a path position and returns to. Not
 *             ordered, not a step: `awaiting_approval` is beside the path, not
 *             further along it. Each hold declares which path states can enter
 *             it (`enteredFrom`) and where a task goes when it leaves.
 *   `exit`  — a TERMINAL ending that is not success. `declined` and
 *             `superseded` end the task; they are not holds, because nothing
 *             returns from them.
 *
 * `done` is a path state (the last one) AND terminal. `declined`/`superseded`
 * are exits AND terminal. Terminality is a property, not a kind — which is why
 * `TERMINAL` has three members and the page groups all three as endings.
 */
export type StateKind = "path" | "hold" | "exit";

export interface Stage {
  /** Which of the two axes this state lives on. See `StateKind`. */
  kind: StateKind;
  /**
   * For a hold: the path states `_TRANSITIONS` shows entering it. Empty for
   * path states and exits — a path state's predecessor is its position.
   */
  enteredFrom: string[];
  /**
   * True when NO daemon ever writes this state — verified against every
   * `set_task_status` call site plus `gating.py`'s direct status corrections.
   *
   * THE DISTINCTION THIS EXISTS TO MAKE, and it is the same one `measuredSample`
   * makes for the Schemas tab. A state showing `0` can mean two opposite things:
   *
   *   "no task is here right now"  — the pipeline is idle at this step. Normal.
   *   "no task has ever been here" — nothing can write it. The state is dead
   *                                  vocabulary, and its zero is structural
   *                                  rather than a reading of throughput.
   *
   * Rendering those identically is what lets a zero argue for a fast pipeline
   * when it actually means an unreachable state. So the flag is set from the
   * code, and the view must show it.
   */
  neverWritten?: boolean;
  /**
   * Written by sessions or agents out of band, but by no daemon in the
   * dispatch loop. Distinct from `neverWritten`: the state IS reached, just
   * never by the state machine that claims to own it.
   */
  writtenOutOfBandOnly?: boolean;
  /** For a path state: is it required, or may the path skip it? */
  optional?: boolean;
  /** The value written to `task.status`, verbatim. */
  key: string;
  label: string;
  /** What being in this state means. */
  meaning: string;
  /** What moves a task INTO it — the transition, and who makes it. */
  entry: string;
  entryBy: Actor;
  /** What moves it OUT. */
  exit: string;
  exitBy: Actor;
  /** Legal successor states, from `_TRANSITIONS`. */
  next: string[];
  phase: Phase;
  /** In `TERMINAL` — the dispatcher never moves out of it automatically. */
  terminal: boolean;
  /** In `ACTIVE` — work may still be dispatched from here. */
  active: boolean;
}

/**
 * The eleven states, each tagged with its KIND. Transcribed from `TaskStatus`
 * and `_TRANSITIONS`.
 *
 * The array order is no longer meaningful on its own — the view partitions by
 * `kind` and orders the path by `PATH_ORDER`. Reading this list top to bottom
 * as a sequence is exactly the misreading the page now prevents.
 */
export const STAGES: Stage[] = [
  {
    key: "pending",
    kind: "path",
    enteredFrom: [],
    label: "Pending",
    meaning: "Created and not yet routed. The state every task is born in.",
    entry: "Written at creation by whatever agent or session filed the task. No transition — this is the entry point.",
    entryBy: "none",
    exit: "Apis resolves an owner and routes it; or it is held, blocked, declined, or superseded.",
    exitBy: "apis",
    next: ["routed", "awaiting_approval", "awaiting_input", "blocked", "declined", "superseded"],
    phase: "queued",
    terminal: false,
    active: true,
  },
  {
    key: "routed",
    kind: "path",
    enteredFrom: [],
    label: "Routed",
    meaning: "The dispatcher resolved an owner and a skill. Recorded so a task in flight can never still read “pending”.",
    entry: "Apis resolves a role from the task's tags or assigned_to and writes ROUTED before any gate runs.",
    entryBy: "apis",
    exit: "Passes the readiness and approval gates into executing; or is held at one of them.",
    exitBy: "apis",
    next: ["executing", "awaiting_approval", "awaiting_input", "blocked", "declined"],
    phase: "queued",
    terminal: false,
    active: true,
  },
  {
    key: "executing",
    kind: "path",
    enteredFrom: [],
    label: "Executing",
    meaning: "The agent subprocess has been spawned and is doing the work.",
    entry: "Apis writes EXECUTING immediately before spawning the agent.",
    entryBy: "apis",
    exit: "The run finishes (done, or verified first), raises (failed), or is blocked.",
    exitBy: "apis",
    next: ["verified", "done", "failed", "blocked"],
    phase: "running",
    terminal: false,
    active: true,
  },
  {
    key: "verified",
    kind: "path",
    enteredFrom: [],
    optional: true,
    neverWritten: true,
    label: "Verified",
    meaning: "The outcome was checked. An OPTIONAL pre-done gate — the happy path may go straight from executing to done.",
    entry: "Written when a verification step ran and passed.",
    entryBy: "apis",
    exit: "Accepted as done, or sent back as failed or blocked.",
    exitBy: "apis",
    next: ["done", "failed", "blocked"],
    phase: "running",
    terminal: false,
    active: true,
  },
  {
    key: "done",
    kind: "path",
    enteredFrom: [],
    label: "Done",
    meaning: "Terminal success. Apis writes a result summary alongside it.",
    entry: "The agent run completed, from executing or verified.",
    entryBy: "apis",
    exit: "Nothing. Terminal — no automatic transition leaves this state.",
    exitBy: "none",
    next: [],
    phase: "terminal",
    terminal: true,
    active: false,
  },
  {
    key: "failed",
    kind: "hold",
    enteredFrom: ["executing", "verified"],
    label: "Failed",
    meaning:
      "A TRANSIENT failure, not a verdict, and NOT an ending despite the name. `failed` is in ACTIVE, not TERMINAL — the watchdog may retry it with backoff, and a retry rejoins the path at routed.",
    entry: "The dispatch raised, or the run reported failure, from executing or verified.",
    entryBy: "apis",
    exit: "The watchdog re-routes it for a retry; once attempts are exhausted it is blocked instead.",
    exitBy: "watchdog",
    next: ["routed", "blocked", "declined"],
    phase: "held",
    terminal: false,
    active: true,
  },
  {
    key: "blocked",
    kind: "hold",
    enteredFrom: ["pending", "routed", "executing", "verified", "failed", "awaiting_approval", "awaiting_input"],
    label: "Blocked",
    meaning:
      "Needs the operator. Deliberately NOT terminal — remediation reopens it. Written with a blocked_reason: no owner could be routed, or retries ran out.",
    entry: "Apis finds no route or owner; or the watchdog exhausts the retry budget.",
    entryBy: "watchdog",
    exit: "The operator remediates and it is re-routed; or it is declined or superseded.",
    exitBy: "operator",
    next: ["routed", "declined", "superseded"],
    phase: "held",
    terminal: false,
    active: true,
  },
  {
    key: "awaiting_approval",
    kind: "hold",
    enteredFrom: ["pending", "routed"],
    label: "Awaiting approval",
    meaning: "Held at a gate checkpoint. The blast radius or confidence tripped the gate, so the operator decides before anything runs.",
    entry: "Apis holds the task at the gate and files a checkpoint_brief with the reason.",
    entryBy: "apis",
    exit: "The operator approves (back to routed) or rejects (declined).",
    exitBy: "operator",
    next: ["routed", "declined", "blocked"],
    phase: "held",
    terminal: false,
    active: true,
  },
  {
    key: "awaiting_input",
    kind: "hold",
    enteredFrom: ["pending", "routed"],
    label: "Awaiting input",
    meaning:
      "Parked as under-specified. The readiness gate scored the task below its threshold, so it needs operator context before it can run.",
    entry: "The readiness assessment fails; Apis records which fields are missing.",
    entryBy: "apis",
    exit: "The operator supplies the missing context and it is re-routed; or it is declined or blocked.",
    exitBy: "operator",
    next: ["routed", "declined", "blocked"],
    phase: "held",
    terminal: false,
    active: true,
  },
  {
    key: "declined",
    kind: "exit",
    enteredFrom: ["pending", "routed", "failed", "blocked", "awaiting_approval", "awaiting_input"],
    label: "Declined",
    meaning: "The operator rejected the task. Terminal.",
    entry: "An operator decision at a gate, or on a blocked or failed task.",
    entryBy: "operator",
    exit: "Nothing. Terminal.",
    exitBy: "none",
    next: [],
    phase: "terminal",
    terminal: true,
    active: false,
  },
  {
    key: "superseded",
    kind: "exit",
    writtenOutOfBandOnly: true,
    enteredFrom: ["pending", "blocked"],
    label: "Superseded",
    meaning: "Replaced by another task. Terminal.",
    entry: "Written when a newer task takes this one's place.",
    entryBy: "operator",
    exit: "Nothing. Terminal.",
    exitBy: "none",
    next: [],
    phase: "terminal",
    terminal: true,
    active: false,
  },
];

/**
 * WHICH STATES ANYTHING ACTUALLY WRITES — audited, not assumed.
 *
 * Enumerated from every `set_task_status(...)` call site in `apis.py` and
 * `task_watchdog.py`, plus `gating.py`'s direct `status` correction, plus
 * task creation. Result: of the eleven declared states, ONE has no writer at
 * all, and one more is reached only from outside the dispatch loop.
 *
 *   Written by a daemon (7): routed, executing, failed, blocked, done,
 *                            awaiting_input, awaiting_approval
 *   Written elsewhere (3):   pending (at creation), declined (gating.py),
 *                            superseded (sessions/agents, no daemon)
 *   NEVER written (1):       verified
 *
 * `verified` is dead vocabulary. It is declared in `TaskStatus`, it has a row
 * in `_TRANSITIONS`, the page describes it as an optional pre-done gate — and
 * nothing has ever put a task in it. Its live count of 0 is therefore not
 * evidence about throughput; the state is unreachable as the code stands.
 * That is the same "declared intent with no consumer" pattern as a schema
 * field nothing populates, and the view marks it rather than letting the zero
 * speak for itself.
 */
export const WRITER_AUDIT = {
  byDaemon: [
    "routed",
    "executing",
    "failed",
    "blocked",
    "done",
    "awaiting_input",
    "awaiting_approval",
  ],
  elsewhere: ["pending", "declined", "superseded"],
  neverWritten: ["verified"],
  checkedAgainst:
    "every set_task_status call site in apis.py and task_watchdog.py, gating.py's status correction, and task creation",
} as const;

/** The forward progression, in order. The ONLY ordered axis on this page. */
export const PATH_ORDER = ["pending", "routed", "executing", "verified", "done"] as const;

/** Partition the eleven states by kind. The view renders three groups, not one list. */
export function byKind(kind: Stage["kind"]): Stage[] {
  const stages = STAGES.filter((s) => s.kind === kind);
  if (kind !== "path") return stages;
  return stages
    .slice()
    .sort(
      (a, b) =>
        PATH_ORDER.indexOf(a.key as (typeof PATH_ORDER)[number]) -
        PATH_ORDER.indexOf(b.key as (typeof PATH_ORDER)[number]),
    );
}

/**
 * IS THERE A REVIEW STAGE? — the question the flat list made askable.
 *
 * Asked as: "don't we need a review stage? I guess that's the awaiting approval
 * stage." The guess is wrong, and the two things it conflates are worth keeping
 * apart because they are owned by different machines entirely.
 *
 * Verified before being asserted:
 *   - `awaiting_approval` is written by the GATE, `lib/daemon_runtime/gating.py`,
 *     which files a `checkpoint_brief` (`write_checkpoint_brief`, `checkpoint_name:
 *     "PLAN"`) and holds on blast radius and confidence. What it gates is a PLAN,
 *     BEFORE the work runs. It is an operator decision, not a reading of code.
 *   - CODE review is Lanius's `pr_review` gate. It runs on `issue` entities via
 *     `gate_status` (`{pm, ux, arch, impl, pr_review, qa, legal}`) — a SEPARATE
 *     state machine, on a different entity type, on the PR. Grepped for every
 *     `set_task_status` call site: they are all in `apis.py` and
 *     `task_watchdog.py`. Nothing in any review path writes `task.status`.
 *
 * So the task lifecycle has NO code-review state, and no merge or deploy state
 * either. A task can read `done` while the PR that implements it is unreviewed,
 * unmerged, and undeployed — the lifecycle simply cannot express the difference.
 */
export const REVIEW_NOTE = {
  approvalIs:
    "A CHECKPOINT gate, not a review. gating.py holds the task before the work runs and files a checkpoint_brief for the operator to decide on — it weighs blast radius and confidence, and nobody reads any code.",
  codeReviewIs:
    "Lanius's pr_review gate, on the PR. It lives on the issue entity's gate_status map (pm, ux, arch, impl, pr_review, qa, legal) — a different state machine on a different entity type.",
  absence:
    "The task lifecycle has no review, merge, or deploy state. Every set_task_status call site is in apis.py or task_watchdog.py; no review path writes task.status. A task can read done while its PR is unreviewed, unmerged, and undeployed.",
  issue: {
    number: 565,
    url: "https://github.com/markmhendrickson/ateles/issues/565",
    title:
      "APPROVED PRs with green checks are never merged; the refusal is silent",
  },
} as const;

/**
 * WHY THE STATUS DATA IS WEAKER THAN IT LOOKS — `is_valid_transition` fails open.
 *
 * `can_transition` in `task_lifecycle.py` returns True for any origin state it
 * does not recognise:
 *
 *     if f not in _TRANSITIONS:
 *         return True  # unknown (legacy/ad-hoc) origin → don't block
 *
 * That is deliberate — the module's stated goal is to RECORD progress, not to
 * police it. But it means validation only bites on the minority of tasks whose
 * status is already canonical. Most tasks carry an off-vocabulary spelling
 * (`open`, `todo`, `completed`, `in_progress`), which the `task` schema accepts
 * because `status` is an unconstrained string — so for most tasks, EVERY
 * transition passes unchecked, including ones the graph above forbids.
 *
 * The consequence for reading this page: the counts are what is stored, not
 * what was validated. A task in `done` did not necessarily pass through the
 * path to get there.
 */
export const VALIDATION_NOTE = {
  behaviour:
    "can_transition() returns True for any origin state not in _TRANSITIONS — an unknown status is never blocked.",
  rationale:
    "Deliberate: the module records progress rather than policing it, so a legacy or ad-hoc status can never wedge a write.",
  consequence:
    "Validation only bites on tasks already carrying a canonical status. For the rest, every transition passes unchecked — including ones this graph forbids.",
} as const;

/**
 * OWNER vs ASSIGNEE — distinct fields, and the distinction is load-bearing.
 *
 * The `task` schema declares BOTH `owner` and `assigned_to`. They are not
 * synonyms and the lifecycle only reads one of them: `assigned_to` carries the
 * ROLE NAME the dispatcher routes on ("cicada"), and it is what Apis resolves
 * a skill from before writing ROUTED. `owner` is a free-text field no
 * dispatcher reads.
 *
 * This matters because an unroutable task looks identical to a routed one from
 * the outside: a task with `owner` set but `assigned_to` empty has a person's
 * name on it and still goes BLOCKED with "no route/owner", because nothing can
 * spawn from `owner`.
 */
export const OWNERSHIP_NOTE = {
  assignedTo:
    "The role the dispatcher routes on. Apis resolves it (with the task's tags) to a skill, and cannot write ROUTED without one.",
  owner:
    "A declared owner, free text. No dispatcher reads it — a task carrying only an owner still blocks with “no route/owner”.",
  perStage:
    "There is no per-stage assignee field on a task. Who acts at each stage is fixed by the state machine — Apis, its watchdog, or the operator — not stored per task.",
} as const;

/** Live count for one status, as `/api/lifecycle` returns it. */
export interface StageCount {
  status: string;
  /** Total matching tasks, or null when the count could not be read. */
  total: number | null;
}

export interface LifecyclePayload {
  counts?: StageCount[];
  /** Tasks whose status is none of the eleven — legacy and ad-hoc values. */
  offVocabulary?: number | null;
  /** Total `task` entities, the denominator. */
  totalTasks?: number | null;
  error?: string;
}

/**
 * Index counts by status, DROPPING any that failed to read.
 *
 * Dropping rather than zero-filling is the whole point: a status absent from
 * this map is unmeasured, and `Map.get` returning `undefined` cannot be
 * mistaken for a measurement the way a defaulted `0` can. Every read site must
 * therefore go through `stageCount` below rather than `?? 0`.
 */
export function countsByStatus(counts: StageCount[]): Map<string, number> {
  const map = new Map<string, number>();
  for (const c of counts) {
    if (typeof c.total === "number") map.set(c.status, c.total);
  }
  return map;
}

/**
 * The one honest read of a per-state count. `null` means UNMEASURED.
 *
 * THE BUG CLASS THIS CLOSES, which the Schemas tab shipped three times in one
 * day: a `.catch(() => null)` upstream becomes a `?? 0` downstream, and a query
 * that timed out renders as a confident "0 tasks" — indistinguishable from a
 * state that genuinely holds none, and then cached in that shape.
 *
 * The proxy already refuses to fabricate: `countByStatus` in `neotomaProxy.ts`
 * returns `total: null` on any failure. This is the client-side half of the
 * same contract, and it is the half that was easy to lose, because `?? 0` reads
 * as a harmless default at every call site where it is wrong.
 *
 * Mirrors `measuredSample()` in `schemaData.ts`: same distinction, same reason.
 */
export function stageCount(counts: Map<string, number>, key: string): number | null {
  return counts.has(key) ? (counts.get(key) as number) : null;
}

/**
 * Sum a set of states, or `null` if ANY of them is unmeasured.
 *
 * A partial sum is a fabricated total — it looks like a measurement of the
 * whole set while silently describing a subset. Any aggregate this page shows
 * ("the middle is empty", "N tasks are held") must be all-or-nothing.
 */
export function sumMeasured(
  counts: Map<string, number>,
  keys: readonly string[],
): number | null {
  let total = 0;
  for (const k of keys) {
    const n = stageCount(counts, k);
    if (n === null) return null;
    total += n;
  }
  return total;
}
