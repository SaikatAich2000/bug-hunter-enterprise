/** "⋯" menu button with a popover list of actions (keyboard and screen-reader
 * friendly). Items: [{ label, onSelect, disabled?, danger?, separator? }]. */
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

const stop = (e) => e.stopPropagation();

export default function ActionMenu({ label = "More actions", items, className = "", icon = "⋯" }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState({ top: 0, left: 0 });
  const buttonRef = useRef(null);
  const menuRef = useRef(null);
  const menuId = useId();

  useLayoutEffect(() => {
    if (!open || !buttonRef.current) return;
    const r = buttonRef.current.getBoundingClientRect();
    const width = menuRef.current?.offsetWidth || 200;
    const height = menuRef.current?.offsetHeight || 200;
    const left = Math.max(8, Math.min(r.right - width, window.innerWidth - width - 8));
    const below = r.bottom + 4;
    const top = below + height > window.innerHeight - 8 ? Math.max(8, r.top - height - 4) : below;
    setPos({ top, left });
    menuRef.current?.querySelector("button:not([disabled])")?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => {
      if (menuRef.current?.contains(e.target) || buttonRef.current?.contains(e.target)) return;
      setOpen(false);
    };
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
        buttonRef.current?.focus();
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const buttons = [...(menuRef.current?.querySelectorAll("button:not([disabled])") ?? [])];
        const at = buttons.indexOf(document.activeElement);
        const next = e.key === "ArrowDown" ? (at + 1) % buttons.length : (at - 1 + buttons.length) % buttons.length;
        buttons[next]?.focus();
      }
    };
    const onScroll = () => setOpen(false);
    // Capture phase: a press on another control (another menu's button, a
    // row checkbox) stops propagation, and must still close this menu.
    document.addEventListener("mousedown", onDown, true);
    document.addEventListener("touchstart", onDown, true);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onDown, true);
      document.removeEventListener("touchstart", onDown, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [open]);

  const visible = (items || []).filter(Boolean);
  if (!visible.length) return null;
  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className={`ag-menu-btn ${className}`.trim()}
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
        // Presses and keys belong to the menu, never to the draggable row or
        // card around it.
        onPointerDown={stop}
        onMouseDown={stop}
        onTouchStart={stop}
        onKeyDown={stop}
      >
        {icon}
      </button>
      {open && createPortal(
        <div ref={menuRef} id={menuId} role="menu" className="ag-menu" style={{ top: pos.top, left: pos.left }}
          tabIndex={-1} onKeyDown={stop} onPointerDown={stop} onMouseDown={stop} onTouchStart={stop}>
          {visible.map((item, n) => (item.separator ? (
            <div key={`sep-${n}`} role="separator" className="ag-menu-sep" />
          ) : (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              className={`ag-menu-item${item.danger ? " danger" : ""}`}
              disabled={item.disabled}
              onClick={(e) => {
                e.stopPropagation();
                setOpen(false);
                buttonRef.current?.focus();
                item.onSelect();
              }}
            >
              {item.label}
            </button>
          )))}
        </div>,
        document.body,
      )}
    </>
  );
}
