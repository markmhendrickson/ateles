/**
 * AGENT DIRECTORY DATA
 * --------------------
 * `agent_definition` entities describe every agent in the swarm. They come back
 * from the same `/entities/query` endpoint as tasks, so the parsing shape here
 * mirrors `tasks.ts` — including the double-nested-snapshot tolerance.
 *
 * The wrinkle specific to this type: list-valued fields are NOT consistently
 * typed across entities. `tool_allowlist` arrives as a real array on some
 * agents (Turdus), a JSON-encoded string on others (Sitta), and a bare
 * comma-separated string in older raw fragments. `asList` below normalizes all
 * three rather than trusting any one shape.
 */

/** Shape of the rows Neotoma returns from POST /entities/query. */
export interface AgentEntity {
  entity_id: string;
  canonical_name?: string;
  snapshot?: Record<string, unknown> | null;
  last_observation_at?: string | null;
  computed_at?: string | null;
}

export interface Agent {
  id: string;
  /** Display name, derived defensively — see `deriveName`. */
  name: string;
  /** Raw tier string as stored, e.g. "T3". Empty when unset. */
  tier: string;
  /** Bucket the raw tier maps into, for grouping and filtering. */
  tierGroup: TierGroup;
  status: string;
  genus: string | null;
  description: string | null;
  notes: string | null;
  toolAllowlist: string[];
  contextEntityTypes: string[];
  operationalEntityTypes: string[];
  agentGrant: string | null;
  aauthSub: string | null;
  version: string | null;
  promptMarkdown: string | null;
  updatedAt: Date | null;
}

export const TIERS = ["T1", "T2", "T3", "T4", "other"] as const;
export type TierGroup = (typeof TIERS)[number];

export const TIER_LABELS: Record<TierGroup, string> = {
  T1: "T1 · Hosts",
  T2: "T2 · Resident",
  T3: "T3 · Daemons",
  T4: "T4 · Invocable",
  other: "Unclassified",
};

/** One line on what each tier structurally *is*, shown above each group. */
export const TIER_BLURBS: Record<TierGroup, string> = {
  T1: "Hosts — channel message loops.",
  T2: "Resident per-session conversational agents.",
  T3: "Always-on launchd daemons.",
  T4: "Invocable — spawned per task as a CLI subprocess, no long-lived process.",
  other: "No tier recorded on the definition.",
};

/**
 * Tier is stored as "T1".."T4", but tolerate case and stray whitespace rather
 * than dropping an agent out of its group over a formatting difference.
 * Anything unrecognized lands in `other` so nothing vanishes from the view.
 */
export function toTierGroup(tier: string): TierGroup {
  const t = tier.trim().toUpperCase();
  return (TIERS as readonly string[]).includes(t) && t !== "other" ? (t as TierGroup) : "other";
}

function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

/**
 * Coerce a list-valued field into a string array.
 *
 * These fields are stored inconsistently across agent definitions: a real JSON
 * array, a JSON-encoded string, or a bare comma-separated string. Handle all
 * three — a naive `Array.isArray` check silently renders nothing for the agents
 * using the string forms.
 */
export function asList(v: unknown): string[] {
  if (Array.isArray(v)) {
    return v.map((x) => String(x).trim()).filter(Boolean);
  }
  const s = str(v);
  if (!s) return [];
  if (s.startsWith("[")) {
    try {
      const parsed: unknown = JSON.parse(s);
      if (Array.isArray(parsed)) return parsed.map((x) => String(x).trim()).filter(Boolean);
    } catch {
      // Malformed JSON — fall through to the comma split below.
    }
  }
  return s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}

/** Neotoma sometimes double-nests the snapshot; tolerate both shapes. */
function unwrap(row: AgentEntity): Record<string, unknown> {
  const snap = row.snapshot;
  if (snap && typeof snap === "object") {
    const inner = (snap as Record<string, unknown>).snapshot;
    if (inner && typeof inner === "object") return inner as Record<string, unknown>;
    return snap as Record<string, unknown>;
  }
  return {};
}

/**
 * Not every entity carries a usable `name`. `canonical_name` is reliably
 * prefixed `agent_definition:<name>` for this type, so strip the prefix; fall
 * back to the entity id rather than rendering a blank row.
 */
function deriveName(snap: Record<string, unknown>, row: AgentEntity): string {
  const name = str(snap.name);
  if (name) return name;

  const canonical = str(row.canonical_name);
  if (canonical) {
    const stripped = canonical.replace(/^agent_definition:/i, "").trim();
    // Guard against canonical_name being a punctuation-stripped copy of a long
    // description rather than an actual name, as happens on other entity types.
    if (stripped && stripped.length <= 60) return stripped;
  }
  return row.entity_id;
}

export function parseAgent(row: AgentEntity): Agent {
  const snap = unwrap(row);
  const tier = str(snap.tier) ?? "";
  const ts = row.last_observation_at ?? row.computed_at ?? null;
  const parsed = ts ? new Date(ts) : null;

  return {
    id: row.entity_id,
    name: deriveName(snap, row),
    tier,
    tierGroup: toTierGroup(tier),
    status: str(snap.status) ?? "unknown",
    genus: str(snap.genus),
    description: str(snap.description),
    notes: str(snap.notes),
    toolAllowlist: asList(snap.tool_allowlist),
    contextEntityTypes: asList(snap.context_entity_types),
    operationalEntityTypes: asList(snap.operational_entity_types),
    agentGrant: str(snap.agent_grant),
    aauthSub: str(snap.aauth_sub),
    version: str(snap.version),
    promptMarkdown: str(snap.prompt_markdown),
    updatedAt: parsed && !Number.isNaN(parsed.getTime()) ? parsed : null,
  };
}

/** Bucket a status into a coarse tone for the badge, tolerating variants. */
export function statusTone(status: string): "ok" | "pending" | "off" {
  const s = status.toLowerCase();
  if (s === "active") return "ok";
  if (s.startsWith("active-") || s === "planned" || s === "proposed") return "pending";
  return "off";
}

/** First sentence or line of the description, for the list row. */
export function truncate(text: string | null, max = 160): string {
  if (!text) return "";
  const firstLine = text.split("\n").find((l) => l.trim())?.trim() ?? text;
  if (firstLine.length <= max) return firstLine;
  // Prefer a sentence boundary when one lands within the budget.
  const stop = firstLine.lastIndexOf(". ", max);
  if (stop > max * 0.5) return firstLine.slice(0, stop + 1);
  return `${firstLine.slice(0, max - 1).trimEnd()}…`;
}

/* ------------------------------------------------------------------ *
 * WHAT AN AGENT IS RESPONSIBLE FOR
 * ------------------------------------------------------------------ */

/**
 * Tasks whose `assigned_to` names this agent, as `/api/assigned` returns them.
 *
 * THE HONEST NUMBERS, measured on a 500-task sample of the 20,922 in Neotoma:
 * 468 carry no `assigned_to` at all, and the 32 that do are spread across
 * cicada (14), corvus (5), pavo (4), Bombycilla (2), and one each for operator,
 * waxwing, sturnus, ciconia, ateles, Luscinia, and fringilla.
 *
 * So MOST AGENTS WILL HAVE ZERO, and that is the accurate answer rather than a
 * broken section. The view says "No tasks assigned" in words for those agents;
 * it does not render an empty region that reads as a failed load.
 *
 * The other half of the finding is why this section earns its place: of those
 * 32 assigned tasks, the ones naming corvus, pavo, Bombycilla, waxwing,
 * ciconia, Luscinia, and operator name an owner Apis cannot spawn. They look
 * dispatched and will never run. The dispatchable ratio shown on the agent page
 * is exactly that fact, per agent.
 */
export interface AssignedTasksPayload {
  entities?: AgentEntity[];
  total?: number;
  error?: string;
}
