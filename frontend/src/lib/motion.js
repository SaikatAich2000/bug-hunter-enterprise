/** Shared motion settings. The CSS tokens in styles/motion.css use the same durations; keep them in step. */

/** Duration of every UI transition in ms (the CSS --motion token). */
export const MOTION_MS = 200;
const MOTION_EASE = "cubic-bezier(0.16, 1, 0.3, 1)";

/**
 * True when the person asked the system for less motion. Environments without matchMedia
 * (jsdom, very old browsers) count as reduced so nothing waits on an animation that cannot run.
 */
export function prefersReducedMotion() {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return true;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Drop animation for dnd-kit's DragOverlay: a short ease-out settle, or none under reduced motion. */
export function dragDropAnimation() {
  if (prefersReducedMotion()) return null;
  return { duration: MOTION_MS, easing: MOTION_EASE };
}

/**
 * Plays the exit animation (motion.css [data-leaving]) on rows about to be removed and resolves once it
 * has finished. Resolves at once under reduced motion or with nothing to animate. The resolved value
 * undoes the mark, for when the removal then fails.
 */
export async function exitRows(rows) {
  const els = [...rows].filter(Boolean);
  const undo = () => els.forEach((el) => el.removeAttribute("data-leaving"));
  if (!els.length || prefersReducedMotion()) return undo;
  els.forEach((el) => el.setAttribute("data-leaving", ""));
  await new Promise((r) => setTimeout(r, MOTION_MS));
  return undo;
}
