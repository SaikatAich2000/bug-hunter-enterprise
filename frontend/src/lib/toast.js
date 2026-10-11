/** Imperative toasts via the shell's #toast div — callable anywhere without prop-drilling. */
import { ApiError } from "./api";
import { MOTION_MS, prefersReducedMotion } from "./motion";

let toastTimer;
let leaveTimer;

export function toast(msg, type = "") {
  const el = document.getElementById("toast");
  if (!el) return;
  clearTimeout(leaveTimer);
  // The same message again while it is showing just stays up longer (no flicker storm).
  const className = `toast ${type}`;
  if (!(el.textContent === msg && el.className === className && !el.hidden)) {
    el.textContent = msg;
    el.className = className;
    el.hidden = false;
  } else {
    el.classList.remove("leaving");
  }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    if (prefersReducedMotion()) {
      el.hidden = true;
      return;
    }
    el.classList.add("leaving");
    leaveTimer = setTimeout(() => {
      el.hidden = true;
      el.classList.remove("leaving");
    }, MOTION_MS);
  }, 3500);
}

/** Error toast; quiet for the silent 401-redirect ApiError. */
export function toastError(err) {
  if (err instanceof ApiError && err.silent) return;
  let msg = "Something went wrong";
  if (err instanceof Error) msg = err.message;
  else if (typeof err === "string") msg = err;
  toast(msg, "error");
}
