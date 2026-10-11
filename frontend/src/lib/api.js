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

// A request that hangs (a stalled proxy, a dropped connection) fails instead of spinning
// forever; uploads get longer.
const REQUEST_TIMEOUT_MS = 30_000;
const UPLOAD_TIMEOUT_MS = 5 * 60_000;
export const OFFLINE_MESSAGE = "Can't reach the server. Check your connection and try again.";
export const TIMEOUT_MESSAGE = "The server took too long to respond. Please try again.";

/** fetch with a timeout, turning network failures into readable ApiErrors (status 0). */
async function send(path, init, timeoutMs) {
  const timeout = typeof AbortSignal.timeout === "function" ? AbortSignal.timeout(timeoutMs) : undefined;
  const signal = init.signal && timeout && AbortSignal.any ? AbortSignal.any([init.signal, timeout]) : (init.signal || timeout);
  try {
    return await fetch(`${API}${path}`, { ...init, signal });
  } catch (err) {
    if (err?.name === "TimeoutError") throw new ApiError(TIMEOUT_MESSAGE, 0);
    if (err?.name === "AbortError" && init.signal?.aborted) throw err; // the caller cancelled
    if (err instanceof TypeError || err?.name === "AbortError") throw new ApiError(OFFLINE_MESSAGE, 0);
    throw err;
  }
}

function timeoutFor(opts) {
  return opts.timeoutMs ?? (opts.body instanceof FormData ? UPLOAD_TIMEOUT_MS : REQUEST_TIMEOUT_MS);
}

export async function api(path, opts = {}) {
  const { timeoutMs: _timeout, ...rest } = opts;
  const res = await send(path, requestInit(rest), timeoutFor(opts));

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
  const { timeoutMs: _timeout, ...rest } = opts;
  // Exports can take a while to build; allow as long as an upload.
  const res = await send(path, requestInit(rest), opts.timeoutMs ?? UPLOAD_TIMEOUT_MS);
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
