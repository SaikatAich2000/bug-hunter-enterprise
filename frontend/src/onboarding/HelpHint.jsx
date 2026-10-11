/** A small "?" next to a complex field: opens a short explanation with a link to the full guide.
 * Keyboard: Enter/Space toggles, Escape closes and returns focus to the button. */
import { useEffect, useId, useRef, useState } from "react";
import { useOpenGuide } from "./useOpenGuide";

export default function HelpHint({ label, children, guide }) {
  const [open, setOpen] = useState(false);
  const popId = useId();
  const rootRef = useRef(null);
  const btnRef = useRef(null);
  const openGuide = useOpenGuide();

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    };
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
        btnRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  return (
    <span className="help-hint" ref={rootRef}>
      <button
        type="button"
        ref={btnRef}
        className="help-hint-btn"
        aria-label={`Help: ${label}`}
        aria-expanded={open}
        aria-controls={open ? popId : undefined}
        onClick={() => setOpen((o) => !o)}
      >
        ?
      </button>
      {open && (
        <span id={popId} className="help-hint-pop" role="region" aria-label={label}>
          <span className="help-hint-text">{children}</span>
          {guide && (
            <button
              type="button"
              className="help-hint-more"
              onClick={() => {
                setOpen(false);
                // The Help view replaces the page, so any open dialog should not stay behind it.
                document.querySelectorAll(".modal:not([hidden]) .modal-close").forEach((b) => b.click());
                openGuide(guide);
              }}
            >
              Read the guide
            </button>
          )}
        </span>
      )}
    </span>
  );
}
