/**
 * Full-screen overlay for video attachments using the custom player.
 * Portaled to <body> to escape any parent modal's stacking/transform context.
 */
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import VideoPlayer from "./VideoPlayer";

export default function VideoLightbox({ src, type, label, onClose }) {
  const dialogRef = useRef(null);
  const closeBtnRef = useRef(null);
  const stageRef = useRef(null);

  // Move focus into the dialog, restore it on unmount.
  useEffect(() => {
    const prevActive = document.activeElement;
    closeBtnRef.current?.focus();
    return () => {
      if (prevActive && typeof prevActive.focus === "function") prevActive.focus();
    };
  }, []);

  // Esc closes, background scroll locks, Tab/Shift+Tab trap within the dialog.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key === "Tab") {
        const root = dialogRef.current;
        if (!root) return;
        const focusable = Array.from(
          root.querySelectorAll(
            'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
          ),
        ).filter((el) => el.offsetParent !== null || el === document.activeElement);
        if (focusable.length === 0) {
          e.preventDefault();
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const active = document.activeElement;
        if (e.shiftKey) {
          if (active === first || !root.contains(active)) {
            e.preventDefault();
            last.focus();
          }
        } else if (active === last || !root.contains(active)) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey, true);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  // Backdrop click closes, clicks inside the stage card don't. Attached
  // imperatively so the overlay itself carries no JSX click handler.
  // The listener is added during the same task that opened the lightbox, so
  // the opening click (e.g. the "View" button) is still bubbling towards
  // <document> when it registers — without the guard below it would close
  // the lightbox immediately, making it impossible to open via a click.
  const ignoreOpeningClick = useRef(true);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      ignoreOpeningClick.current = false;
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);
  useEffect(() => {
    const onDocClick = (e) => {
      if (ignoreOpeningClick.current) return;
      const t = e.target instanceof Node ? e.target : null;
      if (t && stageRef.current?.contains(t)) return;
      onClose();
    };
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [onClose]);

  return createPortal(
    
    <div
      className="video-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label={label ? `Video: ${label}` : "Video"}
      ref={dialogRef}
    >
      <button
        type="button"
        className="video-lightbox-close"
        aria-label="Close video"
        ref={closeBtnRef}
        onClick={onClose}
      >
        ✕
      </button>
      {label ? <div className="video-lightbox-title">{label}</div> : null}

      <div className="video-lightbox-stage" ref={stageRef}>
        <VideoPlayer src={src} type={type} label={label} autoPlay />
      </div>
    </div>,
    document.body,
  );
}
