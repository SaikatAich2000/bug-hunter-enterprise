/** Keeps Tab inside `ref` while `active`, moves focus in on activation and puts it back on the
 * element that had it before. Used by the tour card; Modal has its own copy of the same idea. */
import { useEffect } from "react";

const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

export function useFocusTrap(ref, active, focusKey) {
  useEffect(() => {
    if (!active) return undefined;
    const previous = document.activeElement;
    return () => {
      if (previous && previous.isConnected && typeof previous.focus === "function") previous.focus({ preventScroll: true });
    };
  }, [active]);

  // Focus moves to the card whenever the step changes so screen readers announce it.
  useEffect(() => {
    if (active) ref.current?.focus({ preventScroll: true });
  }, [active, focusKey, ref]);

  useEffect(() => {
    if (!active) return undefined;
    const onKey = (e) => {
      const root = ref.current;
      if (e.key !== "Tab" || !root) return;
      const items = [...root.querySelectorAll(FOCUSABLE)];
      if (!items.length) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const here = document.activeElement;
      if (!root.contains(here)) {
        e.preventDefault();
        first.focus();
      } else if (e.shiftKey && (here === first || here === root)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && here === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [active, ref]);
}
