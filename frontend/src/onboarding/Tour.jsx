/** Coach-mark tour: a spotlight on the control a step is about, plus a card with Back / Next / Skip.
 *
 * Accessible as a modal dialog: focus moves to the card on every step, Tab stays inside it, Escape
 * pauses the tour (it can be resumed from the account menu) and focus returns to where it was. */
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { useFocusTrap } from "./useFocusTrap";

const GAP = 12;
const MARGIN = 12;

function measure(selector) {
  // A selector may list alternatives (a top-bar button on phones, a floating one on desktop): take the one on screen.
  const rects = selector ? [...document.querySelectorAll(selector)].map((el) => el.getBoundingClientRect()) : [];
  const r = rects.find((b) => b.width > 0 && b.height > 0);
  if (!r) return null;
  return { top: r.top, left: r.left, width: r.width, height: r.height };
}

export function placeCard(target, card, viewport) {
  const w = Math.min(card.width, viewport.width - MARGIN * 2);
  if (!target) {
    return { left: Math.max(MARGIN, (viewport.width - w) / 2), top: Math.max(MARGIN, (viewport.height - card.height) / 3), width: w };
  }
  let top = target.top + target.height + GAP;
  if (top + card.height > viewport.height - MARGIN) top = Math.max(MARGIN, target.top - card.height - GAP);
  const centre = target.left + target.width / 2 - w / 2;
  const left = Math.min(Math.max(MARGIN, centre), viewport.width - w - MARGIN);
  return { left, top, width: w };
}

export default function Tour({ steps, index, onIndex, onPause, onFinish }) {
  const step = steps[index];
  const titleId = useId();
  const bodyId = useId();
  const cardRef = useRef(null);
  const [target, setTarget] = useState(null);
  const [pos, setPos] = useState({ left: MARGIN, top: MARGIN, width: 340 });

  useFocusTrap(cardRef, true, index);

  // Re-measure on step change, resize and scroll (the page can move under the spotlight).
  useLayoutEffect(() => {
    const update = () => {
      const t = measure(step.selector);
      setTarget(t);
      const card = cardRef.current;
      setPos(placeCard(
        t,
        { width: 340, height: card ? card.offsetHeight : 180 },
        { width: window.innerWidth, height: window.innerHeight },
      ));
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [step]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onPause();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onPause]);

  const last = index === steps.length - 1;

  return (
    <div className="tour-root" data-tour-step={step.id}>
      <div className="tour-backdrop" aria-hidden="true" />
      {target && (
        <div
          className="tour-spot"
          aria-hidden="true"
          style={{
            width: target.width + 8,
            height: target.height + 8,
            transform: `translate(${target.left - 4}px, ${target.top - 4}px)`,
          }}
        />
      )}
      <div
        ref={cardRef}
        className="tour-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        tabIndex={-1}
        style={{ left: pos.left, top: pos.top, width: pos.width }}
      >
        <p className="tour-progress">Step {index + 1} of {steps.length}</p>
        <h2 id={titleId} className="tour-title">{step.title}</h2>
        <p id={bodyId} className="tour-body">{step.body}</p>
        <div className="tour-actions">
          <button type="button" className="btn ghost" onClick={onPause}>Skip tour</button>
          <span className="grow" />
          <button type="button" className="btn" disabled={index === 0} onClick={() => onIndex(index - 1)}>Back</button>
          <button
            type="button"
            className="btn primary"
            onClick={() => (last ? onFinish() : onIndex(index + 1))}
          >
            {last ? "Finish" : "Next"}
          </button>
        </div>
      </div>
    </div>
  );
}
