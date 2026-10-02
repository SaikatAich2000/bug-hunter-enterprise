/** Counter-based global loader; overlay stays up until all callers finish. */
let pending = 0;

function apply(message) {
  const el = document.getElementById("globalLoader");
  const txt = document.getElementById("globalLoaderText");
  if (!el) return;
  if (message && txt) txt.textContent = message;
  const on = pending > 0;
  el.hidden = !on;
  document.body.classList.toggle("is-loading", on);
}

export function showLoader(message = "Working…") {
  pending += 1;
  apply(message);
}

export function hideLoader() {
  pending = Math.max(0, pending - 1);
  apply();
}

export async function withLoader(
  thunk,
  message = "Working…",
) {
  showLoader(message);
  try {
    return await thunk();
  } finally {
    hideLoader();
  }
}
