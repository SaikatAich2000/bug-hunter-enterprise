/** Imperative toasts via the shell's #toast div — callable anywhere without prop-drilling. */
import { ApiError } from "./api";

let toastTimer;

export function toast(msg, type = "") {
  const el = document.getElementById("toast");
  if (!el) return;
  // The same message again while it is showing just stays up longer (no flicker storm).
  const className = `toast ${type}`;
  if (!(el.textContent === msg && el.className === className && !el.hidden)) {
    el.textContent = msg;
    el.className = className;
    el.hidden = false;
  }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
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
