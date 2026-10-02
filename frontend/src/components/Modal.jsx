/** Generic modal wrapper; open/close via `hidden`. Escape handled centrally in App.jsx (top-most first).
 *
 * Accessibility: the card is a modal dialog labelled by its title. Opening it
 * moves focus into it (to an element marked `data-autofocus`, else the card
 * itself), Tab and Shift+Tab stay inside it, and closing it returns focus to
 * where it was. */
import { useEffect, useId, useRef } from "react";

const FOCUSABLE = [
  "a[href]", "button:not([disabled])", "input:not([disabled]):not([type=hidden])", "select:not([disabled])",
  "textarea:not([disabled])", "[tabindex]:not([tabindex='-1'])", "[contenteditable='true']",
].join(",");

function focusables(root) {
  return [...root.querySelectorAll(FOCUSABLE)].filter(
    (el) => !el.closest("[hidden]") && el.getClientRects().length > 0,
  );
}

export default function Modal({
  id,
  open,
  title,
  subtitle,
  size = "",
  cardClass = "",
  headExtra,
  onClose,
  children,
}) {
  const titleId = useId();
  const cardRef = useRef(null);
  const returnTo = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    returnTo.current = document.activeElement;
    // After this render, so fields seeded on open are in place.
    const frame = requestAnimationFrame(() => {
      const card = cardRef.current;
      if (!card || card.contains(document.activeElement)) return;
      const target = card.querySelector("[data-autofocus]") || card;
      target.focus({ preventScroll: true });
    });
    return () => {
      cancelAnimationFrame(frame);
      const back = returnTo.current;
      returnTo.current = null;
      if (back && back.isConnected && typeof back.focus === "function") back.focus({ preventScroll: true });
    };
  }, [open]);

  const onKeyDown = (e) => {
    if (e.key !== "Tab" || !cardRef.current) return;
    const items = focusables(cardRef.current);
    if (!items.length) {
      e.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === cardRef.current)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="modal" id={id} hidden={!open} data-bh-modal>
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- Tab trap for the dialog */}
      <div
        ref={cardRef}
        className={`modal-card ${size} ${cardClass}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <div className="modal-head">
          <div className="modal-head-text">
            <h2 id={titleId}>{title}</h2>
            {subtitle != null && <p className="modal-subtitle">{subtitle}</p>}
          </div>
          {headExtra}
          <button
            type="button"
            className="icon-btn modal-close"
            aria-label="Close"
            onClick={onClose}
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
