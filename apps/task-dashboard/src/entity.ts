/**
 * ONE ENTITY, FETCHED AND PARSED
 * ------------------------------
 * Backs both presentations of an entity detail: the full page at
 * `#/entities/<id>` and the slide-over sheet. They share this module and the
 * `EntityDetail` component, so there is exactly one renderer per entity type
 * and the two surfaces cannot drift apart.
 *
 * WHY IN-APP AT ALL
 * -----------------
 * Entity links used to leave for `https://neotoma.…/entities/<id>`. That threw
 * away the session the operator was reading from, and it did not even work:
 * entities and rendered pages 401 without an access_token, and those tokens
 * exist only in publish-time responses. The dev proxy already holds a bearer
 * token, so the same entity reads fine server-side — see `/api/entity`.
 *
 * VOCABULARY RULE, inherited from conversation.ts
 * ----------------------------------------------
 * Every label traces to an entity_type, a declared field name, or a stored
 * field value. Field names are humanized for display (`meta_description` ->
 * "Meta description") and never renamed into something more evocative. The
 * generic fallback below humanizes whatever fields an unrecognized type
 * happens to carry, which is why it can render a type this app has never seen
 * without inventing a vocabulary for it.
 */

/** An edge as `/api/entity` returns it. Mirrors the session view's shape. */
export interface EntityEdge {
  entity_id: string;
  /** null while the proxy hydrates this neighbour in the background. */
  entity_type: string | null;
  canonical_name: string | null;
  relationship_type: string;
  snapshot: Record<string, unknown> | null;
  direction: "outgoing" | "incoming";
}

export interface EntityRecord {
  entity_id: string;
  entity_type: string | null;
  canonical_name: string | null;
  snapshot: Record<string, unknown> | null;
}

export interface EntityPayload {
  entity?: EntityRecord | null;
  outgoing?: EntityEdge[];
  incoming?: EntityEdge[];
  /** True when the relationships read failed; the entity's own fields still render. */
  relationshipsFailed?: boolean;
  error?: string;
}

/** Neotoma sometimes double-nests the snapshot; tolerate both shapes. */
export function unwrapSnapshot(
  snapshot: Record<string, unknown> | null | undefined,
): Record<string, unknown> {
  if (!snapshot || typeof snapshot !== "object") return {};
  const inner = snapshot.snapshot;
  if (inner && typeof inner === "object") return inner as Record<string, unknown>;
  return snapshot;
}

export function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

/**
 * `canonical_name` arrives prefixed with the entity type on several types
 * ("plan:Ateles Agent Swarm Architecture"). Strip the prefix — the remainder is
 * still the stored name, not a rewritten one.
 */
export function entityTitle(record: EntityRecord): string {
  const s = unwrapSnapshot(record.snapshot);
  const title = str(s.title) ?? str(s.name);
  if (title) return title;

  const canonical = str(record.canonical_name);
  if (!canonical) return record.entity_id;
  const prefix = `${record.entity_type}:`;
  const stripped =
    record.entity_type && canonical.startsWith(prefix)
      ? canonical.slice(prefix.length).trim()
      : canonical;

  // Some types set canonical_name to a punctuation-stripped copy of a long
  // description. A headline-length cap keeps that from becoming a wall of text.
  if (!stripped) return record.entity_id;
  return stripped.length > 140 ? `${stripped.slice(0, 137)}…` : stripped;
}

/** Humanize a declared field name for display: `meta_description` -> "Meta description". */
export function fieldLabel(field: string): string {
  const words = field.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Singular display form of an entity_type: `rendered_page` -> "Rendered page". */
export function typeLabel(entityType: string | null): string {
  if (!entityType) return "Entity";
  const words = entityType.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * A snapshot value rendered as text, for the generic fallback.
 *
 * Returns null for anything empty so the caller can omit the row entirely
 * rather than printing a label with nothing under it. Arrays and objects are
 * kept structured (see `FieldValue` kinds) rather than being JSON-stringified
 * into an unreadable line.
 */
export type FieldKind = "text" | "markdown" | "list" | "json" | "scalar";

export interface Field {
  name: string;
  label: string;
  kind: FieldKind;
  /** Populated for text/markdown/scalar. */
  text?: string;
  /** Populated for list. */
  items?: string[];
  /** Populated for json — pretty-printed. */
  json?: string;
}

/** Fields whose stored value is markdown prose rather than a scalar. */
const MARKDOWN_FIELDS = new Set([
  "body",
  "public_body",
  "description",
  "details",
  "result",
  "summary",
  "overview",
  "scope",
  "rationale",
  "context",
  "notes",
  "prompt_markdown",
  "architecture_markdown",
  "phases_markdown",
  "taxonomy_markdown",
  "scope_summary",
  "core_rule",
  "purpose",
  "section_guidance",
]);

/** Prose long enough to want markdown rendering even without a known field name. */
const PROSE_THRESHOLD = 180;

/**
 * Turn one snapshot entry into a renderable field, or null when it holds
 * nothing worth a row.
 *
 * A string that parses as a JSON array or object is unwrapped first: several
 * declared list fields are stored JSON-encoded on some entities and as real
 * arrays on others (the same inconsistency `agents.ts` handles for
 * `tool_allowlist`), and a naive renderer prints one of those as a quoted blob.
 */
export function toField(name: string, value: unknown): Field | null {
  const label = fieldLabel(name);

  if (value === null || value === undefined || value === "") return null;

  if (typeof value === "boolean" || typeof value === "number") {
    return { name, label, kind: "scalar", text: String(value) };
  }

  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return null;

    // JSON-encoded arrays/objects arrive as strings on some entities.
    if (trimmed.startsWith("[") || trimmed.startsWith("{")) {
      try {
        const parsed: unknown = JSON.parse(trimmed);
        if (parsed && typeof parsed === "object") return toField(name, parsed);
      } catch {
        // Not JSON after all — fall through and treat it as prose.
      }
    }

    const kind: FieldKind =
      MARKDOWN_FIELDS.has(name) || trimmed.length > PROSE_THRESHOLD || trimmed.includes("\n")
        ? "markdown"
        : "text";
    return { name, label, kind, text: trimmed };
  }

  if (Array.isArray(value)) {
    // A list of scalars renders as chips; a list of objects has no faithful
    // chip form, so it keeps its structure as JSON.
    const scalars = value.filter((v) => typeof v === "string" || typeof v === "number");
    if (scalars.length === value.length) {
      const items = scalars.map((v) => String(v).trim()).filter(Boolean);
      return items.length ? { name, label, kind: "list", items } : null;
    }
    return value.length
      ? { name, label, kind: "json", json: JSON.stringify(value, null, 2) }
      : null;
  }

  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    return entries.length
      ? { name, label, kind: "json", json: JSON.stringify(value, null, 2) }
      : null;
  }

  return null;
}

/**
 * Every populated snapshot field, as renderable rows.
 *
 * `omit` carries the fields a type-specific view has already rendered itself,
 * so the generic remainder never duplicates them. This is what makes the
 * special-cased views safe: anything the schema grows that a view does not know
 * about still appears, rather than being silently dropped because no one
 * updated the component.
 */
export function fields(
  snapshot: Record<string, unknown> | null | undefined,
  omit: Iterable<string> = [],
): Field[] {
  const skip = new Set(omit);
  // `canonical_name` duplicates the heading; it is never a useful row.
  skip.add("canonical_name");

  const out: Field[] = [];
  for (const [name, value] of Object.entries(unwrapSnapshot(snapshot))) {
    if (skip.has(name)) continue;
    const field = toField(name, value);
    if (field) out.push(field);
  }
  return out;
}

/** The canonical in-app address for an entity. */
export function entityHash(id: string): string {
  return `#/entities/${id}`;
}
