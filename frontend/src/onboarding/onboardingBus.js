/** Tiny window-event bridge so the profile menu, help center and empty states can trigger onboarding
 * pieces (welcome, tour, a specific guide) without threading props through the shell. */
const EVENT = "bh:onboarding";

/** action: "welcome" | "tour" | "tour-resume" | "guide" (detail.guide = guide id). */
export function emitOnboarding(action, detail = {}) {
  window.dispatchEvent(new CustomEvent(EVENT, { detail: { action, ...detail } }));
}

export function onOnboarding(handler) {
  const listener = (e) => handler(e.detail);
  window.addEventListener(EVENT, listener);
  return () => window.removeEventListener(EVENT, listener);
}

// A guide requested before the Help view has mounted (it mounts on setView) waits here; a Help view
// that is already open hears the event and takes it straight away.
const GUIDE_EVENT = "bh:guide-requested";
let pendingGuide = null;
export function requestGuide(id) {
  pendingGuide = id;
  window.dispatchEvent(new Event(GUIDE_EVENT));
}
export function takePendingGuide() {
  const id = pendingGuide;
  pendingGuide = null;
  return id;
}
export function onGuideRequested(handler) {
  window.addEventListener(GUIDE_EVENT, handler);
  return () => window.removeEventListener(GUIDE_EVENT, handler);
}
