/**
 * @param {Record<string, unknown>} raw
 */
export function flattenEntitySnapshot(raw) {
  if (!raw || typeof raw !== "object") return {};
  const top = /** @type {Record<string, unknown>} */ (raw);
  const snap = top.snapshot && typeof top.snapshot === "object" ? top.snapshot : {};
  const s = /** @type {Record<string, unknown>} */ (snap);
  const inner = s.snapshot && typeof s.snapshot === "object" ? s.snapshot : {};
  const i = /** @type {Record<string, unknown>} */ (inner);
  return {
    ...top,
    ...s,
    ...i,
    entity_id: String(top.entity_id || s.entity_id || i.entity_id || ""),
    entity_type: String(top.entity_type || s.entity_type || i.entity_type || ""),
  };
}

/**
 * @param {Record<string, unknown>} issue
 */
export function heuristicClassify(issue) {
  const title = String(issue.title || issue.subject || "").toLowerCase();
  const body = String(issue.body || issue.description || "").toLowerCase();
  const text = `${title}\n${body}`;
  if (/\bduplicate\b|\balready (filed|reported)\b/.test(text)) {
    return { classification: "duplicate", confidence: 0.55, notes: "heuristic: duplicate cues" };
  }
  if (/\?|how (do|to)|what is|why does/.test(title) || text.includes("question")) {
    return { classification: "question", confidence: 0.5, notes: "heuristic: question cues" };
  }
  if (/\bdoc(s|umentation)?\b|\breadme\b/.test(text)) {
    return { classification: "documentation", confidence: 0.55, notes: "heuristic: documentation cues" };
  }
  if (/\bfeature\b|\benhancement\b|\brequest\b/.test(text)) {
    return { classification: "feature_request", confidence: 0.5, notes: "heuristic: feature cues" };
  }
  if (/\bout of scope\b|\bwon'?t fix\b/.test(text)) {
    return { classification: "out_of_scope", confidence: 0.55, notes: "heuristic: scope cues" };
  }
  if (/\bbug\b|\bcrash\b|\berror\b|\bfix\b|\bstack\b/.test(text)) {
    return { classification: "bug_fix", confidence: 0.55, notes: "heuristic: bug cues" };
  }
  return { classification: "bug_fix", confidence: 0.35, notes: "heuristic: default bug_fix" };
}

/**
 * @param {{
 *   issue: Record<string, unknown>;
 *   openaiApiKey?: string;
 *   openaiModel?: string;
 *   basePreview?: string;
 * }} opts
 */
export async function classifyIssue(opts) {
  const key = opts.openaiApiKey || process.env.OPENAI_API_KEY?.trim();
  const model = opts.openaiModel || process.env.OPENAI_CLASSIFIER_MODEL?.trim() || "gpt-4o-mini";
  if (!key) {
    return heuristicClassify(opts.issue);
  }
  const title = String(opts.issue.title || opts.issue.subject || "");
  const body = String(opts.issue.body || opts.issue.description || "").slice(0, 12000);
  const preview = opts.basePreview || "";
  const sys = `You classify Neotoma/GitHub issues for an automated daemon. Reply with JSON only: {"classification":"bug_fix|feature_request|documentation|question|duplicate|out_of_scope|needs_repro","confidence":0-1,"notes":"short"}. Use needs_repro when reporter git pin is missing or reproduction is impossible from text.`;
  const user = `Title: ${title}\n\nBody:\n${body}\n\nBase preview:\n${preview}`;
  const res = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model,
      temperature: 0.1,
      messages: [
        { role: "system", content: sys },
        { role: "user", content: user },
      ],
      response_format: { type: "json_object" },
    }),
  });
  const raw = await res.text();
  if (!res.ok) {
    return {
      ...heuristicClassify(opts.issue),
      notes: `openai_http_${res.status}; fallback. ${raw.slice(0, 120)}`,
    };
  }
  try {
    const j = JSON.parse(raw);
    const content = j?.choices?.[0]?.message?.content;
    const parsed = typeof content === "string" ? JSON.parse(content) : {};
    return {
      classification: String(parsed.classification || "bug_fix"),
      confidence: Number(parsed.confidence) || 0,
      notes: String(parsed.notes || ""),
    };
  } catch {
    return heuristicClassify(opts.issue);
  }
}
