/** Markdown renderer for the guides: formatting works and hostile input stays inert. */
import { describe, expect, it } from "vitest";

import { headingsOf, markdownToHtml } from "./markdown.js";

describe("markdownToHtml", () => {
  it("renders headings, lists, emphasis and inline code", () => {
    const html = markdownToHtml("# T\n\n## Sub\n\n- **a** and *b*\n- `c`\n\n1. one\n2. two");
    expect(html).toContain('<h1 id="g-t">T</h1>');
    expect(html).toContain('<h2 id="g-sub">Sub</h2>');
    expect(html).toContain("<li><strong>a</strong> and <em>b</em></li>");
    expect(html).toContain("<li><code>c</code></li>");
    expect(html).toContain("<ol><li>one</li><li>two</li></ol>");
  });

  it("renders tables and fenced code verbatim", () => {
    const html = markdownToHtml("| A | B |\n|---|---|\n| 1 | 2 |\n\n```\n<b>x</b>\n```");
    expect(html).toContain("<th>A</th>");
    expect(html).toContain("<td>2</td>");
    expect(html).toContain("&lt;b&gt;x&lt;/b&gt;");
  });

  it("escapes raw HTML and scripts", () => {
    const html = markdownToHtml('<script>alert(1)</script> <img src=x onerror="alert(2)">');
    expect(html).not.toContain("<script");
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;script&gt;");
  });

  it("drops unsafe link targets but keeps the text", () => {
    const html = markdownToHtml("[bad](javascript:alert(1)) [ok](https://example.com) [in](#help/items)");
    expect(html).not.toContain("javascript:");
    expect(html).toContain("bad");
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('href="#help/items"');
  });

  it("strips the internal placeholder character from input", () => {
    expect(markdownToHtml("a 0 `x`")).toContain("<code>x</code>");
  });

  it("lists level-2 headings, ignoring fenced code", () => {
    const h = headingsOf("# T\n## One\n```\n## no\n```\n### Deep\n## Two");
    expect(h.map((x) => x.text)).toEqual(["One", "Deep", "Two"]);
  });
});
