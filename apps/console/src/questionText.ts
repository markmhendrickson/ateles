/**
 * QUESTION PROSE → MARKDOWN
 * -------------------------
 * Question `description`, `details` and `result` are authored as PLAIN TEXT
 * with newlines — not markdown. They carry real structure all the same:
 * paragraph breaks, lettered options `(a)`/`(b)`/`(c)`, ALL-CAPS section leads
 * like `THE FINDING:` and `RECOMMENDATION:`, and occasionally an indented
 * code-ish block.
 *
 * The detail view renders them through the app's existing Markdown renderer
 * (see Markdown.tsx), which is the right call for two reasons: some of this
 * text does contain genuine markdown — a bullet list, a fenced block, a
 * backticked identifier such as `assigned_to` — and rendering it as markdown
 * costs nothing when it does not.
 *
 * But a markdown renderer joins consecutive non-blank lines into ONE paragraph.
 * Plain text that used single newlines as its paragraph separator would come
 * out as a wall — strictly worse than the raw text. That is what this module
 * exists to prevent.
 *
 * THE RULE: a single newline is preserved as a hard break, by promoting it to a
 * blank line, UNLESS the following line continues a construct where the
 * renderer's own line joining is correct — a list item's wrapped continuation,
 * or anything inside a fenced code block, which is passed through verbatim.
 *
 * Nothing is ever deleted. Worst case a construct renders as its own paragraph,
 * which is exactly how it reads in the stored text.
 */

/** ```fence``` — everything between a pair is verbatim, so we never touch it. */
const FENCE = /^\s*```/;

/** A markdown block that legitimately owns the lines wrapped beneath it. */
const LIST_ITEM = /^\s*(?:[-*+]|\d+[.)])\s+/;

/** A heading, rule, or blockquote: block-level, already its own paragraph. */
const BLOCK_LEAD = /^\s*(?:#{1,6}\s|>|(?:[-*_])(?:\s*[-*_]){2,}\s*$)/;

/**
 * An indented block — two or more leading spaces on a non-list line. In plain
 * text this is the "code-ish block" convention these questions sometimes use,
 * and the surrounding lines belong together, so the whole run is fenced rather
 * than reflowed. Markdown's own 4-space rule is deliberately loosened to 2:
 * the stored text indents by eye, not by spec.
 */
const INDENTED = /^\s{2,}\S/;

/**
 * Promote plain-text line breaks so the existing Markdown renderer preserves
 * the structure the author actually typed.
 */
export function toMarkdown(source: string): string {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  let inFence = false;
  let inIndent = false;
  // Inside a markdown list, an indented line is a bullet's wrapped
  // continuation, NOT a code block. Tracked so the indent rule below does not
  // tear a wrapped bullet out of its list. Reset by a blank line, which ends
  // the list run.
  let inList = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Verbatim region: copy through untouched, including blank lines.
    if (FENCE.test(line)) {
      if (inIndent) {
        // An explicit fence supersedes an indented run we had opened.
        out.push("```");
        inIndent = false;
      }
      inFence = !inFence;
      out.push(line);
      continue;
    }
    if (inFence) {
      out.push(line);
      continue;
    }

    const blank = !line.trim();

    // Track the surrounding list run before the indent rule consults it.
    if (blank) inList = false;
    else if (LIST_ITEM.test(line)) inList = true;
    else if (!INDENTED.test(line)) inList = false;

    // An indented run becomes a real fenced block, so the renderer keeps its
    // whitespace instead of reflowing it into a paragraph. Suppressed inside a
    // list, where an indented line is a wrapped bullet the renderer already
    // joins correctly.
    if (!inIndent && !inList && INDENTED.test(line) && !LIST_ITEM.test(line)) {
      // A blank line before it, if any, is already in `out`.
      out.push("```");
      inIndent = true;
    } else if (inIndent && !INDENTED.test(line) && !blank) {
      out.push("```");
      inIndent = false;
    }
    if (inIndent) {
      out.push(line);
      continue;
    }

    out.push(line);
    if (blank) continue;

    // Decide whether the NEXT line should start a new block. A blank line is
    // inserted between them when it should — that is what stops the renderer
    // from joining two typed lines into one paragraph.
    const next = lines[i + 1];
    if (next === undefined || !next.trim()) continue;

    // The next line already opens its own block: the renderer will break there
    // on its own, and an extra blank would only add vertical space.
    if (BLOCK_LEAD.test(next) || FENCE.test(next)) continue;

    // A list item's wrapped continuation. Markdown's join is CORRECT here, so
    // leave it: breaking it would split one bullet into two blocks and end the
    // list. Applies while the run continues, including a continuation line that
    // itself wraps onto another.
    // Two adjacent items belong to ONE list; a blank line between them would
    // split it into a run of single-item lists.
    if (inList) continue;

    // Everything else: the author's line break was meaningful. Keep it.
    out.push("");
  }

  if (inIndent) out.push("```");
  if (inFence) out.push("```");

  return out.join("\n");
}

/**
 * Split a stored recommendation or description into a lead-in and the portion
 * introduced by an ALL-CAPS `RECOMMENDATION:` marker.
 *
 * Question #5 carries its recommendation INSIDE the description as well as in
 * `details`, and that inline portion is the agent's opinion — it must not read
 * as part of the operator's brief, and it must not read as a decision. Pulling
 * it out lets the detail view give it the same amber treatment as the `details`
 * recommendation instead of burying it mid-paragraph.
 *
 * Returns the recommendation as null when there is no such marker, which is the
 * common case.
 */
export function splitRecommendation(text: string): { body: string; recommendation: string | null } {
  // TWO AUTHORED FORMS, both anchored to a line start so the word appearing
  // mid-sentence is never a hit:
  //
  //   RECOMMENDATION: …          the plain-text convention
  //   **Recommendation:** …      markdown, used by every question filed since
  //
  // The markdown form is the MAJORITY of live questions (six of eleven at the
  // time of writing, none of which carry a `details` field), so matching only
  // the ALL-CAPS form left most recommendations invisible on the card. The
  // emphasis markers are consumed along with the label — the UI supplies its
  // own heading, and a leading `**` would otherwise open an unclosed bold run.
  const m =
    /^[ \t]*RECOMMENDATION[ \t]*:[ \t]*/m.exec(text) ??
    /^[ \t]*\*{1,2}Recommendation:?\*{0,2}[ \t]*:?[ \t]*/im.exec(text);
  if (!m) return { body: text, recommendation: null };

  const body = text.slice(0, m.index).trimEnd();
  const recommendation = text.slice(m.index + m[0].length).trim();
  // A marker with nothing after it is not a recommendation.
  if (!recommendation) return { body: text, recommendation: null };
  // A marker at the very top means the whole field IS the recommendation.
  return { body, recommendation };
}
