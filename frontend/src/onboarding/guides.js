/** Bundled help guides. Each Markdown file in ./guides is one guide: the file name gives its order
 * and id ("02-sprints.md" is "sprints"), the first "# " heading its title, the first paragraph
 * its summary. They are bundled at build time, so help works offline and never touches the API. */
const files = import.meta.glob("./guides/*.md", { query: "?raw", import: "default", eager: true });

function firstParagraph(md) {
  for (const block of md.split(/\n\s*\n/)) {
    const text = block.trim();
    if (text && !text.startsWith("#")) return text.replace(/\s+/g, " ");
  }
  return "";
}

export const GUIDES = Object.entries(files)
  .sort(([a], [b]) => a.localeCompare(b))
  .map(([path, md]) => {
    const id = path.replace(/^.*\/\d*-?/, "").replace(/\.md$/, "");
    const title = /^#\s+(.+)$/m.exec(md)?.[1] ?? id;
    return { id, title, summary: firstParagraph(md), markdown: md };
  });

export function findGuide(id) {
  return GUIDES.find((g) => g.id === id) ?? null;
}

/** Guides whose title or text contains every word of the query. Empty query returns all. */
export function searchGuides(query) {
  const words = String(query ?? "").toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return GUIDES;
  return GUIDES.filter((g) => {
    const hay = `${g.title}\n${g.markdown}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}
