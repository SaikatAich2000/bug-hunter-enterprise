/** Help center: bundled guides (Markdown) with a search box, a contents list and in-guide links.
 * Navigation never touches the URL hash, which the app reserves for notification deep links. */
import { useEffect, useMemo, useRef, useState } from "react";
import { markdownToHtml, headingsOf } from "../lib/markdown";
import { GUIDES, findGuide, searchGuides } from "./guides";
import { onGuideRequested, takePendingGuide } from "./onboardingBus";
import { emitOnboarding } from "./onboardingBus";

export default function HelpCenter() {
  const [query, setQuery] = useState("");
  const [guideId, setGuideId] = useState(() => takePendingGuide() ?? GUIDES[0]?.id);
  const articleRef = useRef(null);
  const userPicked = useRef(false);

  useEffect(() => onGuideRequested(() => {
    const id = takePendingGuide();
    if (id && findGuide(id)) {
      userPicked.current = true;
      setGuideId(id);
      setQuery("");
    }
  }), []);

  const shown = useMemo(() => searchGuides(query), [query]);
  const guide = findGuide(guideId) ?? shown[0] ?? GUIDES[0];
  const html = useMemo(() => (guide ? markdownToHtml(guide.markdown) : ""), [guide]);
  const toc = useMemo(() => (guide ? headingsOf(guide.markdown) : []), [guide]);

  // After a person picks a guide, move focus to it so keyboard and screen-reader users land on the text.
  useEffect(() => {
    if (userPicked.current) {
      userPicked.current = false;
      articleRef.current?.focus({ preventScroll: true });
    }
  }, [guide?.id]);

  const pick = (id) => {
    userPicked.current = true;
    setGuideId(id);
  };

  const onArticleClick = (e) => {
    const a = e.target.closest?.("a[href]");
    if (!a) return;
    const href = a.getAttribute("href") || "";
    if (href.startsWith("#help/")) {
      e.preventDefault();
      const id = href.slice("#help/".length);
      if (findGuide(id)) pick(id);
    } else if (href.startsWith("#")) {
      e.preventDefault();
      document.getElementById(href.slice(1))?.scrollIntoView?.({ block: "start" });
    }
  };

  return (
    <section className="help-center" id="viewHelp" aria-label="Help center">
      <aside className="help-side">
        <div className="field help-search">
          <label htmlFor="helpSearch" className="sr-only">Search the guides</label>
          <input
            id="helpSearch"
            type="search"
            placeholder="Search the guides…"
            autoComplete="off"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <nav aria-label="Guides">
          <ul className="help-list">
            {shown.map((g) => (
              <li key={g.id}>
                <button
                  type="button"
                  className={`help-list-item${g.id === guide?.id ? " active" : ""}`}
                  aria-current={g.id === guide?.id ? "page" : undefined}
                  onClick={() => pick(g.id)}
                >
                  {g.title}
                </button>
              </li>
            ))}
          </ul>
          {shown.length === 0 && <p className="help-none muted" role="status">No guide matches &ldquo;{query}&rdquo;.</p>}
        </nav>
        <div className="help-side-actions">
          <button type="button" className="btn ghost" onClick={() => emitOnboarding("tour")}>Take the product tour</button>
        </div>
      </aside>

      <article className="help-article" key={guide?.id} ref={articleRef} tabIndex={-1} aria-label={guide?.title}>
        {toc.length > 1 && (
          <nav className="help-toc" aria-label="On this page">
            {toc.filter((h) => h.level === 2).map((h) => (
              <button
                key={h.id}
                type="button"
                className="help-toc-link"
                onClick={() => document.getElementById(h.id)?.scrollIntoView?.({ block: "start" })}
              >
                {h.text}
              </button>
            ))}
          </nav>
        )}
        {/* html comes from markdownToHtml, which escapes and sanitizes it */}
        <div className="help-md" onClick={onArticleClick} role="presentation" dangerouslySetInnerHTML={{ __html: html }} />
      </article>
    </section>
  );
}
