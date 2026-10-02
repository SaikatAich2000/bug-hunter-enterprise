// BhDateInput — controlled custom date picker; bh-date-* class names must match styles.css.
// Popover is portaled to <body> so transformed ancestors can't break its fixed positioning.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

const DOW = ["S", "M", "T", "W", "T", "F", "S"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** YYYY-MM-DD in local time. */
function isoDate(d) {
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

/** Parse YYYY-MM-DD  time; round-trip check rejects dates JS would roll over. */
function parseIso(s) {
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const [y, m, d] = s.split("-").map((n) => Number.parseInt(n, 10));
  const date = new Date(y, m - 1, d);
  if (isoDate(date) !== s) return null;
  return date;
}

/** "Jun 12, 2026". */
function formatHumanDate(s) {
  const d = parseIso(s);
  if (!d) return "";
  return `${MONTHS[d.getMonth()].slice(0, 3)} ${d.getDate()}, ${d.getFullYear()}`;
}

/** One calendar cell; muted = prev/next-month bleed. */
function computeCell(offset, daysInMonth, daysInPrev, viewYear, viewMonth) {
  if (offset < 0) {
    let cellMonth = viewMonth - 1;
    let cellYear = viewYear;
    if (cellMonth < 0) { cellMonth = 11; cellYear--; }
    return { dayNum: daysInPrev + offset + 1, cellMonth, cellYear, muted: true };
  }
  if (offset >= daysInMonth) {
    let cellMonth = viewMonth + 1;
    let cellYear = viewYear;
    if (cellMonth > 11) { cellMonth = 0; cellYear++; }
    return { dayNum: offset - daysInMonth + 1, cellMonth, cellYear, muted: true };
  }
  return { dayNum: offset + 1, cellMonth: viewMonth, cellYear: viewYear, muted: false };
}

export default function BhDateInput({ name, value, onChange, required, disabled, id, min, max, ariaLabel, ariaLabelledBy }) {
  const [open, setOpen] = useState(false);
  // Viewed month is independent of the value (browse without committing); re-seeded on open.
  const [view, setView] = useState(() => {
    const seed = parseIso(value) ?? new Date();
    return { y: seed.getFullYear(), m: seed.getMonth() };
  });
  const wrapRef = useRef(null);
  const btnRef = useRef(null);
  const popRef = useRef(null);

  // Align to the trigger button, flipping above when below would clip.
  const placePop = useCallback(() => {
    const btn = btnRef.current;
    const pop = popRef.current;
    if (!btn || !pop) return;
    const r = btn.getBoundingClientRect();
    const vh = window.innerHeight;
    const vw = window.innerWidth;
    const ph = pop.offsetHeight || 360;
    const pw = pop.offsetWidth || 320;
    const spaceBelow = vh - r.bottom;
    const spaceAbove = r.top;
    const openAbove = spaceBelow < ph + 12 && spaceAbove > spaceBelow;
    pop.style.position = "fixed";
    pop.style.top = openAbove
      ? `${Math.max(8, r.top - ph - 6)}px`
      : `${Math.min(vh - ph - 8, r.bottom + 6)}px`;
    let left = r.left;
    if (left + pw > vw - 8) left = Math.max(8, r.right - pw);
    pop.style.left = `${left}px`;
  }, []);

  // Place before paint, re-place next frame (late layout shifts), track scroll/resize.
  useLayoutEffect(() => {
    if (!open) return;
    placePop();
    const raf = requestAnimationFrame(placePop);
    window.addEventListener("resize", placePop);
    window.addEventListener("scroll", placePop, true);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", placePop);
      window.removeEventListener("scroll", placePop, true);
    };
  }, [open, placePop]);

  // Outside click / Escape closes (capture-phase Escape so a modal doesn't close).
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e) => {
      const t = e.target instanceof Node ? e.target : null;
      if (
        t &&
        ((wrapRef.current?.contains(t) ?? false) || (popRef.current?.contains(t) ?? false))
      ) {
        return;
      }
      setOpen(false);
    };
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setOpen(false);
      btnRef.current?.focus();
    };
    document.addEventListener("click", onDocClick);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("click", onDocClick);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  const openPop = () => {
    const seed = parseIso(value) ?? new Date();
    setView({ y: seed.getFullYear(), m: seed.getMonth() });
    setOpen(true);
  };

  const commit = (iso) => {
    if ((min && iso < min) || (max && iso > max)) return;
    onChange(iso);
    setOpen(false);
  };

  const navMonth = (delta) => {
    setView((v) => {
      let m = v.m + delta;
      let y = v.y;
      if (m < 0) { m = 11; y--; }
      if (m > 11) { m = 0; y++; }
      return { y, m };
    });
  };

  // 6×7 = 42-cell grid covers any month.
  const startDow = new Date(view.y, view.m, 1).getDay(); // 0=Sun
  const daysInMonth = new Date(view.y, view.m + 1, 0).getDate();
  const daysInPrev = new Date(view.y, view.m, 0).getDate();
  const todayIso = isoDate(new Date());
  const cells = [];
  for (let i = 0; i < 42; i++) {
    const { dayNum, cellMonth, cellYear, muted } =
      computeCell(i - startDow, daysInMonth, daysInPrev, view.y, view.m);
    const iso = isoDate(new Date(cellYear, cellMonth, dayNum));
    const cls = [
      "bh-date-cell",
      muted ? "muted" : "",
      iso === todayIso ? "is-today" : "",
      iso === value ? "is-selected" : "",
    ].filter(Boolean).join(" ");
    cells.push({ iso, dayNum, cls });
  }

  return (
    <div className="bh-date-wrap" ref={wrapRef}>
      {/* Hidden input carries name/value for form submission. */}
      <input
        type="hidden"
        className="bh-date-native"
        name={name}
        id={id}
        value={value}
        required={required}
        disabled={disabled}
      />
      <button
        type="button"
        className="bh-date-btn"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledBy}
        disabled={disabled}
        ref={btnRef}
        onClick={() => {
          if (open) setOpen(false);
          else openPop();
        }}
      >
        <span className="bh-date-icon" aria-hidden="true">📅</span>
        <span className={`bh-date-label${value ? "" : " bh-date-placeholder"}`}>
          {value ? formatHumanDate(value) : "Select date"}
        </span>
      </button>
      {value && !disabled ? (
        <button
          type="button"
          className="bh-date-clear"
          aria-label="Clear date"
          title="Clear"
          onClick={(e) => {
            e.stopPropagation();
            onChange("");
          }}
        >
          ×
        </button>
      ) : null}
      {open &&
        createPortal(
          <div
            className="bh-date-pop"
            role="dialog"
            aria-label="Pick a date"
            ref={popRef}
          >
            <div className="bh-date-head">
              <button
                type="button"
                className="bh-date-nav"
                data-nav="prev"
                aria-label="Previous month"
                onClick={(e) => { e.stopPropagation(); navMonth(-1); }}
              >
                ‹
              </button>
              <span className="bh-date-title">{MONTHS[view.m]} {view.y}</span>
              <button
                type="button"
                className="bh-date-nav"
                data-nav="next"
                aria-label="Next month"
                onClick={(e) => { e.stopPropagation(); navMonth(1); }}
              >
                ›
              </button>
            </div>
            <div className="bh-date-grid">
              {DOW.map((d, i) => (
                <span key={i} className="bh-date-dow">{d}</span>
              ))}
              {cells.map((c) => (
                <button
                  type="button"
                  key={c.iso}
                  className={c.cls}
                  data-iso={c.iso}
                  onClick={(e) => { e.stopPropagation(); commit(c.iso); }}
                >
                  {c.dayNum}
                </button>
              ))}
            </div>
            <div className="bh-date-foot">
              <button
                type="button"
                className="bh-date-today"
                onClick={(e) => { e.stopPropagation(); commit(isoDate(new Date())); }}
              >
                Today
              </button>
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
