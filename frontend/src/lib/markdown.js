/** Small Markdown renderer for the bundled help guides.
 *
 * Supports headings (#..####), paragraphs, bullet and numbered lists, block quotes, fenced code,
 * tables, horizontal rules, **bold**, *italic*, `code` and [links](url). Everything is HTML-escaped
 * first and the result goes through DOMPurify with a short allow-list, so even a hostile guide
 * cannot inject markup. Links must be http(s), mailto or an in-app "#help/..." anchor.
 */
import DOMPurify from "dompurify";

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => s.replace(/[&<>"']/g, (c) => ESC[c]);

function slugify(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

function safeHref(href) {
  const h = href.trim();
  return /^(https?:\/\/|mailto:|#)/i.test(h) ? h : "";
}

/** Inline formatting on text that is already escaped. */
function inline(text) {
  const codes = [];
  let out = text.replace(/`([^`]+)`/g, (_, c) => {
    codes.push(c);
    return `\uE000${codes.length - 1}\uE000`;
  });
  out = out
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, href) => {
      const url = safeHref(href.replace(/&amp;/g, "&"));
      if (!url) return label;
      const ext = /^https?:/i.test(url);
      return `<a href="${esc(url)}"${ext ? ' target="_blank" rel="noopener noreferrer"' : ""}>${label}</a>`;
    });
  return out.replace(/\uE000(\d+)\uE000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
}

// U+E000 (private use) delimits the inline-code placeholders, so it is stripped from the input first.
const cleanInput = (md) => String(md ?? "").replace(/\uE000/g, "").replace(/\r\n?/g, "\n");

function splitRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l) && l.includes("-");
const isBlockStart = (l) => /^(#{1,4}\s|>\s?|\s*[-*]\s|\s*\d+\.\s|```|---+\s*$)/.test(l);

export function markdownToHtml(md) {
  const lines = cleanInput(md).split("\n");
  const html = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    if (line.startsWith("```")) {
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]);
      i++;
      html.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }

    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      const level = h[1].length;
      html.push(`<h${level} id="g-${slugify(h[2])}">${inline(esc(h[2]))}</h${level}>`);
      i++;
      continue;
    }

    if (/^---+\s*$/.test(line)) { html.push("<hr>"); i++; continue; }

    if (line.startsWith(">")) {
      const buf = [];
      while (i < lines.length && lines[i].startsWith(">")) buf.push(lines[i++].replace(/^>\s?/, ""));
      html.push(`<blockquote>${inline(esc(buf.join(" ")))}</blockquote>`);
      continue;
    }

    if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(splitRow(lines[i++]));
      const cell = (tag, c) => `<${tag}>${inline(esc(c))}</${tag}>`;
      html.push(
        `<table><thead><tr>${head.map((c) => cell("th", c)).join("")}</tr></thead><tbody>` +
        rows.map((r) => `<tr>${r.map((c) => cell("td", c)).join("")}</tr>`).join("") +
        "</tbody></table>",
      );
      continue;
    }

    const listMatch = /^\s*([-*]|\d+\.)\s+/.exec(line);
    if (listMatch) {
      const ordered = /\d/.test(listMatch[1]);
      const items = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        let text = lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, "");
        while (i < lines.length && lines[i].trim() && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
          text += ` ${lines[i++].trim()}`;
        }
        items.push(`<li>${inline(esc(text))}</li>`);
      }
      html.push(`<${ordered ? "ol" : "ul"}>${items.join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }

    const buf = [line.trim()];
    i++;
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) buf.push(lines[i++].trim());
    html.push(`<p>${inline(esc(buf.join(" ")))}</p>`);
  }
  return sanitizeGuide(html.join("\n"));
}

function sanitizeGuide(html) {
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: ["h1", "h2", "h3", "h4", "p", "ul", "ol", "li", "strong", "em", "code", "pre", "a",
      "blockquote", "hr", "table", "thead", "tbody", "tr", "th", "td", "br"],
    ALLOWED_ATTR: ["href", "rel", "id"],
    ADD_ATTR: ["target"],
    ALLOW_DATA_ATTR: false,
  });
}

/** Plain-text outline of headings (level 2 and 3) for a guide's table of contents. */
export function headingsOf(md) {
  const out = [];
  let inFence = false;
  for (const line of cleanInput(md).split("\n")) {
    if (line.startsWith("```")) inFence = !inFence;
    const m = !inFence && /^(#{2,3})\s+(.*)$/.exec(line);
    if (m) out.push({ level: m[1].length, text: m[2], id: `g-${slugify(m[2])}` });
  }
  return out;
}
