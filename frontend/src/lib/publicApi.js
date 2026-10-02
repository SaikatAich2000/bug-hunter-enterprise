// Fetch helpers for the signed-out pages (login, sign-up, invitation, deletion): unlike api(),
// a 401 here is an answer to show, not a reason to redirect.

/** The readable text of a FastAPI error body (string or Pydantic error list). */
export function errorMessage(data, fallback) {
  const detail = data && typeof data === "object" ? data.detail : null;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : ""))
      .filter(Boolean);
    if (parts.length) return parts.join(", ");
  }
  return fallback;
}

/** Send JSON and resolve `{ ok, status, data }`; network failures reject. */
export async function sendJson(path, { method = "POST", json } = {}) {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "include",
    headers: json === undefined ? {} : { "Content-Type": "application/json" },
    body: json === undefined ? undefined : JSON.stringify(json),
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* empty or non-JSON body */
  }
  return { ok: res.ok, status: res.status, data };
}
