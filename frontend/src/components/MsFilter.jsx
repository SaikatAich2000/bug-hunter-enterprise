/**
 * Filter-bar multi-select dropdown; class names must match styles.css.
 * Only one panel open at a time via a module-level registry.
 */
import { memo, useCallback, useEffect, useMemo, useState } from "react";

// Each instance registers its close callback; opening a panel closes the rest.
const closers = new Set();
function closeOthers(except) {
  closers.forEach((close) => {
    if (close !== except) close();
  });
}

/** Button label: "All {label}", a single name, or "{noun} (n)". */
function msButtonLabel(label, noun, options, selected) {
  if (selected.length === 0) return `All ${label}`;
  if (selected.length === 1) {
    const only = selected[0];
    const match = options.find(([v]) => v === only);
    return match ? match[1] : only;
  }
  return `${noun} (${selected.length})`;
}

function MsFilter({ filterKey, label, noun, options, selected, onToggle }) {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);

  useEffect(() => {
    closers.add(close);
    return () => {
      closers.delete(close);
    };
  }, [close]);

  // Click-outside to close; row/toggle clicks stopPropagation so no containment check needed.
  useEffect(() => {
    if (!open) return;
    const onDocClick = () => setOpen(false);
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  // Capture-phase Escape to pre-empt any surrounding modal's handler.
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setOpen(false);
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open]);

  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const panelId = `ms-panel-${filterKey}`;

  return (
    <div className="ms-wrap" data-filter={filterKey}>
      <button
        type="button"
        className={`ms-btn${selected.length > 0 ? " active" : ""}`}
        data-ms-toggle=""
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={(e) => {
          e.stopPropagation();
          closeOthers(close);
          setOpen((o) => !o);
        }}
      >
        <span className="ms-btn-label">{msButtonLabel(label, noun, options, selected)}</span>
        <span className="ms-caret">▾</span>
      </button>
      <div className="ms-panel" id={panelId} hidden={!open} role="menu">
        {options.length === 0 ? (
          <div className="ms-empty">No options</div>
        ) : (
          options.map(([v, lbl]) => {
            const isOn = selectedSet.has(v);
            const toggle = (e) => {
              e.stopPropagation();
              onToggle(v);
            };
            return (
              <div
                key={v}
                className={`ms-row${isOn ? " on" : ""}`}
                data-ms-value={v}
                role="menuitemcheckbox"
                aria-checked={isOn}
                tabIndex={0}
                onClick={toggle}
                onKeyDown={(e) => {
                  // Enter/Space mirror a mouse click on the row.
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    toggle(e);
                  }
                }}
              >
                <span className="ms-check">{isOn ? "✓" : ""}</span>
                <span className="ms-text">{lbl}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// Memo: FilterBar passes stable handlers/arrays, so parent re-renders don't cascade here.
export default memo(MsFilter);
