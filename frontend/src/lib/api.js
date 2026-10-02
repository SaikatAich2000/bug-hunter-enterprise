/** Typed fetch wrapper: cookies, deduped 401 redirect, FastAPI {detail} unwrapping. */

export const API = "/api";

export class ApiError extends Error {
  status;
  silent;

  constructor(message, status, silent = false) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.silent = silent;
  }
}

let sessionRedirectInFlight = false;

function bounceToLogin() {
  if (sessionRedirectInFlight) return;
  sessionRedirectInFlight = true;
  const next = encodeURIComponent(
    location.pathname + location.search + location.hash,
  );
  location.replace(`/login.html?next=${next}`);
}

/** Readable message from a FastAPI error body. */
function detailToMessage(body, fallback) {
  if (!body || typeof body !== "object") return fallback;
  const detail = body.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // Pydantic validation error list
    const msgs = detail
      .map((d) =>
        d && typeof d === "object" && "msg" in d
          ? String(d.msg)
          : "",
      )
      .filter(Boolean);
    if (msgs.length) return msgs.join("; ");
  }
  return fallback;
}

export function requestInit(opts = {}) {
  const { json, headers, body, ...rest } = opts;
  const hasJson = json !== undefined;
  const requestBody = hasJson ? JSON.stringify(json) : body;
  return {
    credentials: "include",
    ...rest,
    headers: {
      ...(hasJson ? { "Content-Type": "application/json" } : {}),
      ...headers,
    },
    body: requestBody,
  };
}

export async function api(path, opts = {}) {
  const init = requestInit(opts);

  const res = await fetch(`${API}${path}`, init);

  if (res.status === 401) {
    bounceToLogin();
    // navigating away; silent suppresses caller toasts
    throw new ApiError("Session expired", 401, true);
  }

  if (res.status === 204) return null;

  const text = await res.text();
  let parsed = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = text;
  }

  if (!res.ok) {
    throw new ApiError(
      detailToMessage(parsed, `Request failed (${res.status})`),
      res.status,
    );
  }
  return parsed;
}

/** Fetch a binary endpoint; returns blob + filename. */
export async function apiBlob(path, opts = {}) {
  const res = await fetch(`${API}${path}`, requestInit(opts));
  if (res.status === 401) {
    bounceToLogin();
    throw new ApiError("Session expired", 401, true);
  }
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      msg = detailToMessage(await res.json(), msg);
    } catch {
      /* binary body; keep fallback message */
    }
    throw new ApiError(msg, res.status);
  }
  const cd = res.headers.get("Content-Disposition") || "";
  // RFC 5987 filename* wins; plain regex excludes '*' so it can't half-match that form
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(cd);
  const plain = /filename="?([^";*][^";]*)"?/i.exec(cd);
  let raw = "";
  if (star) {
    try { raw = decodeURIComponent(star[1].trim()); } catch { raw = star[1].trim(); }
  } else if (plain) {
    raw = plain[1].trim();
  }
  // strip path separators from server-supplied names
  const filename = raw ? (raw.replace(/[\\/]/g, "_").trim() || null) : null;
  return { blob: await res.blob(), filename };
}
