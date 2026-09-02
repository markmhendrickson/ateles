/**
 * SESSION DIGESTS — PARSING AND THE COVERAGE QUESTION
 * ---------------------------------------------------
 * A `session_digest` is a record written ABOUT a session: what it claimed to
 * do, what was verified, what it left open. Fields verified against
 * `describe_entity_type` and 344 live entities rather than assumed — several
 * plausible-sounding ones do not exist, and two behave unexpectedly:
 *
 * A CLAIM IS NOT A TASK. The field is named `tasks_claimed`, and the name is
 * the source of a real ambiguity in the UI, so state it once here:
 *
 *   A CLAIM is a sentence a session wrote ABOUT ITSELF — "I did this." It has
 *   no owner, no lifecycle, and no existence outside the digest row. It is
 *   evidence of an assertion, not of the work.
 *   A TASK is an entity in the graph, with an id, an owner, and a status that
 *   something other than the claimant maintains.
 *
 * So "3 of 5" against a session means it ASSERTED five things and three were
 * checked. It does NOT mean five `task` entities exist — most claims never
 * become one. The two words are kept apart everywhere they surface, and the UI
 * says "self-reported" rather than leaning on "claims" to carry that alone,
 * because "claims" only reads as self-report to someone who has read this file.
 *
 *   - `session_title` is present on only 59 of 344 digests. The other 285 fall
 *     back to `worktree`, which the schema itself calls "advisory only" because
 *     worktree names can misdescribe content. Hence `displayTitle`.
 *   - `tasks_claimed` is an ARRAY on 343 digests and a JSON-encoded STRING on
 *     one. Both shapes are parsed; a single malformed row must not blank the
 *     view. See `parseClaims`.
 *
 * TWO DIFFERENT DATES, AND WHY IT MATTERS
 * ---------------------------------------
 * `time_span_end` is when the session RAN. `last_observation_at` is when the
 * digest ROW was written. They diverge sharply here: digests covering sessions
 * from 30 June to 27 August were mostly written on 24-25 August, in a
 * retroactive sweep. Sorting by the wrong one tells a completely different
 * story about the swarm's habits, so both are parsed and named explicitly.
 */

/** Raw entity as it arrives from `/api/sessions`. */
export interface SessionEntity {
  entity_id: string;
  last_observation_at?: string | null;
  snapshot?: Record<string, unknown> | null;
}

/**
 * One claimed task from `tasks_claimed`.
 *
 * `verification_state` is the load-bearing field: a claim starts as `intent`
 * (the session said it did this) and only `verify-work` upgrades it to
 * `confirmed` or `refuted` against a system of record. Rendering a claim
 * without its state would present a session's self-report as fact — precisely
 * the failure this dashboard exists to expose.
 */
export interface Claim {
  claim: string;
  /** outstanding | complete | blocked | dropped */
  statusClaimed: string | null;
  /** intent | confirmed | refuted | unverifiable */
  verificationState: string | null;
  verificationNote: string | null;
  evidence: string[];
}

/**
 * WHERE A SESSION'S DISPLAYED NAME CAME FROM.
 *
 * Measured over all 344 live digests: only 59 carry a `session_title`. The
 * other 285 previously fell back to `worktree`, which is a filesystem PATH —
 * and 75 of them are the same path, `/Users/markmhendrickson/repos/ateles`. An
 * index of 75 identical rows is not an index, and search over identical strings
 * only narrows to a large pile of the same thing.
 *
 * So a name is DERIVED when none is stored, and this field records which
 * happened. It is not cosmetic: the UI marks a derived name as derived, because
 * presenting one as a stored title would be the same class of confident-wrong
 * answer as rendering a failed count as `0`.
 *
 *   stored  — `session_title`, written by whoever digested the session.
 *   topics  — joined `topics`, which is populated on 342 of 344 digests and is
 *             genuinely distinguishing ("bottega8 · neotoma-security · …").
 *   summary — the first clause of `summary`, for the rare row with neither.
 *   path    — the `worktree` path, the last resort. Shown AS a path.
 *   id      — nothing to name it with at all; the session id's first 8 chars.
 *
 * Deriving from topics takes the index from 75 identical names to 338 unique
 * names out of 344. Nothing is written back: this is a display name, not a
 * stored one, and the app is read-only.
 */
export type NameSource = "stored" | "topics" | "summary" | "path" | "id";

export interface SessionDigest {
  id: string;
  sessionKey: string;
  /** The harness session id — the part after `claude-code:`. */
  sessionId: string;
  /** What to show as this session's name. Derived when nothing is stored. */
  title: string;
  /** Which field `title` came from — rendered when it is not a stored title. */
  titleSource: NameSource;
  harness: string | null;
  worktree: string | null;
  /** The worktree's last path segment: secondary context, never the name. */
  worktreeLabel: string | null;
  summary: string | null;
  topics: string[];
  /** When the session ran. */
  spanStart: string | null;
  spanEnd: string | null;
  /** When the digest row was written — NOT when the session ran. */
  writtenAt: Date | null;
  digestMethod: string | null;
  claims: Claim[];
  decisions: string[];
  openQuestions: string[];
  artifacts: { kind: string | null; ref: string | null; description: string | null }[];
  /**
   * How many transcript files this session spans, or null when `lineage_files`
   * is absent. Null is NOT zero — one digest genuinely has no lineage list, and
   * printing `0 files` for it would invent a measurement.
   */
  lineageFiles: number | null;
}

/** The live-session hint the proxy attaches. Filesystem-derived, not Neotoma. */
export interface LiveSessionHint {
  sessionId: string;
  sessionKey: string;
  projectSlug: string;
  mtime: string;
  basis: string;
}

export interface SessionsPayload {
  entities?: SessionEntity[];
  live?: LiveSessionHint | null;
  error?: string;
  /**
   * How many `session_digest` entities EXIST, as against how many `entities`
   * holds. Upstream has always sent this and the route has always passed it
   * through; it was simply absent from this type, so no consumer could reach
   * it and the index printed its own row count as the store's total instead.
   * An unread field is how a page ends up truthful only by coincidence.
   */
  total?: unknown;
}

function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

/** Coerce to a string array, tolerating a JSON-encoded string. */
function strArray(v: unknown): string[] {
  const raw = typeof v === "string" ? safeJson(v) : v;
  if (!Array.isArray(raw)) return [];
  return raw.map((x) => (typeof x === "string" ? x : String(x ?? ""))).filter(Boolean);
}

function safeJson(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

/**
 * `tasks_claimed` arrives as an array on 343 of 344 digests and as a
 * JSON-encoded string on one. Handle both rather than letting the odd row
 * render as an empty task list, which would misreport that session as having
 * claimed no work at all.
 */
function parseClaims(v: unknown): Claim[] {
  const raw = typeof v === "string" ? safeJson(v) : v;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((c) => {
      if (!c || typeof c !== "object") return null;
      const o = c as Record<string, unknown>;
      const text = str(o.claim);
      if (!text) return null;
      return {
        claim: text,
        statusClaimed: str(o.status_claimed),
        verificationState: str(o.verification_state),
        verificationNote: str(o.verification_note),
        evidence: strArray(o.evidence_pointers),
      };
    })
    .filter((c): c is Claim => c !== null);
}

function parseArtifacts(v: unknown): SessionDigest["artifacts"] {
  const raw = typeof v === "string" ? safeJson(v) : v;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((a) => {
      if (!a || typeof a !== "object") return null;
      const o = a as Record<string, unknown>;
      return { kind: str(o.kind), ref: str(o.ref), description: str(o.description) };
    })
    .filter((a): a is SessionDigest["artifacts"][number] => a !== null && Boolean(a.ref));
}

/**
 * Does this string look like a filesystem path rather than a name?
 *
 * Both shapes occur: real POSIX paths (`/Users/…`) and the slugified form
 * Claude Code uses for transcript directories (`-Users-markmhendrickson-…`).
 * Twelve of the 59 STORED titles are path-shaped too, so this is applied to
 * stored titles as well — a stored path is still a path, and calling it a title
 * because a field held it would be trusting the field over its contents.
 */
export function looksLikePath(v: string): boolean {
  return v.startsWith("/") || v.startsWith("-Users") || v.includes("/");
}

/** The last meaningful segment of a worktree path — `…/worktrees/foo` → `foo`. */
function pathLeaf(v: string | null): string | null {
  if (!v) return null;
  const leaf = v.replace(/\/+$/, "").split("/").pop()?.trim();
  return leaf ? leaf : null;
}

/**
 * The first clause of a summary, as a last-resort name.
 *
 * Summaries run 391-2509 characters, so this takes only the opening sentence
 * and caps it. It is explicitly marked as derived-from-summary in the UI.
 */
function summaryClause(v: string | null): string | null {
  if (!v) return null;
  const first = v.split(/(?<=[.!?])\s|\n/)[0]?.trim();
  if (!first) return null;
  return first.length > 96 ? `${first.slice(0, 95).trimEnd()}…` : first;
}

/**
 * NAME A SESSION, and say where the name came from.
 *
 * Order is by how well each source distinguishes one session from another, not
 * by which field sounds most authoritative. A stored `session_title` wins —
 * unless it is itself a path, in which case topics describe the session better
 * than the directory it ran in did.
 */
export function displayName(s: {
  storedTitle: string | null;
  topics: string[];
  summary: string | null;
  worktree: string | null;
  sessionId: string;
}): { title: string; source: NameSource } {
  if (s.storedTitle && !looksLikePath(s.storedTitle)) {
    return { title: s.storedTitle, source: "stored" };
  }
  if (s.topics.length) {
    return { title: s.topics.slice(0, 3).join(" · "), source: "topics" };
  }
  const clause = summaryClause(s.summary);
  if (clause) return { title: clause, source: "summary" };

  // Nothing describes the work. Show the path — but the caller renders it AS a
  // path, so it never reads as a title someone chose.
  const leaf = pathLeaf(s.storedTitle ?? s.worktree);
  if (leaf) return { title: leaf, source: "path" };

  return { title: s.sessionId.slice(0, 8) || "Untitled session", source: "id" };
}

export function parseSession(row: SessionEntity): SessionDigest {
  const s = row.snapshot ?? {};
  const sessionKey = str(s.session_key) ?? "";
  // session_key is `<harness>:<session-id>`; the id is what matches a live
  // transcript filename.
  const sessionId = sessionKey.includes(":") ? sessionKey.slice(sessionKey.indexOf(":") + 1) : sessionKey;
  const written = str(row.last_observation_at);
  const worktree = str(s.worktree);
  const topics = strArray(s.topics);
  const summary = str(s.summary);

  const named = displayName({
    storedTitle: str(s.session_title),
    topics,
    summary,
    worktree,
    sessionId,
  });

  // Absent `lineage_files` is null, not 0 — see the field's doc comment.
  const lineage = Array.isArray(s.lineage_files) ? s.lineage_files.length : null;

  return {
    id: row.entity_id,
    sessionKey,
    sessionId,
    title: named.title,
    titleSource: named.source,
    harness: str(s.harness),
    worktree,
    worktreeLabel: pathLeaf(worktree),
    summary,
    topics,
    spanStart: str(s.time_span_start),
    spanEnd: str(s.time_span_end),
    writtenAt: written ? new Date(written) : null,
    digestMethod: str(s.digest_method),
    claims: parseClaims(s.tasks_claimed),
    decisions: strArray(s.decisions),
    openQuestions: strArray(s.open_questions),
    artifacts: parseArtifacts(s.artifacts),
    lineageFiles: lineage,
  };
}

/**
 * Newest session first, by when the session RAN (`time_span_end`).
 *
 * Deliberately not `writtenAt`: the operator is browsing sessions, and "when
 * did this work happen" is the axis that makes a list of sessions navigable.
 * The dates are ISO `YYYY-MM-DD` strings so a lexical compare is chronological.
 * Ties break on `writtenAt` so the order is stable rather than arbitrary.
 */
export function byRecency(a: SessionDigest, b: SessionDigest): number {
  const d = String(b.spanEnd ?? "").localeCompare(String(a.spanEnd ?? ""));
  if (d !== 0) return d;
  return (b.writtenAt?.getTime() ?? 0) - (a.writtenAt?.getTime() ?? 0);
}

/** Tone for a claim's verification state. Confirmed and refuted must not look alike. */
export function verificationTone(state: string | null): "ok" | "bad" | "warn" | "muted" {
  switch ((state ?? "").toLowerCase()) {
    case "confirmed":
      return "ok";
    case "refuted":
      return "bad";
    case "unverifiable":
      return "warn";
    default:
      return "muted"; // `intent`, `narrative`, or absent — nobody checked
  }
}

/**
 * IS THIS CLAIM CHECKED, and by what?
 *
 * `verification_state` is the only field separating "a session said so" from
 * "a system of record agrees". Measured over all 344 live digests (3062
 * claims), five values occur — one more than the four the schema documents:
 *
 *   intent        2435   claimed, never checked
 *   confirmed      528   checked against a system of record, and true
 *   refuted         46   checked, and false
 *   unverifiable    42   checked, and no record could settle it
 *   narrative       11   UNDECLARED — prose, not a checkable assertion
 *
 * `narrative` is not in the schema's value set and has no defined meaning here,
 * so it is deliberately NOT counted as checked. Lumping an undeclared value in
 * with `confirmed` would be exactly the confident-wrong answer this dashboard
 * exists to catch; it renders muted alongside `intent` and is counted as
 * unchecked until someone declares what it means.
 */
export function isChecked(c: Claim): boolean {
  const s = (c.verificationState ?? "").toLowerCase();
  return s === "confirmed" || s === "refuted" || s === "unverifiable";
}

/** A claim a system of record actually confirmed — the only "done" worth the word. */
export function isConfirmed(c: Claim): boolean {
  return (c.verificationState ?? "").toLowerCase() === "confirmed";
}

/**
 * THE VERIFICATION MIX for a set of claims.
 *
 * Every figure is counted from the claims in hand; nothing is defaulted. An
 * absent `verification_state` counts as `intent`, because that is what the
 * schema says an unset state means — a claim nobody has checked.
 */
export interface ClaimMix {
  total: number;
  /** Claims the session itself marked `complete`. Self-report, not evidence. */
  claimedComplete: number;
  confirmed: number;
  refuted: number;
  unverifiable: number;
  /** `intent`, plus any undeclared state such as `narrative`. */
  unchecked: number;
  /** Every distinct stored state and its count, including undeclared ones. */
  states: { state: string; count: number }[];
}

export function claimMix(claims: Claim[]): ClaimMix {
  const states = new Map<string, number>();
  let claimedComplete = 0;
  let confirmed = 0;
  let refuted = 0;
  let unverifiable = 0;

  for (const c of claims) {
    const s = (c.verificationState ?? "intent").toLowerCase();
    states.set(s, (states.get(s) ?? 0) + 1);
    if (isComplete(c)) claimedComplete += 1;
    if (s === "confirmed") confirmed += 1;
    else if (s === "refuted") refuted += 1;
    else if (s === "unverifiable") unverifiable += 1;
  }

  return {
    total: claims.length,
    claimedComplete,
    confirmed,
    refuted,
    unverifiable,
    unchecked: claims.length - confirmed - refuted - unverifiable,
    states: [...states.entries()]
      .map(([state, count]) => ({ state, count }))
      .sort((a, b) => b.count - a.count),
  };
}

/** Tone for what the session CLAIMED, independent of whether anyone verified it. */
export function claimStatusTone(status: string | null): "ok" | "bad" | "warn" | "muted" {
  switch ((status ?? "").toLowerCase()) {
    case "complete":
      return "ok";
    case "blocked":
      return "bad";
    case "outstanding":
      return "warn";
    default:
      return "muted"; // `dropped`, or unstated
  }
}

/** A claim counts as finished only if the session said so. */
export function isComplete(c: Claim): boolean {
  return (c.statusClaimed ?? "").toLowerCase() === "complete";
}

/**
 * COVERAGE — the point of the histogram.
 *
 * Counts digests by the day their ROW was written, not by the day the session
 * ran, because the question being answered is "how regularly does this swarm
 * record its sessions?". By write date the answer is stark: 307 of 344 digests
 * were written on 24-25 August, and none since 28 August.
 */
export interface CoverageDay {
  date: string;
  count: number;
}

export function coverageByWriteDate(sessions: SessionDigest[]): CoverageDay[] {
  const hist = new Map<string, number>();
  for (const s of sessions) {
    if (!s.writtenAt) continue;
    const key = s.writtenAt.toISOString().slice(0, 10);
    hist.set(key, (hist.get(key) ?? 0) + 1);
  }
  return [...hist.entries()].map(([date, count]) => ({ date, count })).sort((a, b) => a.date.localeCompare(b.date));
}

/** Whole days between the newest digest and now — the size of the blind spot. */
export function daysSince(latest: Date | null): number | null {
  if (!latest) return null;
  return Math.floor((Date.now() - latest.getTime()) / 86_400_000);
}

/* ────────────────────────────────────────────────────────────────────────────
 * MEASURED COVERAGE — three states that must never be rendered as each other
 * ────────────────────────────────────────────────────────────────────────────
 *
 * The panel this feeds previously had one failure mode and one silent lie.
 *
 * THE SILENT LIE. When `/api/sessions` failed, the caller left `sessions` at
 * `[]`, the panel early-returned `null`, and the whole section vanished from
 * the page. A vanished panel is indistinguishable from "the swarm is fully
 * digested" — the reader draws the opposite of the true conclusion. The list
 * route deliberately only raises an error when BOTH it and the conversation
 * read fail, so a digest-only failure reaches this code as an empty array with
 * no error attached. Coverage therefore takes `failed` EXPLICITLY rather than
 * inferring absence from a zero count.
 *
 * THE HARDCODED SHAPE. The prose asserted the burst was "just two days" and
 * the histogram's own top-2 was formatted to match. That was true of the
 * August snapshot and is not a fact about the data — on any other corpus it
 * fabricates a claim the bars do not support. `burst` is now whatever minimal
 * set of days actually carries a majority of the digests, and `burstDays`
 * reports how many that took, so the sentence describes the data in hand.
 *
 * WHAT THE GAP MEASURES. `daysSince(newest)` counts from the newest digest ROW
 * write (`last_observation_at`), which is the honest answer to "when did this
 * swarm last record a session" — but it is only the gap for THIS entity type.
 * Nothing else in the store is being asserted about, so `subject` names the
 * type out loud. Read the panel's number as "session_digest rows", never as
 * "the swarm reported nothing" — daemon_report is a separate, far staler
 * channel (13 rows, newest 2026-08-18) that this view does not count.
 */
export type CoverageState = "failed" | "empty" | "measured";

export interface Coverage {
  state: CoverageState;
  /** The entity type actually counted. Named so the gap is not over-read. */
  subject: string;
  days: CoverageDay[];
  total: number;
  newest: Date | null;
  /** Whole days since the newest digest row was written, or null. */
  gapDays: number | null;
  /** The fewest write-days that together hold a majority of all digests. */
  burst: CoverageDay[];
  /** How many digests those days hold. */
  burstCount: number;
}

/**
 * Build the coverage model, keeping "the query failed" distinct from "no
 * digests exist". Every figure is derived from `sessions`; nothing is assumed
 * and nothing is defaulted to a plausible-looking number.
 */
export function measureCoverage(
  sessions: SessionDigest[],
  failed: boolean,
  subject = "session_digest",
): Coverage {
  const days = coverageByWriteDate(sessions);
  const newest = sessions.reduce<Date | null>(
    (m, s) => (s.writtenAt && (!m || s.writtenAt > m) ? s.writtenAt : m),
    null,
  );

  // Smallest set of days covering a majority — the honest version of "a
  // one-off sweep". On evenly-spread data this grows, and the prose that
  // reads it stops claiming a burst, which is the correct behaviour.
  const ranked = [...days].sort((a, b) => b.count - a.count);
  const burst: CoverageDay[] = [];
  let burstCount = 0;
  for (const d of ranked) {
    if (burstCount * 2 > sessions.length) break;
    burst.push(d);
    burstCount += d.count;
  }

  return {
    state: failed ? "failed" : sessions.length === 0 ? "empty" : "measured",
    subject,
    days,
    total: sessions.length,
    newest,
    gapDays: daysSince(newest),
    burst: burst.sort((a, b) => a.date.localeCompare(b.date)),
    burstCount,
  };
}

export function shortDate(iso: string | null): string {
  if (!iso) return "unknown";
  const d = new Date(iso.length <= 10 ? `${iso}T00:00:00` : iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/* ────────────────────────────────────────────────────────────────────────────
 * THE INDEX — duration, search, and filtering
 * ──────────────────────────────────────────────────────────────────────────── */

/**
 * How long the session ran, in hours — or null when that cannot be measured.
 *
 * `time_span_start`/`end` arrive in TWO shapes: a full ISO timestamp on 270 of
 * 344 digests, and a bare `YYYY-MM-DD` date on the other 74. A date-only span
 * cannot yield a duration: `2026-08-26` to `2026-08-27` is anywhere from a
 * minute to two days, and subtracting the midnights would manufacture a
 * confident "24h" that was never measured. Those rows return null and the index
 * prints "—".
 *
 * This is the same rule as `measuredSample()` on the Schemas page: a figure that
 * was not measured must not render as a number.
 */
export function durationHours(s: SessionDigest): number | null {
  const { spanStart: a, spanEnd: b } = s;
  // Length is the discriminator: 10 chars is a date, ~24 is a timestamp.
  if (!a || !b || a.length <= 10 || b.length <= 10) return null;
  const t0 = Date.parse(a);
  const t1 = Date.parse(b);
  if (Number.isNaN(t0) || Number.isNaN(t1)) return null;
  const hours = (t1 - t0) / 3_600_000;
  return hours >= 0 ? hours : null;
}

/** A duration for display, or "—" when it was never measurable. */
export function formatDuration(hours: number | null): string {
  if (hours === null) return "—";
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  if (hours < 48) return `${hours < 10 ? hours.toFixed(1) : Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

/**
 * Does this session match the operator's search?
 *
 * Matches across NAME, TOPICS, WORKTREE, HARNESS, and DATE — deliberately more
 * than the title, because the titles are the very thing that is unhelpful.
 * Searching "ateles" should find sessions that ran in the ateles worktree even
 * when their derived name says nothing about it, and searching "aug 26" should
 * find that day's work.
 *
 * Every term must match somewhere (AND across terms, OR across fields), so
 * adding a word narrows rather than widens.
 */
/**
 * Case- and accent-insensitive comparison key.
 *
 * NFD splits an accented character into its base letter plus a combining mark,
 * and stripping the marks leaves "Theodóre" and "theodore" identical. Without
 * this, the two Theodóre sessions are reachable only by typing the accent — and
 * they matched a plain-spelled "theodore" ONLY through their summary prose,
 * which is why the results looked arbitrary rather than simply absent.
 */
function fold(v: string): string {
  return v.normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();
}

/**
 * Does this session match the operator's search?
 *
 * AND ACROSS TOKENS, OR ACROSS FIELDS. Every whitespace-separated token must
 * appear SOMEWHERE in the row, so "theodore elsa" matches a row with "theodore"
 * in the name and "elsa" in the topics, and adding a word can only ever narrow.
 *
 * WHY THIS LOOKED LIKE AN OR-BUG. The token combination was always `.every()`.
 * The accent was the real defect: "theodóre" does not contain the substring
 * "theodore", so a plain-spelled query hit those rows only via their summary
 * text. Because each token then matched a DIFFERENT arbitrary subset, adding a
 * second word could appear to broaden the results. Folding accents removes the
 * inconsistency that produced the illusion. Verified against all 344 live
 * digests: adding a token now narrows monotonically for every query tried,
 * including the "theodore elsa" case that prompted the report. (This app has no
 * test runner configured, so that check was run as a script, not committed.)
 *
 * Matching spans NAME, TOPICS, WORKTREE, HARNESS, METHOD, SUMMARY and DATE
 * deliberately: the titles are the very thing that is unhelpful here, so
 * searching "ateles" must still find sessions by the worktree they ran in.
 */
export function matchesQuery(s: SessionDigest, query: string): boolean {
  // `split(/\s+/)` on a trimmed string never yields an empty token, so a stray
  // double space collapses harmlessly rather than producing a "" that would
  // match every row (`includes("")` is true) and silently disable the filter.
  const terms = fold(query.trim()).split(/\s+/).filter(Boolean);
  if (!terms.length) return true;

  const haystack = fold(
    [
      s.title,
      s.worktree,
      s.worktreeLabel,
      s.harness,
      s.digestMethod,
      s.summary,
      s.spanEnd,
      s.spanStart,
      shortDate(s.spanEnd),
      ...s.topics,
    ]
      .filter(Boolean)
      .join(" "),
  );

  return terms.every((term) => haystack.includes(term));
}
