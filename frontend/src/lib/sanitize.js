/** Render-time sanitization for dangerouslySetInnerHTML — second line of defense vs stored XSS. */
import DOMPurify from "dompurify";

export function sanitizeHtml(html) {
  // keep target="_blank" emitted by the rich editor
  return DOMPurify.sanitize(html ?? "", { ADD_ATTR: ["target"] });
}

/** Stricter variant for Sleuth chat: only mdLite tags; data-open-bug kept for bug links. */
export function sanitizeSleuth(html) {
  return DOMPurify.sanitize(html ?? "", {
    ALLOWED_TAGS: ["a", "strong", "em", "code", "ul", "li", "p", "br"],
    ALLOWED_ATTR: ["href", "target", "rel", "data-open-bug"],
    ALLOW_DATA_ATTR: false,
  });
}
