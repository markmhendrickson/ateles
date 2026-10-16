/**
 * MINIMAL MARKDOWN RENDERER
 * -------------------------
 * `prompt_markdown` runs long (the Ateles agent's is ~9,800 characters), and a
 * pre-wrapped monospace block that size is legible but not readable — the
 * structure is what makes a prompt scannable.
 *
 * Rather than pull in a markdown library for one field, this renders the block
 * level structure the prompts actually use: headings, bullet and numbered
 * lists, fenced code, and paragraphs, plus inline code/bold/italic and links.
 * Anything it does not recognize falls through as a paragraph, so no content is
 * ever dropped — the worst case is that a rare construct renders as plain text.
 *
 * Everything goes through React elements, never `dangerouslySetInnerHTML`, so
 * prompt text cannot inject markup into the page.
 */
import type { ReactNode } from "react";

/** Split on inline spans: `code`, **bold**, *italic*, and [text](url). */
function renderInline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  // One alternation pass so the earliest match always wins, avoiding the
  // nested-replacement bugs that come from running each pattern separately.
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)\s]+\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;

  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    const key = `${keyBase}-i${i++}`;

    if (tok.startsWith("`")) {
      out.push(<code key={key}>{tok.slice(1, -1)}</code>);
    } else if (tok.startsWith("**")) {
      out.push(<strong key={key}>{tok.slice(2, -2)}</strong>);
    } else if (tok.startsWith("[")) {
      const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(tok);
      // Only http(s) links become anchors; anything else stays literal text so
      // a `javascript:` URL in stored content can never become clickable.
      if (link && /^https?:\/\//i.test(link[2])) {
        out.push(
          <a key={key} href={link[2]} target="_blank" rel="noreferrer">
            {link[1]}
          </a>,
        );
      } else {
        out.push(tok);
      }
    } else {
      out.push(<em key={key}>{tok.slice(1, -1)}</em>);
    }
    last = m.index + tok.length;
  }

  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ source }: { source: string }) {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];

  // Buffers for the multi-line constructs being accumulated.
  let para: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let code: string[] | null = null;
  let k = 0;

  const flushPara = () => {
    if (!para.length) return;
    const text = para.join(" ");
    blocks.push(<p key={`p${k++}`}>{renderInline(text, `p${k}`)}</p>);
    para = [];
  };

  const flushList = () => {
    if (!list) return;
    const { ordered, items } = list;
    const kids = items.map((it, n) => <li key={n}>{renderInline(it, `l${k}-${n}`)}</li>);
    blocks.push(ordered ? <ol key={`l${k++}`}>{kids}</ol> : <ul key={`l${k++}`}>{kids}</ul>);
    list = null;
  };

  const flushAll = () => {
    flushPara();
    flushList();
  };

  for (const raw of lines) {
    const line = raw.trimEnd();

    // Fenced code: everything between fences is verbatim, so this is checked
    // before any other construct.
    if (/^\s*```/.test(line)) {
      if (code) {
        blocks.push(
          <pre key={`c${k++}`}>
            <code>{code.join("\n")}</code>
          </pre>,
        );
        code = null;
      } else {
        flushAll();
        code = [];
      }
      continue;
    }
    if (code) {
      code.push(raw);
      continue;
    }

    if (!line.trim()) {
      flushAll();
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushAll();
      const depth = heading[1].length;
      // Prompts start at `#`, but this block sits inside the page's own
      // hierarchy — demote so the document outline stays sane.
      const Tag = (["h3", "h4", "h5", "h6", "h6", "h6"] as const)[depth - 1];
      blocks.push(<Tag key={`h${k++}`}>{renderInline(heading[2], `h${k}`)}</Tag>);
      continue;
    }

    // Horizontal rule.
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flushAll();
      blocks.push(<hr key={`r${k++}`} />);
      continue;
    }

    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      const ordered = Boolean(numbered);
      const item = (bullet ?? numbered)![1];
      flushPara();
      // A change of list kind starts a new list rather than mixing markers.
      if (list && list.ordered !== ordered) flushList();
      if (!list) list = { ordered, items: [] };
      list.items.push(item);
      continue;
    }

    // A continuation line inside a list item belongs to that item.
    if (list) {
      list.items[list.items.length - 1] += ` ${line.trim()}`;
      continue;
    }

    para.push(line.trim());
  }

  // Close anything still open at EOF — an unterminated fence still renders.
  if (code) {
    blocks.push(
      <pre key={`c${k++}`}>
        <code>{code.join("\n")}</code>
      </pre>,
    );
  }
  flushAll();

  return <div className="md">{blocks}</div>;
}
