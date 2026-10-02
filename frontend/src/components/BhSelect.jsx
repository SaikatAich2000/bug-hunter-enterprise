/** Custom single-select; popover portaled to <body> so ancestor transforms can't break fixed positioning. */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// placeholder labels look like "— … —"
const PLACEHOLDER_RE = /^—.*—$/;

export default function BhSelect({ value, onChange, options, disabled, id, name, ariaLabel }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef(null);
  const btnRef = useRef(null);
  const popRef = useRef(null);

  // fixed-coord placement; flips above when clipping below
  const placePop = useCallback(() => {
    const btn = btnRef.current;
    const pop = popRef.current;
    if (!btn || !pop) return;
    const r = btn.getBoundingClientRect();
    const vh = window.innerHeight;
    const vw = window.innerWidth;
    const ph = pop.offsetHeight || 240;
    const pw = Math.max(pop.offsetWidth || 200, r.width);
    pop.style.minWidth = `${r.width}px`;
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

  // close on outside click
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
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  // capture-phase Escape so the surrounding modal doesn't close mid-form
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setOpen(false);
      btnRef.current?.focus();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open]);

  // unmatched value → selectedIndex -1, label "—"
  const selectedIndex = options.findIndex((o) => o.value === value);
  const selOpt = selectedIndex >= 0 ? options[selectedIndex] : undefined;
  const labelText = selOpt ? selOpt.label : "";
  const isPlaceholder = !value && selOpt !== undefined && PLACEHOLDER_RE.test(selOpt.label || "");

  return (
    <div className="bh-sel-wrap" ref={wrapRef}>
      {/* native select stays for form submission + keyboard a11y */}
      <select
        className="bh-sel-native"
        id={id}
        name={name}
        value={value}
        disabled={disabled}
        aria-label={ariaLabel}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value} title={o.title}>
            {o.label}
          </option>
        ))}
      </select>
      <button
        type="button"
        className={`bh-sel-btn${disabled ? " is-disabled" : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        ref={btnRef}
        onClick={() => {
          if (disabled) return;
          setOpen((o) => !o);
        }}
      >
        <span className={`bh-sel-label${isPlaceholder ? " bh-sel-placeholder" : ""}`}>
          {labelText || "—"}
        </span>
        {!disabled && <span className="bh-sel-caret" aria-hidden="true">▾</span>}
      </button>
      {/* portal to <body>; popRef still points at the real node */}
      {open &&
        createPortal(
          <div className="bh-sel-pop" role="listbox" ref={popRef}>
            {options.map((o, i) => {
              const isSel = i === selectedIndex;
              return (
                <button
                  type="button"
                  key={o.value}
                  className={`bh-sel-row${isSel ? " is-selected" : ""}`}
                  role="option"
                  aria-selected={isSel}
                  data-bh-sel-i={i}
                  title={o.title}
                  onClick={() => {
                    onChange(o.value);
                    setOpen(false);
                  }}
                >
                  {o.label}
                </button>
              );
            })}
          </div>,
          document.body,
        )}
    </div>
  );
}
