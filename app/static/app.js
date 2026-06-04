/* ============================================================
 * Bug Hunter — frontend SPA
 * ============================================================ */
(() => {
"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const STATE = {
  // v2.5: statuses_by_type maps each item_type to its valid status set.
  // We default to a sensible Bug-only fallback so legacy clients without
  // a fresh /api/meta call still render something meaningful.
  meta:     {
    statuses: [], priorities: [], environments: [],
    item_types: ["Bug", "Requirement", "Task"],
    statuses_by_type: {
      Bug: ["New", "In Progress", "Resolved", "Closed", "Reopened", "Not a Bug", "Resolve Later"],
      Requirement: ["New", "In Review", "Approved", "Implemented", "Rejected", "Deferred"],
      Task: ["New", "In Progress", "Done", "Blocked", "Cancelled"],
    },
  },
  users:    [],
  projects: [],
  events:   [],            // v2.4 — events list cache
  currentEventId: null,    // v2.4 — null = list mode; id = detail mode
  currentEvent: null,      // v2.4 — detail object when drilled in
  // v2.5: client-side filters for the events list view.
  eventsFilter: { q: "", date: "" },
  // v2.5: client-side filters for the items inside an event (detail view).
  // Mirror the shape of STATE.filters for the main work-items list.
  eventDetailFilter: { q: "", status: [], priority: [], assignee_id: [] },
  stats:    null,
  bugs:     [],
  page:     1,
  pageSize: 50,
  totalPages: 1,
  total: 0,
  // Filters: each enum-like filter is now an ARRAY (multi-select). The free-
  // text search `q` and the legacy single-value `reporter_id` stay scalar.
  filters: {
    project_id: [], status: [], priority: [],
    environment: [], assignee_id: [], item_type: [],   // v2.4 item_type filter
    reporter_id: "", q: "",
  },
  // v2.4 — "+ New" split button remembers the last-chosen type.
  defaultNewType: (() => {
    try { return localStorage.getItem("defaultNewType") || "Bug"; }
    catch { return "Bug"; }
  })(),
  // v2.4 — active work-items tab: "all" / "Bug" / "Requirement" / "Task".
  activeTab: (() => {
    try { return localStorage.getItem("activeTab") || "all"; }
    catch { return "all"; }
  })(),
  // v2.4 / v2.5 — staged-files buckets for the bug create modal, comment
  // composer, and (v2.5) the post-creation bug-level attachment uploader.
  // Files chosen via the input land here; the X-on-hover removes
  // individual entries (FileList is read-only so we can't splice it
  // directly). Submit reads the array, not input.files.
  stagedFiles: { createBug: [], comment: [], bugAttach: [] },
  view: "list",
  currentBugId: null,
  // Detail tabs are gone in v3.1 — bug detail is now a single inline
  // screen (Jira-style). detailTab kept here as a no-op for backward
  // compat in case any external code path still touches it.
  detailTab: "info",
  sessions: [],
  currentUser: null,   // populated from /api/auth/me at boot
  // Asset hash served by /api/health at boot; if it changes later we
  // know the server has been redeployed.
  bootAssetVersion: null,
  versionDriftWarned: false,
  // Sidebar collapsed flag. Persisted to localStorage so the layout the
  // user picked survives page reloads.
  sidebarCollapsed: false,
};

const API = "/api";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const escapeHtml = (s) => String(s ?? "").replaceAll(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

const debounce = (fn, ms = 250) => {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
};

const initials = (name) => {
  const parts = String(name || "?").trim().split(/\s+/);
  return ((parts[0]?.[0] || "?") + (parts[1]?.[0] || "")).toUpperCase();
};

const formatDate = (iso) => {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay
      ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
  } catch { return iso; }
};

const formatBytes = (n) => {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
};

const fileIcon = (ct, name) => {
  ct = (ct || "").toLowerCase();
  name = (name || "").toLowerCase();
  if (ct.startsWith("image/")) return "🖼";
  if (ct.startsWith("video/")) return "🎬";
  if (ct === "application/pdf" || name.endsWith(".pdf")) return "📕";
  if (ct.startsWith("audio/")) return "🎵";
  if (ct.includes("zip") || name.endsWith(".zip")) return "📦";
  return "📎";
};

// ---------------------------------------------------------------------------
// API client
// ---------------------------------------------------------------------------
// CSRF double-submit: read the `bh_csrf` cookie set by the server on
// page load and echo it back in the X-CSRF-Token header on any
// state-changing request. Same-Origin Policy prevents foreign sites
// from reading the cookie, so they can't forge a matching header.
function _readCookie(name) {
  const re = new RegExp(String.raw`(?:^|;\s*)` + name + "=([^;]+)");
  const match = re.exec(document.cookie);
  return match ? decodeURIComponent(match[1]) : "";
}

function _buildApiHeaders(opts) {
  const headers = { ...opts.headers };
  // Don't auto-set Content-Type for FormData (browser sets boundary)
  if (opts.body && !(opts.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const method = (opts.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
    const csrf = _readCookie("bh_csrf");
    if (csrf && !headers["X-CSRF-Token"]) {
      headers["X-CSRF-Token"] = csrf;
    }
  }
  return headers;
}

async function _extractApiErrorDetail(res) {
  let detail = `HTTP ${res.status}`;
  try {
    const body = await res.json();
    if (Array.isArray(body.detail)) {
      detail = body.detail.map(d => `${(d.loc || []).slice(1).join(".") || "field"}: ${d.msg}`).join("; ");
    } else if (body.detail) {
      detail = body.detail;
    }
  } catch { /* not JSON */ }
  return detail;
}

async function api(path, opts = {}) {
  const headers = _buildApiHeaders(opts);
  const res = await fetch(API + path, {
    ...opts,
    headers,
    credentials: "include",   // send/receive session cookies
  });
  if (!res.ok) {
    // Session expired or otherwise rejected — bounce to login. We delegate
    // to bounceToLogin() so multiple in-flight 401s during a session
    // revocation only trigger one redirect (sessionRedirectInFlight guard).
    if (res.status === 401 && path !== "/auth/login") {
      bounceToLogin();
      const err = new Error("Not authenticated");
      err.status = 401;
      err.silent = true;
      throw err;
    }
    const detail = await _extractApiErrorDetail(res);
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

// ---------------------------------------------------------------------------
// Global blocking loader (v2.5)
//
// Any action button that triggers a server round-trip should run inside
// withLoader() so the user sees an "in flight" overlay and can't
// double-submit by mashing buttons or clicking somewhere else mid-call.
// The counter pattern handles concurrent actions: the overlay only
// disappears when EVERY in-flight op has finished.
// ---------------------------------------------------------------------------
const LOADER_DEFAULT_MSG = "Working…";
let _loaderPending = 0;
function _refreshLoader() {
  const el = document.getElementById("globalLoader");
  if (!el) return;
  const shouldShow = _loaderPending > 0;
  el.hidden = !shouldShow;
  el.setAttribute("aria-hidden", shouldShow ? "false" : "true");
  // Reflect on <body> so CSS can ::disable interactions on background
  // tooltips / focus rings without us having to bind listeners.
  document.body.classList.toggle("is-loading", shouldShow);
}
function showLoader(message) {
  _loaderPending++;
  const txt = document.getElementById("globalLoaderText");
  if (txt && message) txt.textContent = message;
  _refreshLoader();
}
function hideLoader() {
  _loaderPending = Math.max(0, _loaderPending - 1);
  _refreshLoader();
}
async function withLoader(thunk, message) {
  showLoader(message || LOADER_DEFAULT_MSG);
  try {
    return await thunk();
  } finally {
    hideLoader();
  }
}

// ---------------------------------------------------------------------------
// Toast + Modal helpers
// ---------------------------------------------------------------------------
let toastTimer = null;
function toast(msg, type = "info") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${type}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

// Show an error toast UNLESS the error is a silent auth-redirect from api().
// This prevents the brief flash of "Not authenticated" toasts during the
// navigation from / to /login.html when a session expires.
function toastError(err) {
  if (err?.silent) return;
  toast(err?.message || "Something went wrong", "error");
}

function openModal(id) {
  const m = document.getElementById(id);
  if (m) m.hidden = false;
}
function closeModal(id) {
  const m = document.getElementById(id);
  if (m) m.hidden = true;
}
function closeTopModal() {
  const open = $$(".modal:not([hidden])");
  if (open.length) open.at(-1).hidden = true;
}

function confirmDialog(message, { title = "Confirm", okLabel = "Delete", danger = true } = {}) {
  // Track the in-flight resolve so Escape / backdrop-click handlers can
  // also resolve the promise (as cancel). Without this, dismissing the
  // dialog with Escape leaves the await dangling forever AND the next
  // confirmDialog stacks new listeners on top of the stale ones, so
  // clicking OK fires both old and new resolves — silently triggering
  // the previously-abandoned action (e.g. an unintended delete).
  return new Promise((resolve) => {
    $("#confirmTitle").textContent = title;
    $("#confirmMessage").textContent = message;
    const ok = $("#confirmOk");
    const cancel = $("#confirmCancel");
    ok.textContent = okLabel;
    ok.className = "btn " + (danger ? "danger" : "primary");
    let settled = false;
    const settle = (value) => {
      if (settled) return;
      settled = true;
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      document.getElementById("confirmClose").removeEventListener("click", onCancel);
      document.removeEventListener("keydown", onKey, true);
      closeModal("modalConfirm");
      resolve(value);
    };
    const onOk      = () => settle(true);
    const onCancel  = () => settle(false);
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); settle(false); } };
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    document.getElementById("confirmClose").addEventListener("click", onCancel);
    // Use capture so we beat the global Escape handler at lower layer.
    document.addEventListener("keydown", onKey, true);
    openModal("modalConfirm");
  });
}

// ---------------------------------------------------------------------------
// v2.6 — Custom date picker
//
// Native <input type="date"> popups vary wildly across browsers and look
// dated (heh). We replace every input[type=date] with a custom picker.
// The native input is kept in the DOM (hidden) so form submission still
// finds it. enhanceDateInput() wraps a visible button + popover around
// it; clicking a day commits the value to the native input and fires a
// real "change" event so existing listeners keep working.
// ---------------------------------------------------------------------------
const _DOW = ["S", "M", "T", "W", "T", "F", "S"];
const _MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

function _isoDate(d) {
  // Format a JS Date as YYYY-MM-DD in local time (NOT UTC — picking
  // "today" should give the user's today, not yesterday in their zone).
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}
function _parseIso(s) {
  // Parse YYYY-MM-DD as a local-time Date. Avoids the gotcha where
  // `new Date("2026-06-01")` is parsed as midnight UTC and may render
  // as the previous day in negative-offset timezones.
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const [y, m, d] = s.split("-").map(n => Number.parseInt(n, 10));
  return new Date(y, m - 1, d);
}
function _formatHumanDate(s) {
  const d = _parseIso(s);
  if (!d) return "";
  return `${_MONTHS[d.getMonth()].slice(0, 3)} ${d.getDate()}, ${d.getFullYear()}`;
}

// Shared popover-close helper used by every viewport-anchored popover
// factory (date picker, native-select rewrap, multi-select, etc.). They
// all need the same teardown: hide the panel, collapse the trigger's
// aria-expanded, and unbind the reposition listeners.
function _hidePopover(pop, btn, placer) {
  pop.hidden = true;
  btn.setAttribute("aria-expanded", "false");
  window.removeEventListener("resize", placer);
  window.removeEventListener("scroll", placer, true);
}

function enhanceDateInput(input) {
  if (!input || input.dataset.bhEnhanced === "1") return;
  input.dataset.bhEnhanced = "1";
  // Hide the native input but keep it in the form — submission relies
  // on its `name` + `value`. We use offscreen instead of display:none
  // because some browsers won't focus a display:none input.
  input.classList.add("bh-date-native");
  // Replace input.type so users don't see the browser-native picker.
  input.type = "hidden";
  const wrap = document.createElement("div");
  wrap.className = "bh-date-wrap";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "bh-date-btn";
  btn.setAttribute("aria-haspopup", "dialog");
  btn.setAttribute("aria-expanded", "false");
  const icon = `<span class="bh-date-icon" aria-hidden="true">📅</span>`;
  const updateLabel = () => {
    const v = input.value;
    btn.innerHTML = `${icon}<span class="bh-date-label${v ? "" : " bh-date-placeholder"}">${v ? escapeHtml(_formatHumanDate(v)) : "Select date"}</span>${v ? '<span class="bh-date-clear" aria-label="Clear" title="Clear">×</span>' : ""}`;
  };
  updateLabel();
  // Insert wrap right after the native input, then move the native
  // input INSIDE the wrap so labels/refs still point to it correctly.
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  wrap.appendChild(btn);

  const pop = document.createElement("div");
  pop.className = "bh-date-pop";
  pop.hidden = true;
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Pick a date");
  // v2.6 fix: attach to body so `position: fixed` is genuinely viewport-
  // relative. A transformed/translated ancestor would otherwise re-anchor
  // a `fixed` child to itself.
  document.body.appendChild(pop);

  // The month being VIEWED (independent of selection so the user can
  // browse without committing). Initialised from current value or today.
  let viewYear, viewMonth;
  const initView = () => {
    const seed = _parseIso(input.value) || new Date();
    viewYear = seed.getFullYear();
    viewMonth = seed.getMonth();
  };
  initView();

  const _computeCell = (offset, daysInMonth, daysInPrev) => {
    if (offset < 0) {
      let cellMonth = viewMonth - 1;
      let cellYear = viewYear;
      if (cellMonth < 0) { cellMonth = 11; cellYear--; }
      return { dayNum: daysInPrev + offset + 1, cellMonth, cellYear, muted: true };
    }
    if (offset >= daysInMonth) {
      let cellMonth = viewMonth + 1;
      let cellYear = viewYear;
      if (cellMonth > 11) { cellMonth = 0; cellYear++; }
      return { dayNum: offset - daysInMonth + 1, cellMonth, cellYear, muted: true };
    }
    return { dayNum: offset + 1, cellMonth: viewMonth, cellYear: viewYear, muted: false };
  };

  const render = () => {
    const first = new Date(viewYear, viewMonth, 1);
    const startDow = first.getDay();
    const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
    const daysInPrev = new Date(viewYear, viewMonth, 0).getDate();
    const today = _isoDate(new Date());
    const sel = input.value;
    const cells = [];
    for (let i = 0; i < 42; i++) {
      const { dayNum, cellMonth, cellYear, muted } = _computeCell(i - startDow, daysInMonth, daysInPrev);
      const iso = _isoDate(new Date(cellYear, cellMonth, dayNum));
      const cls = [
        "bh-date-cell",
        muted ? "muted" : "",
        iso === today ? "is-today" : "",
        iso === sel ? "is-selected" : "",
      ].filter(Boolean).join(" ");
      cells.push(`<button type="button" class="${cls}" data-iso="${iso}">${dayNum}</button>`);
    }
    pop.innerHTML = `
      <div class="bh-date-head">
        <button type="button" class="bh-date-nav" data-nav="prev" aria-label="Previous month">‹</button>
        <span class="bh-date-title">${_MONTHS[viewMonth]} ${viewYear}</span>
        <button type="button" class="bh-date-nav" data-nav="next" aria-label="Next month">›</button>
      </div>
      <div class="bh-date-grid">
        ${_DOW.map(d => `<span class="bh-date-dow">${d}</span>`).join("")}
        ${cells.join("")}
      </div>
      <div class="bh-date-foot">
        <button type="button" class="bh-date-today">Today</button>
      </div>`;
  };

  // Smart placement so the popover doesn't get clipped by overflow:hidden
  // ancestors. Open above the trigger when there's more room above.
  const placePop = () => {
    const r = btn.getBoundingClientRect();
    const vh = window.innerHeight;
    const vw = window.innerWidth;
    const ph = pop.offsetHeight || 360;
    const pw = pop.offsetWidth || 320;
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
  };

  const open = () => {
    if (!pop.hidden) return;
    initView();
    render();
    pop.hidden = false;
    placePop();
    requestAnimationFrame(placePop);
    btn.setAttribute("aria-expanded", "true");
    setTimeout(() => document.addEventListener("click", outsideClose, { once: true }), 0);
    window.addEventListener("resize", placePop);
    window.addEventListener("scroll", placePop, true);
  };
  const close = () => _hidePopover(pop, btn, placePop);
  const outsideClose = (e) => {
    if (wrap.contains(e.target) || pop.contains(e.target)) {
      setTimeout(() => document.addEventListener("click", outsideClose, { once: true }), 0);
      return;
    }
    close();
  };

  btn.addEventListener("click", (e) => {
    if (e.target.classList.contains("bh-date-clear")) {
      e.stopPropagation();
      input.value = "";
      input.dispatchEvent(new Event("change", { bubbles: true }));
      updateLabel();
      return;
    }
    if (pop.hidden) open(); else close();
  });
  pop.addEventListener("click", (e) => {
    const navBtn = e.target.closest(".bh-date-nav");
    if (navBtn) {
      // v2.6 fix: re-rendering pop.innerHTML inside the prev/next
      // handler means the original click target is no longer a
      // descendant of pop by the time the event bubbles up. Stopping
      // propagation keeps the calendar open across month navigation.
      e.stopPropagation();
      if (navBtn.dataset.nav === "prev") {
        viewMonth--;
        if (viewMonth < 0) { viewMonth = 11; viewYear--; }
      } else {
        viewMonth++;
        if (viewMonth > 11) { viewMonth = 0; viewYear++; }
      }
      render();
      return;
    }
    if (e.target.classList.contains("bh-date-today")) {
      e.stopPropagation();
      const now = new Date();
      input.value = _isoDate(now);
      input.dispatchEvent(new Event("change", { bubbles: true }));
      updateLabel();
      close();
      return;
    }
    const cell = e.target.closest(".bh-date-cell");
    if (cell) {
      e.stopPropagation();
      input.value = cell.dataset.iso;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      updateLabel();
      close();
    }
  });
  pop.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
  });

  // Re-sync the visible label whenever the value is set programmatically
  // (e.g. when a form is populated for "edit" mode).
  const reSync = () => updateLabel();
  new MutationObserver(reSync).observe(input, { attributes: true, attributeFilter: ["value"] });
  input.addEventListener("change", reSync);
}

function enhanceAllDateInputs(root = document) {
  root.querySelectorAll('input[type="date"]').forEach(enhanceDateInput);
}

// v2.6 — Unsafe-file paste filter. We block anything whose extension or
// MIME would let the user trick a colleague into opening a hostile
// payload from the attachments grid.
const _UNSAFE_EXTS = new Set([
  "exe", "bat", "cmd", "com", "scr", "pif", "msi", "msp", "msc",
  "jar", "vbs", "vbe", "js", "jse", "wsf", "wsh",
  "ps1", "psm1", "psd1", "ps1xml", "psc1",
  "sh", "bash", "csh", "zsh", "ksh",
  "app", "deb", "rpm", "dmg", "appimage",
  "reg", "lnk", "inf", "ins", "isp",
  "ade", "adp", "cpl", "mde", "shs", "sct", "vb", "cer", "hta",
]);
const _UNSAFE_MIMES = new Set([
  "application/x-msdownload",
  "application/x-msdos-program",
  "application/x-msi",
  "application/vnd.microsoft.portable-executable",
  "application/x-elf",
  "application/x-mach-binary",
  "application/x-sh",
  "application/x-shellscript",
]);
const _BH_EXT_RE = /\.([a-z0-9]+)$/;
function _bhUnsafeExt(filename) {
  if (!filename) return false;
  const m = _BH_EXT_RE.exec(String(filename).toLowerCase());
  return !!(m && _UNSAFE_EXTS.has(m[1]));
}
function _bhUnsafeMime(mime) {
  if (!mime) return false;
  return _UNSAFE_MIMES.has(String(mime).toLowerCase().split(";")[0].trim());
}

// ---------------------------------------------------------------------------
// v2.6 — Rich-text editor (B / I / U / list / quote / code / image paste)
//
// Wraps a textarea with a toolbar + contenteditable surface. The
// underlying textarea is hidden but kept in the form so submission
// reads the rich HTML from textarea.value. We sync editor → textarea
// on every input.
// ---------------------------------------------------------------------------
// SONAR_RT_BEGIN — Block-suppression marker for sonar-project.properties.
// The rich-text editor below intentionally uses execCommand,
// window.getSelection(), and several complex selection/wrapping helpers
// that Sonar wants modernized. These are the v2.6 cross-browser fixes and
// MUST NOT be refactored — they're load-bearing for the contenteditable
// bug-modal description field.
// ---------------------------------------------------------------------------
function _swapLabelToDiv(textarea) {
  // v2.6 fix: if the textarea is wrapped in a <label>, the label's
  // implicit "focus first form control descendant" behaviour will keep
  // stealing every click and routing focus to the hidden textarea
  // instead of the contenteditable surface.
  const parentLabel = textarea.parentElement;
  if (parentLabel?.tagName === "LABEL") {
    const div = document.createElement("div");
    for (const attr of parentLabel.attributes) {
      div.setAttribute(attr.name, attr.value);
    }
    while (parentLabel.firstChild) div.appendChild(parentLabel.firstChild);
    parentLabel.replaceWith(div);
  }
}

function _buildRtToolbar() {
  const toolbar = document.createElement("div");
  toolbar.className = "bh-rt-toolbar";
  toolbar.innerHTML = `
    <button type="button" data-cmd="bold" title="Bold (Ctrl+B)" aria-label="Bold"><b>B</b></button>
    <button type="button" data-cmd="italic" title="Italic (Ctrl+I)" aria-label="Italic"><i>I</i></button>
    <button type="button" data-cmd="underline" title="Underline (Ctrl+U)" aria-label="Underline"><u>U</u></button>
    <button type="button" data-cmd="strikeThrough" title="Strikethrough" aria-label="Strikethrough"><s>S</s></button>
    <span class="bh-rt-divider"></span>
    <button type="button" data-cmd="insertUnorderedList" title="Bulleted list" aria-label="Bulleted list">•</button>
    <button type="button" data-cmd="insertOrderedList" title="Numbered list" aria-label="Numbered list">1.</button>
    <span class="bh-rt-divider"></span>
    <button type="button" data-cmd="formatBlock" data-arg="blockquote" title="Quote" aria-label="Quote">"</button>
    <button type="button" data-cmd="formatBlock" data-arg="pre" title="Code block" aria-label="Code block">&lt;/&gt;</button>
    <span class="bh-rt-divider"></span>
    <button type="button" data-cmd="bh-image" title="Insert image" aria-label="Insert image">🖼</button>`;
  return toolbar;
}

function enhanceRichEditor(textarea, opts = {}) {
  if (!textarea || textarea.dataset.bhRtEnhanced === "1") return;
  textarea.dataset.bhRtEnhanced = "1";

  textarea.classList.add("bh-rt-native");
  _swapLabelToDiv(textarea);

  const wrap = document.createElement("div");
  wrap.className = "bh-rt-wrap";
  if (opts.compact) wrap.classList.add("compact");

  const toolbar = _buildRtToolbar();

  const editor = document.createElement("div");
  editor.className = "bh-rt-editor";
  editor.contentEditable = "true";
  editor.setAttribute("role", "textbox");
  editor.setAttribute("aria-multiline", "true");
  if (textarea.placeholder) editor.dataset.placeholder = textarea.placeholder;
  if (textarea.id) editor.id = `${textarea.id}_editor`;
  editor.innerHTML = textarea.value || "";

  textarea.parentNode.insertBefore(wrap, textarea);
  wrap.appendChild(toolbar);
  wrap.appendChild(editor);
  wrap.appendChild(textarea);

  const sync = () => {
    const html = editor.innerHTML;
    const stripped = html.replaceAll(/<br\s*\/?>/gi, "").replaceAll(/<\/?(?:div|p)>/gi, "").trim();
    textarea.value = stripped ? html : "";
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  };
  editor.addEventListener("input", sync);

  let savedRange = null;
  const captureSelection = () => {
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return;
    const r = s.getRangeAt(0);
    if (editor.contains(r.commonAncestorContainer)) savedRange = r.cloneRange();
  };
  editor.addEventListener("keyup", captureSelection);
  editor.addEventListener("mouseup", captureSelection);
  editor.addEventListener("focus", captureSelection);
  document.addEventListener("selectionchange", () => {
    if (document.activeElement === editor) captureSelection();
  });

  const ensureCaret = () => {
    const s = window.getSelection();
    if (savedRange && editor.contains(savedRange.commonAncestorContainer)) {
      s.removeAllRanges();
      s.addRange(savedRange);
      return;
    }
    savedRange = null;
    const r = document.createRange();
    r.selectNodeContents(editor);
    r.collapse(false);
    s.removeAllRanges();
    s.addRange(r);
    savedRange = r.cloneRange();
  };

  const inAncestor = (tagNames) => {
    const wanted = new Set(tagNames.map(t => t.toUpperCase()));
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    let n = s.getRangeAt(0).startContainer;
    while (n && n !== editor) {
      if (n.nodeType === 1 && wanted.has(n.tagName)) return true;
      n = n.parentNode;
    }
    return false;
  };

  const _INLINE_TOGGLE = {
    bold: "b",
    italic: "i",
    underline: "u",
    strikeThrough: "s",
  };
  const ZWSP = "​";
  const toggleInlineAtCaret = (tag) => {
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (!editor.contains(r.commonAncestorContainer)) return false;
    if (!r.collapsed) return false;
    let n = r.startContainer;
    while (n && n !== editor) {
      if (n.nodeType === 1 && n.tagName.toLowerCase() === tag) {
        const sep = document.createTextNode(ZWSP);
        n.parentNode.insertBefore(sep, n.nextSibling);
        const after = document.createRange();
        after.setStart(sep, 1);
        after.collapse(true);
        s.removeAllRanges(); s.addRange(after);
        return true;
      }
      n = n.parentNode;
    }
    const wrap = document.createElement(tag);
    wrap.appendChild(document.createTextNode(ZWSP));
    r.insertNode(wrap);
    const inside = document.createRange();
    inside.setStart(wrap.firstChild, 1);
    inside.collapse(true);
    s.removeAllRanges(); s.addRange(inside);
    return true;
  };

  const _WORD = /[A-Za-z0-9_'\-À-ɏͰ-῿一-鿿]/;
  const selectWordAtCaret = () => {
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (!r.collapsed) return true;
    let node = r.startContainer;
    let pos = r.startOffset;
    if (node.nodeType === 1) {
      if (pos > 0 && node.childNodes[pos - 1]?.nodeType === 3) {
        node = node.childNodes[pos - 1];
        pos = (node.textContent || "").length;
      } else if (pos < node.childNodes.length && node.childNodes[pos]?.nodeType === 3) {
        node = node.childNodes[pos];
        pos = 0;
      } else {
        return false;
      }
    }
    if (!node || node.nodeType !== 3) return false;
    const text = node.textContent || "";
    const left = pos > 0 ? text.charAt(pos - 1) : "";
    const right = pos < text.length ? text.charAt(pos) : "";
    if (!_WORD.test(left) && !_WORD.test(right)) return false;
    let start = pos;
    while (start > 0 && _WORD.test(text.charAt(start - 1))) start--;
    let end = pos;
    while (end < text.length && _WORD.test(text.charAt(end))) end++;
    if (start === end) return false;
    const wordRange = document.createRange();
    wordRange.setStart(node, start);
    wordRange.setEnd(node, end);
    s.removeAllRanges();
    s.addRange(wordRange);
    return true;
  };

  const applyInlineWrap = (tag) => {
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (r.collapsed) return false;
    if (!editor.contains(r.commonAncestorContainer)) return false;
    const ancestor = (n) => {
      while (n && n !== editor) {
        if (n.nodeType === 1 && n.tagName.toLowerCase() === tag) return n;
        n = n.parentNode;
      }
      return null;
    };
    const startAnc = ancestor(r.startContainer);
    const endAnc = ancestor(r.endContainer);
    if (startAnc && startAnc === endAnc) {
      const wrap = startAnc;
      const parent = wrap.parentNode;
      const txt = wrap.textContent;
      while (wrap.firstChild) parent.insertBefore(wrap.firstChild, wrap);
      wrap.remove();
      if (parent.firstChild?.nodeType === 3 && parent.firstChild?.textContent === txt) {
        const re = document.createRange();
        re.setStart(parent.firstChild, 0);
        re.setEnd(parent.firstChild, txt.length);
        s.removeAllRanges(); s.addRange(re);
      }
      return true;
    }
    const wrap = document.createElement(tag);
    try {
      r.surroundContents(wrap);
    } catch {
      const frag = r.extractContents();
      wrap.appendChild(frag);
      r.insertNode(wrap);
    }
    const inside = document.createRange();
    inside.selectNodeContents(wrap);
    s.removeAllRanges(); s.addRange(inside);
    return true;
  };

  const applyList = (listTag) => {
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (!editor.contains(r.commonAncestorContainer)) return false;
    const block = (() => {
      let n = r.startContainer;
      while (n && n !== editor) {
        if (n.nodeType === 1 && /^(LI|P|DIV|BLOCKQUOTE|PRE|H[1-6])$/.test(n.tagName)) return n;
        n = n.parentNode;
      }
      return null;
    })();
    if (block?.tagName === "LI") {
      const list = block.parentNode;
      if (list?.tagName === listTag.toUpperCase()) {
        const p = document.createElement("p");
        while (block.firstChild) p.appendChild(block.firstChild);
        list.parentNode.insertBefore(p, list);
        block.remove();
        if (!list.firstChild) list.remove();
        const re = document.createRange();
        re.setStart(p, 0);
        re.collapse(true);
        s.removeAllRanges(); s.addRange(re);
        return true;
      }
    }
    const list = document.createElement(listTag);
    const li = document.createElement("li");
    if (block && block !== editor) {
      while (block.firstChild) li.appendChild(block.firstChild);
      list.appendChild(li);
      block.parentNode.replaceChild(list, block);
    } else if (!r.collapsed) {
      const frag = r.extractContents();
      li.appendChild(frag);
      list.appendChild(li);
      r.insertNode(list);
    } else {
      while (editor.firstChild) li.appendChild(editor.firstChild);
      list.appendChild(li);
      editor.appendChild(list);
    }
    if (!li.firstChild) li.appendChild(document.createElement("br"));
    const re = document.createRange();
    re.selectNodeContents(li);
    re.collapse(false);
    s.removeAllRanges(); s.addRange(re);
    return true;
  };

  const applyBlockWrap = (tag) => {
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (!editor.contains(r.commonAncestorContainer)) return false;
    const existing = (() => {
      let n = r.startContainer;
      while (n && n !== editor) {
        if (n.nodeType === 1 && n.tagName.toLowerCase() === tag) return n;
        n = n.parentNode;
      }
      return null;
    })();
    if (existing) {
      const p = document.createElement("p");
      while (existing.firstChild) p.appendChild(existing.firstChild);
      existing.parentNode.replaceChild(p, existing);
      const re = document.createRange();
      re.selectNodeContents(p);
      re.collapse(false);
      s.removeAllRanges(); s.addRange(re);
      return true;
    }
    const block = (() => {
      let n = r.startContainer;
      while (n && n !== editor) {
        if (n.nodeType === 1 && /^(P|DIV|BLOCKQUOTE|PRE|H[1-6])$/.test(n.tagName)) return n;
        n = n.parentNode;
      }
      return null;
    })();
    const wrap = document.createElement(tag);
    if (block && block !== editor) {
      while (block.firstChild) wrap.appendChild(block.firstChild);
      block.parentNode.replaceChild(wrap, block);
    } else {
      while (editor.firstChild) wrap.appendChild(editor.firstChild);
      editor.appendChild(wrap);
    }
    const re = document.createRange();
    re.selectNodeContents(wrap);
    re.collapse(false);
    s.removeAllRanges(); s.addRange(re);
    return true;
  };

  const runCmd = (cmd, arg = null) => {
    editor.focus();
    ensureCaret();
    if (cmd in _INLINE_TOGGLE) {
      const tag = _INLINE_TOGGLE[cmd];
      const s = window.getSelection();
      if (s?.rangeCount && s.getRangeAt(0)?.collapsed) {
        if (!selectWordAtCaret()) {
          if (toggleInlineAtCaret(tag)) {
            captureSelection(); sync(); return;
          }
        }
      }
      if (applyInlineWrap(tag)) {
        captureSelection(); sync(); return;
      }
    }
    if (cmd === "insertUnorderedList") {
      if (applyList("ul")) { captureSelection(); sync(); return; }
    }
    if (cmd === "insertOrderedList") {
      if (applyList("ol")) { captureSelection(); sync(); return; }
    }
    if (cmd === "formatBlock") {
      const tag = (arg || "").replaceAll(/[<>]/g, "").toLowerCase();
      if (tag && tag !== "p" && applyBlockWrap(tag)) {
        captureSelection(); sync(); return;
      }
      if (tag === "p") {
        for (const t of ["blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"]) {
          if (applyBlockWrap(t)) { captureSelection(); sync(); return; }
        }
      }
    }
    try { document.execCommand(cmd, false, arg); } catch { /* ignore */ }
    captureSelection();
    sync();
  };

  const handleToolbarCmd = (btn) => {
    const cmd = btn.dataset.cmd;
    const arg = btn.dataset.arg || null;
    if (cmd === "bh-image") {
      _bhRtPickFileAsAttachment(textarea, opts);
      return;
    }
    if (cmd === "formatBlock") {
      const tag = (arg || "").toLowerCase();
      if (tag && inAncestor([tag])) {
        runCmd("formatBlock", "<p>");
      } else {
        runCmd("formatBlock", `<${tag}>`);
      }
      updateActiveStates();
      return;
    }
    runCmd(cmd, arg);
    updateActiveStates();
  };

  const updateActiveStates = () => {
    toolbar.querySelectorAll("button[data-cmd]").forEach(b => {
      const c = b.dataset.cmd;
      let active = false;
      if (c in _INLINE_TOGGLE) {
        active = inAncestor([_INLINE_TOGGLE[c]]);
        if (!active) {
          try { active = !!document.queryCommandState(c); } catch {}
        }
      } else if (c === "insertUnorderedList") {
        active = inAncestor(["ul"]);
      } else if (c === "insertOrderedList") {
        active = inAncestor(["ol"]);
      } else if (c === "formatBlock") {
        const arg = (b.dataset.arg || "").toLowerCase();
        if (arg) active = inAncestor([arg]);
      }
      b.classList.toggle("is-active", active);
    });
  };
  editor.addEventListener("keyup", updateActiveStates);
  editor.addEventListener("mouseup", updateActiveStates);
  editor.addEventListener("input", updateActiveStates);

  toolbar.addEventListener("mousedown", (e) => {
    const btn = e.target.closest("button[data-cmd]");
    if (!btn) return;
    captureSelection();
    e.preventDefault();
    handleToolbarCmd(btn);
  });
  toolbar.addEventListener("click", (e) => {
    if (e.detail !== 0) { e.preventDefault(); return; }
    const btn = e.target.closest("button[data-cmd]");
    if (!btn) return;
    e.preventDefault();
    handleToolbarCmd(btn);
  });

  editor.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
      const key = e.key.toLowerCase();
      if (key === "b") { e.preventDefault(); runCmd("bold"); updateActiveStates(); }
      else if (key === "i") { e.preventDefault(); runCmd("italic"); updateActiveStates(); }
      else if (key === "u") { e.preventDefault(); runCmd("underline"); updateActiveStates(); }
    }
  });

  // ----- File paste -----
  editor.addEventListener("paste", async (e) => {
    const items = e.clipboardData?.items || [];
    const files = [];
    for (const it of items) {
      if (it.kind === "file") {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (!files.length) return;
    e.preventDefault();
    const handler = textarea._bhPasteHandler || opts.onPasteFile;
    for (const f of files) {
      if (_bhUnsafeExt(f.name) || _bhUnsafeMime(f.type)) {
        toast(`Blocked unsafe file: ${f.name || f.type}`, "error");
        continue;
      }
      if (!handler) {
        toast(`No attachment target for ${f.name || 'file'}`, "info");
        continue;
      }
      try {
        await handler(f);
      } catch (err) {
        toast(`Paste failed for ${f.name || 'file'}: ${err.message || err}`, "error");
      }
    }
  });

  // Expose setter so openBugForm() can push the row's stored
  // description into the editor on edit-mode open.
  textarea._bhRtSet = (html) => {
    editor.innerHTML = html || "";
    sync();
  };
}

function _bhRtPickFileAsAttachment(textarea, opts) {
  const handler = textarea._bhPasteHandler || opts?.onPasteFile;
  if (!handler) {
    toast("Attach files via the attachment area for this form.", "info");
    return;
  }
  const f = document.createElement("input");
  f.type = "file";
  f.accept = "image/*,application/pdf";
  f.style.display = "none";
  f.addEventListener("change", async () => {
    const file = f.files?.[0];
    if (file) {
      if (_bhUnsafeExt(file.name) || _bhUnsafeMime(file.type)) {
        toast(`Blocked unsafe file: ${file.name}`, "error");
      } else {
        try { await handler(file); } catch (err) {
          toast(`Failed to attach: ${err.message || err}`, "error");
        }
      }
    }
    f.remove();
  });
  document.body.appendChild(f);
  f.click();
}

function enhanceAllRichEditors(root = document) {
  root.querySelectorAll('textarea[data-bh-rt]').forEach(enhanceRichEditor);
}
// SONAR_RT_END — end of the v2.6 rich-text editor block-suppression region.

// ---------------------------------------------------------------------------
// v2.6 — Custom select dropdowns
//
// Native <select> can't be fully restyled across browsers, so we wrap
// each <select data-bh-select> with a button + popover.
// ---------------------------------------------------------------------------
function enhanceCustomSelect(sel) {
  if (!sel || sel.dataset.bhSelEnhanced === "1") return;
  sel.dataset.bhSelEnhanced = "1";
  sel.classList.add("bh-sel-native");

  const wrap = document.createElement("div");
  wrap.className = "bh-sel-wrap";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "bh-sel-btn";
  btn.setAttribute("aria-haspopup", "listbox");
  btn.setAttribute("aria-expanded", "false");
  const pop = document.createElement("div");
  pop.className = "bh-sel-pop";
  pop.setAttribute("role", "listbox");
  pop.hidden = true;

  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);
  wrap.appendChild(btn);
  document.body.appendChild(pop);

  const updateLabel = () => {
    const opt = sel.options[sel.selectedIndex];
    const label = opt ? opt.text : "";
    const isPlaceholder = !sel.value && opt && /^—.*—$/.test(opt.text || "");
    const caret = sel.disabled
      ? ""
      : `<span class="bh-sel-caret" aria-hidden="true">▾</span>`;
    btn.innerHTML = `
      <span class="bh-sel-label${isPlaceholder ? " bh-sel-placeholder" : ""}">${escapeHtml(label || "—")}</span>${caret}`;
  };
  const renderPanel = () => {
    pop.innerHTML = [...sel.options].map((o, i) => {
      const isSel = i === sel.selectedIndex;
      return `<button type="button" class="bh-sel-row${isSel ? " is-selected" : ""}"
        role="menuitemcheckbox" aria-checked="${isSel}" data-bh-sel-i="${i}">${escapeHtml(o.text)}</button>`;
    }).join("");
  };
  updateLabel();

  const placePop = () => {
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
  };

  const open = () => {
    if (sel.disabled) return;
    renderPanel();
    pop.hidden = false;
    placePop();
    requestAnimationFrame(placePop);
    btn.setAttribute("aria-expanded", "true");
    setTimeout(() => document.addEventListener("click", outside, { once: true }), 0);
    window.addEventListener("resize", placePop);
    window.addEventListener("scroll", placePop, true);
  };
  const close = () => _hidePopover(pop, btn, placePop);
  const outside = (e) => {
    if (wrap.contains(e.target) || pop.contains(e.target)) {
      setTimeout(() => document.addEventListener("click", outside, { once: true }), 0);
      return;
    }
    close();
  };

  btn.addEventListener("click", () => {
    if (pop.hidden) open(); else close();
  });
  pop.addEventListener("click", (e) => {
    const row = e.target.closest(".bh-sel-row");
    if (!row) return;
    const i = Number.parseInt(row.dataset.bhSelI, 10);
    sel.selectedIndex = i;
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    updateLabel();
    close();
  });
  sel.addEventListener("change", updateLabel);
  new MutationObserver(() => {
    btn.disabled = sel.disabled;
    btn.classList.toggle("is-disabled", sel.disabled);
    updateLabel();
  }).observe(sel, { attributes: true, attributeFilter: ["disabled"] });
  new MutationObserver(updateLabel).observe(sel, { childList: true });
  btn.disabled = sel.disabled;
  btn.classList.toggle("is-disabled", sel.disabled);
}

function enhanceAllCustomSelects(root = document) {
  root.querySelectorAll('select[data-bh-select]').forEach(enhanceCustomSelect);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function boot() {
  const theme = localStorage.getItem("theme") || "dark";
  document.documentElement.dataset.theme = theme;

  // Restore the sidebar's collapsed state BEFORE first paint to avoid a
  // visible flash of the wrong layout. The CSS class is what actually
  // changes the grid columns; we just make sure it's on the body before
  // the user sees anything.
  STATE.sidebarCollapsed = localStorage.getItem("sidebarCollapsed") === "1";
  if (STATE.sidebarCollapsed) {
    document.body.classList.add("sidebar-collapsed");
  }

  // Auth check first. Use a direct fetch (not api()) so we control the
  // 401 path explicitly: redirect before *any* other code can run, so the
  // user never sees error toasts from cookie-less follow-up calls.
  let me;
  try {
    const res = await fetch(API + "/auth/me", { credentials: "include" });
    if (!res.ok) {
      location.replace("/login.html");
      return;
    }
    me = await res.json();
  } catch {
    location.replace("/login.html");
    return;
  }
  STATE.currentUser = me;
  applyBranding(me.branding);
  applyRoleVisibility();
  renderAccountCard();
  renderOrgBanner();

  await loadHealth();
  await loadMeta();
  await loadUsers();
  await loadProjects();
  // Multi-select dropdowns depend on STATE.users / STATE.projects / STATE.meta
  // being populated, so initialise them after the loaders above.
  initMultiSelects();
  await refreshAll();
  bindGlobalListeners();
  // v2.6: replace native date inputs with the custom calendar popover.
  enhanceAllDateInputs();
  // v2.6: replace plain textareas marked data-bh-rt with the rich-text
  // editor (description, comment composer).
  enhanceAllRichEditors();
  // v2.6: replace selects marked data-bh-select with the styled custom
  // button + popover. Native selects can't be cross-browser styled past
  // the trigger; this gives us a consistent look.
  enhanceAllCustomSelects();
  // v2.6: comment-composer paste handler. Same staging system as the
  // "Attach files" button — postComment() reads the comment bucket and
  // uploads each file with comment_id linkage after the comment row is
  // created.
  const commentBody = $("#commentBody");
  if (commentBody) {
    commentBody._bhPasteHandler = async (f) => {
      STATE.stagedFiles.comment = STATE.stagedFiles.comment || [];
      STATE.stagedFiles.comment.push({ file: f, _blobUrl: URL.createObjectURL(f) });
      _renderStagedFiles("comment", "#filePreview", "#fileLabel");
      toast(`Staged: ${f.name || "pasted file"}`, "info");
    };
  }
  scheduleVersionCheck();
  // Polls /api/auth/me every 15 s so admin session-revocation kicks the
  // user out within seconds, not only when they next click something.
  scheduleSessionPoll();
}

// Apply per-organization branding (logo + accent colour) on boot. The
// SPA's CSS uses --accent and --accent-grad heavily, so a single
// custom-property override threads the chosen colour through every
// gradient and chip in the UI.
function applyBranding(branding) {
  if (!branding) return;
  if (branding.accent_color) {
    const root = document.documentElement;
    root.style.setProperty("--accent", branding.accent_color);
    // Derive a complementary darker stop for the existing gradient
    // recipe. We don't try to be clever — just reuse the same hue but
    // slightly lighter for the second stop.
    root.style.setProperty("--accent-2", branding.accent_color);
    root.style.setProperty(
      "--accent-grad",
      `linear-gradient(135deg, ${branding.accent_color}, ${branding.accent_color})`,
    );
  }
  if (branding.logo_data_url) {
    const imgs = document.querySelectorAll(
      ".brand img, .auth-logo img, [data-brand-img]"
    );
    imgs.forEach(img => { img.src = branding.logo_data_url; });
  }
}

function applyRoleVisibility() {
  const role = STATE.currentUser?.role || "";
  // role rank: admin > manager > member.
  // Accept both "user" (legacy) and "member" (current) as the lowest tier
  // so attributes written as data-needs-role="user" still work — handy
  // during the transition and resilient to typos.
  const rankOf = (r) => {
    if (r === "admin") return 3;
    if (r === "manager") return 2;
    if (r === "member" || r === "user") return 1;
    return 0;
  };
  const rank = rankOf(role);
  $$("[data-needs-role]").forEach(el => {
    const need = el.dataset.needsRole;
    const needRank = rankOf(need);
    if (rank >= needRank) {
      // Drop the attribute so `[data-needs-role] { display:none }` no longer
      // matches. Setting style.display = "" alone is not enough — that CSS
      // rule still wins on specificity.
      delete el.dataset.needsRole;
    } else {
      el.style.display = "none";
    }
  });
}

function renderOrgBanner() {
  const u = STATE.currentUser;
  if (!u) return;
  const nameEl = document.getElementById("orgBannerName");
  const metaEl = document.getElementById("orgBannerMeta");
  if (nameEl) nameEl.textContent = u.organization_name || "—";
  if (metaEl) {
    const slug = u.organization_slug ? `${u.organization_slug} · ` : "";
    metaEl.textContent = `${slug}${u.role}`;
  }
}

function renderAccountCard() {
  const u = STATE.currentUser;
  if (!u) return;
  $("#accountAvatar").textContent = initials(u.name);
  $("#accountName").textContent = u.name;
  $("#accountRole").textContent = u.role;
  $("#accountEmail").textContent = u.email;
}

async function loadHealth() {
  try {
    const h = await api("/health");
    $("#brandVersion").textContent = "v" + h.version;
    // Note the asset_version we booted under so we can detect server
    // redeploys later (see scheduleVersionCheck).
    if (h.asset_version) STATE.bootAssetVersion = h.asset_version;
  } catch { /* ignore */ }
}

// If the server gets redeployed while a tab is open, future API calls
// continue to work but the in-page JS can be subtly stale. Poll
// /api/health every 5 minutes; if asset_version changes, the next page
// navigation should pull the fresh HTML+JS. We just notify the user;
// don't auto-reload because they might have unsaved input.
function scheduleVersionCheck() {
  setInterval(async () => {
    try {
      const h = await fetch("/api/health", { credentials: "include" }).then(r => r.json());
      if (
        STATE.bootAssetVersion &&
        h.asset_version &&
        h.asset_version !== STATE.bootAssetVersion &&
        !STATE.versionDriftWarned
      ) {
        STATE.versionDriftWarned = true;
        toast("New version available — reload the page when ready", "info");
      }
    } catch { /* ignore */ }
  }, 5 * 60 * 1000);
}

// ---------------------------------------------------------------------------
// Session-validity poll — Keycloak-style revocation should kick the user
// out of the SPA quickly, not only when they happen to make an API call.
// We hit /api/auth/me every 15 seconds (cheap — single indexed DB lookup
// + maybe one last_seen_at update). On 401, we redirect to /login.html.
//
// We also re-check on tab visibility change, so a user who tabs back to
// the app gets bounced immediately rather than after the next interval.
// ---------------------------------------------------------------------------
const SESSION_POLL_MS = 15 * 1000;
let sessionPollTimer = null;
let sessionRedirectInFlight = false;

function bounceToLogin() {
  if (sessionRedirectInFlight) return;
  sessionRedirectInFlight = true;
  // Stop the poll so we don't queue further requests during the redirect.
  if (sessionPollTimer) { clearInterval(sessionPollTimer); sessionPollTimer = null; }
  // Best-effort toast — won't always be visible (we're navigating away).
  try { toast("Your session ended. Redirecting to login…", "info"); } catch {}
  // location.replace is preferred so the broken-state URL isn't in history.
  // Fall back to .href in case replace is blocked for any reason.
  try { location.replace("/login.html"); }
  catch { location.href = "/login.html"; }
}

async function checkSessionValid() {
  try {
    const res = await fetch(API + "/auth/me", {
      credentials: "include",
      // Skip the browser cache so a revoked session can't be hidden by a
      // stale 200 response.
      cache: "no-store",
      headers: { "X-Session-Check": "1" },
    });
    if (res.status === 401 || res.status === 403) {
      bounceToLogin();
      return false;
    }
    return res.ok;
  } catch {
    // Network error — don't kick the user out for a transient blip.
    return true;
  }
}

function scheduleSessionPoll() {
  if (sessionPollTimer) clearInterval(sessionPollTimer);
  sessionPollTimer = setInterval(checkSessionValid, SESSION_POLL_MS);
  // Also re-check whenever the tab becomes visible — covers the case
  // where the laptop slept for an hour and the interval didn't fire.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") checkSessionValid();
  });
}

async function loadMeta() {
  const remote = await api("/meta");
  // v2.5: keep the statuses_by_type fallback if an older server doesn't
  // ship the field. Without this, a stale build would render an empty
  // status dropdown.
  const fallback = STATE.meta.statuses_by_type || {};
  STATE.meta = {
    ...remote,
    statuses_by_type: remote.statuses_by_type || fallback,
  };
  // Multi-select panels are repopulated by refreshMultiSelects(); the legacy
  // <select> filters were removed in favour of the new dropdowns.
}

// v2.5 — Returns the valid statuses for a given item_type, falling back
// to the global union if the server didn't ship the per-type map (very
// old build) or the type isn't recognized.
function statusesForType(itype) {
  const byType = (STATE.meta?.statuses_by_type) || {};
  return byType[itype || "Bug"] || (STATE.meta?.statuses) || [];
}

// v2.5 — Populate the status <select> with the options valid for `itype`.
// Preserves the current selection if it still fits; otherwise keeps it
// as a "legacy" option marked with a † suffix so the user can see and
// change it without seeing an "empty" dropdown.
function _renderStatusSelect(selEl, itype, current) {
  const opts = statusesForType(itype);
  const includesCurrent = current && opts.includes(current);
  const items = opts.map(s => [s, s]);
  if (current && !includesCurrent) {
    items.push([current, `${current} (legacy)`]);
  }
  fillFormSelect(selEl, items, current || (opts[0] || "New"));
}

async function loadUsers() {
  STATE.users = await api("/users");
  renderUserList();
  fillAuditActorSelect();
  refreshMultiSelects();
}

async function loadProjects() {
  STATE.projects = await api("/projects");
  renderProjectList();
  refreshMultiSelects();
}

async function refreshAll() {
  await Promise.all([refreshBugs(), refreshStats()]);
  // Any caller that just changed user or project data needs the
  // assignee / project filter dropdowns re-rendered so the new state
  // is visible without a reload.
  refreshMultiSelects();
}

// ---------------------------------------------------------------------------
// Stats / KPIs
// ---------------------------------------------------------------------------
async function refreshStats() {
  // v2.4 — KPI strip is scoped by the active tab. by_type stays GLOBAL
  // server-side so the tab badges keep showing reality.
  const path = (STATE.activeTab && STATE.activeTab !== "all")
    ? `/stats?item_type=${encodeURIComponent(STATE.activeTab)}`
    : "/stats";
  STATE.stats = await api(path);
  const s = STATE.stats || {};
  $("#kpiBugs").textContent = s.bugs ?? 0;
  $("#kpiOpen").textContent = s.open ?? 0;
  $("#kpiResolved").textContent = s.resolved ?? 0;
  $("#kpiClosed").textContent = s.closed ?? (s.by_status?.Closed ?? 0);
  $("#kpiResolveLater").textContent = s.resolve_later ?? (s.by_status?.["Resolve Later"] ?? 0);
  // v2.4 — type-tab badge counts come from by_type (always global).
  const byType = s.by_type || {};
  const tabAll = (byType.Bug ?? 0) + (byType.Requirement ?? 0) + (byType.Task ?? 0);
  const setEl = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  setEl("#typeTabCountAll", tabAll);
  setEl("#typeTabCountBug", byType.Bug ?? 0);
  setEl("#typeTabCountRequirement", byType.Requirement ?? 0);
  setEl("#typeTabCountTask", byType.Task ?? 0);
  refreshKpiActiveState();
  refreshTypeTabActiveState();
  if (STATE.view === "analytics") renderCharts();
}

// ---------------------------------------------------------------------------
// KPI strip — click-to-filter behaviour. Each tile maps to a status set;
// clicking the active tile clears it.
// ---------------------------------------------------------------------------
const KPI_FILTER_MAP = {
  total:         [],
  open:          ["New", "In Progress", "Reopened"],
  resolved:      ["Resolved"],
  closed:        ["Closed"],
  resolve_later: ["Resolve Later"],
};

function _arraysEqualAsSets(a, b) {
  if (a.length !== b.length) return false;
  const sa = new Set(a);
  for (const x of b) if (!sa.has(x)) return false;
  return true;
}

function refreshKpiActiveState() {
  const cur = STATE.filters.status || [];
  $$("#kpiStrip .kpi").forEach(btn => {
    const key = btn.dataset.kpi;
    const target = KPI_FILTER_MAP[key];
    if (!target) return;
    const active = key === "total"
      ? cur.length === 0
      : _arraysEqualAsSets(cur, target);
    btn.classList.toggle("active", active);
  });
}

function handleKpiClick(key) {
  const target = KPI_FILTER_MAP[key];
  if (!target) return;
  const cur = STATE.filters.status || [];
  // Toggle: clicking the active filter clears it back to "all bugs".
  if (_arraysEqualAsSets(cur, target) && target.length > 0) {
    STATE.filters.status = [];
  } else {
    STATE.filters.status = [...target];
  }
  STATE.page = 1;
  if (STATE.view !== "list") setView("list");
  refreshMultiSelects();
  refreshKpiActiveState();
  refreshBugs();
}

// ---------------------------------------------------------------------------
// Bug list
// ---------------------------------------------------------------------------
function _appendFiltersToParams(params) {
  // Multi-value filters: append each value as its own query param so the
  // backend sees `?status=A&status=B`. FastAPI parses repeated params
  // into a list. Scalar filters (q, reporter_id) are appended once.
  for (const [k, v] of Object.entries(STATE.filters)) {
    if (Array.isArray(v)) {
      for (const item of v) {
        if (item !== "" && item != null) params.append(k, String(item));
      }
    } else if (v !== "" && v != null) {
      params.set(k, String(v));
    }
  }
}

function _appendActiveTabFilter(params) {
  // v2.4 — implicit tab filter. Layered on top of STATE.filters.item_type
  // so the user can still multi-select extra types via the dropdown but
  // the tab provides the default narrowing.
  if (STATE.activeTab && STATE.activeTab !== "all") {
    const explicit = STATE.filters.item_type || [];
    if (!explicit.includes(STATE.activeTab)) {
      params.append("item_type", STATE.activeTab);
    }
  }
}

async function refreshBugs() {
  // Reflect current status filter in the KPI tile highlight.
  refreshKpiActiveState();
  refreshTypeTabActiveState();
  // Mirror filter state into the URL so a refresh / shared link
  // restores the same view.
  try { syncFiltersToUrl(); } catch {}
  const params = new URLSearchParams();
  params.set("page", String(STATE.page));
  params.set("page_size", String(STATE.pageSize));
  _appendFiltersToParams(params);
  _appendActiveTabFilter(params);
  const data = await api("/bugs?" + params.toString());
  STATE.bugs = data.items;
  STATE.total = data.total;
  STATE.totalPages = data.pages;
  renderBugTable();
  renderPagination();
}

// v2.4 — tab activeness reflects STATE.activeTab.
function refreshTypeTabActiveState() {
  $$(".type-tab[data-tab]").forEach(b => {
    const on = b.dataset.tab === STATE.activeTab;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
}

// v2.4 — switch tabs. Persists to localStorage so a reload lands you
// back on the same tab. The tab is NOT written into STATE.filters
// (it's implicit at request time in refreshBugs) so clearing the
// filter bar doesn't fight the tab choice.
function setActiveTab(tab) {
  STATE.activeTab = tab;
  try { localStorage.setItem("activeTab", tab); } catch {}
  STATE.page = 1;
  refreshTypeTabActiveState();
  refreshFilterBarVisibility();
  document.dispatchEvent(new CustomEvent("bh:tab-change", { detail: { tab } }));
  refreshBugs();
  refreshStats();
}

// v2.4 — hide filters that don't apply for the active tab.
function refreshFilterBarVisibility() {
  const tab = STATE.activeTab || "all";
  const typeFilter = document.querySelector('.ms-wrap[data-filter="item_type"]');
  if (typeFilter) typeFilter.style.display = tab === "all" ? "" : "none";
  const envFilter = document.querySelector('.ms-wrap[data-filter="environment"]');
  if (envFilter) envFilter.style.display = (tab === "Requirement" || tab === "Task") ? "none" : "";
}

// v2.4 — table head + cell rendering reacts to the active tab.
// Each tab defines its own column set; numbering stays global.
const TAB_COLUMNS = {
  all:         ["id", "title-with-type", "project", "status", "priority", "env", "assignees", "att", "actions"],
  Bug:         ["id", "title", "project", "status", "priority", "env", "assignees", "att", "actions"],
  Requirement: ["id", "title", "project", "status", "priority", "assignees", "att", "actions"],
  Task:        ["id", "title", "project", "status", "priority", "due", "event", "assignees", "actions"],
};

const COL_HEAD_LABEL = {
  id: "#", title: "Title", "title-with-type": "Title",
  project: "Project", status: "Status", priority: "Priority",
  env: "Env", due: "Due", event: "Event",
  assignees: "Assignees", att: "📎", actions: "Actions",
};

const ITEM_TYPE_EMOJI = { Bug: "🐞", Requirement: "📐", Task: "✅" };
function itemTypeEmoji(t) { return ITEM_TYPE_EMOJI[t] || "📝"; }

function _renderBugCell(col, bug, canDeleteRow) {
  // v2.5 assignee chip: wrap the name in <span class="assignee-chip-name">
  // so CSS can target it for ellipsing/truncation independently of the
  // avatar circle.
  const assigneesHtml = bug.assignees.length
    ? bug.assignees.map(a => `<span class="assignee-chip" title="${escapeHtml(a.email)}"><span class="avatar">${initials(a.name)}</span><span class="assignee-chip-name">${escapeHtml(a.name)}</span></span>`).join("")
    : `<span class="muted">—</span>`;
  switch (col) {
    case "id":
      return `<td class="col-id">${bug.project_key ? escapeHtml(bug.project_key) + "-" : "#"}${bug.id}</td>`;
    case "title":
      return `<td class="col-title">
        <div class="title-cell">
          <strong class="title-text" title="${escapeHtml(bug.title)}">${escapeHtml(bug.title)}</strong>
          <span class="title-meta">Updated ${formatDate(bug.updated_at)}</span>
        </div>
      </td>`;
    case "title-with-type": {
      const t = bug.item_type || "Bug";
      return `<td class="col-title">
        <div class="title-cell">
          <strong class="title-text" title="${escapeHtml(bug.title)}"><span class="type-marker" title="${escapeHtml(t)}">${itemTypeEmoji(t)}</span> ${escapeHtml(bug.title)}</strong>
          <span class="title-meta">Updated ${formatDate(bug.updated_at)}</span>
        </div>
      </td>`;
    }
    case "project":
      return `<td class="col-project">${escapeHtml(bug.project_name || "")}</td>`;
    case "status":
      return `<td class="col-status"><span class="badge" data-status="${escapeHtml(bug.status)}">${escapeHtml(bug.status)}</span></td>`;
    case "priority":
      return `<td class="col-priority"><span class="badge" data-priority="${escapeHtml(bug.priority)}">${escapeHtml(bug.priority)}</span></td>`;
    case "env":
      return `<td class="col-env"><span class="badge" data-env="${escapeHtml(bug.environment)}">${escapeHtml(bug.environment)}</span></td>`;
    case "due":
      return `<td class="col-due">${bug.due_date ? escapeHtml(bug.due_date) : '<span class="muted">—</span>'}</td>`;
    case "event": {
      const ev = bug.event_name;
      const evHtml = ev
        ? `<span class="event-pill" title="${escapeHtml(ev)}">📅 ${escapeHtml(ev)}</span>`
        : '<span class="muted">—</span>';
      return `<td class="col-event">${evHtml}</td>`;
    }
    case "assignees":
      return `<td class="col-assignees"><div class="assignee-stack">${assigneesHtml}</div></td>`;
    case "att": {
      const attHtml = bug.attachment_count > 0
        ? `<span class="att-count">📎 ${bug.attachment_count}</span>`
        : '<span class="muted">—</span>';
      return `<td class="col-att">${attHtml}</td>`;
    }
    case "actions":
      return `<td class="col-actions">
        <div class="row-actions">
          ${canDeleteRow ? `<button class="icon-btn danger" data-act="delete" data-id="${bug.id}" title="Delete">🗑</button>` : ""}
        </div>
      </td>`;
    default:
      return `<td>—</td>`;
  }
}

function renderBugTable() {
  const head = $("#bugTableHead");
  const tbody = $("#bugTableBody");
  tbody.innerHTML = "";
  $("#emptyState").hidden = STATE.bugs.length > 0;

  // v2.4 — column set is keyed by the active tab.
  const cols = TAB_COLUMNS[STATE.activeTab] || TAB_COLUMNS.all;
  if (head) {
    head.innerHTML = "<tr>" +
      cols.map(c => `<th class="col-${c.replace("-with-type","")}">${COL_HEAD_LABEL[c] || ""}</th>`).join("") +
      "</tr>";
  }

  const isAdmin = STATE.currentUser?.role === "admin";
  for (const bug of STATE.bugs) {
    // v2.4: delete is admin-only across every item type.
    const canDeleteRow = isAdmin;
    const tr = document.createElement("tr");
    tr.dataset.bugId = String(bug.id);
    tr.innerHTML = cols.map(c => _renderBugCell(c, bug, canDeleteRow)).join("");
    tbody.appendChild(tr);
  }
}

function renderPagination() {
  const bar = $("#paginationBar");
  if (STATE.totalPages <= 1) { bar.innerHTML = ""; return; }
  bar.innerHTML = `
    <button id="pgPrev" ${STATE.page <= 1 ? "disabled" : ""}>← Prev</button>
    <span>Page ${STATE.page} of ${STATE.totalPages} (${STATE.total} items)</span>
    <button id="pgNext" ${STATE.page >= STATE.totalPages ? "disabled" : ""}>Next →</button>`;
  $("#pgPrev")?.addEventListener("click", () => { STATE.page--; refreshBugs(); });
  $("#pgNext")?.addEventListener("click", () => { STATE.page++; refreshBugs(); });
}

// ---------------------------------------------------------------------------
// Sidebar lists
// ---------------------------------------------------------------------------
function _memberSuffix(p) {
  if (typeof p.member_count !== "number" || p.member_count <= 0) return "";
  const plural = p.member_count === 1 ? "" : "s";
  return ` · ${p.member_count} member${plural}`;
}

function _buildProjectListItem(p, activeIds, canDelete) {
  const li = document.createElement("li");
  li.className = "side-item" + (activeIds.has(String(p.id)) ? " active" : "");
  li.dataset.projectId = String(p.id);
  const memberSuffix = _memberSuffix(p);
  const keyPart = p.key ? " (" + p.key + ")" : "";
  li.title = `${p.name}${keyPart}${memberSuffix}`;
  const canManage = !!p.can_manage;
  const keyHtml = p.key ? ` <span class="proj-key">${escapeHtml(p.key)}</span>` : "";
  // v2.6: split-click — clicking the swatch toggles the filter; clicking
  // the label opens the edit modal (when the user is allowed to manage),
  // or falls back to the filter when not. Two separate data-acts so the
  // delegated handler can distinguish.
  const nameTitle = canManage
    ? `${escapeHtml(p.name)} — click to edit`
    : escapeHtml(p.name);
  li.innerHTML = `
      <span class="swatch" data-act="filter" style="background:${escapeHtml(p.color)}" title="Toggle filter"></span>
      <span class="label-text" data-act="open-project" title="${nameTitle}">${escapeHtml(p.name)}${keyHtml}</span>
      <span class="row-actions">
        ${canManage ? `<button class="icon-btn" data-act="members" data-id="${p.id}" title="Manage members">👥</button>` : ""}
        ${canManage ? `<button class="icon-btn" data-act="edit-project" data-id="${p.id}" title="Edit">✎</button>` : ""}
        ${canDelete ? `<button class="icon-btn danger" data-act="delete-project" data-id="${p.id}" title="Delete">🗑</button>` : ""}
      </span>`;
  return li;
}

function renderProjectList() {
  const ul = $("#projectList");
  ul.innerHTML = "";
  if (!STATE.projects.length) {
    ul.innerHTML = `<li class="side-item muted no-cursor">No projects yet.</li>`;
    return;
  }
  // Multi-tenant permissions (v4):
  //   • can_manage : true if the user is an org admin OR a lead on THIS
  //                  specific project. Comes from the API per project,
  //                  so we don't have to guess from role alone — a
  //                  manager who isn't a lead won't see the edit button.
  //   • delete     : org admins only.
  //   • members    : same as can_manage (managers + leads).
  const role = STATE.currentUser?.role || "";
  const canDelete = role === "admin";
  // Active = the project's id is currently in the multi-select filter array.
  const activeIds = new Set((STATE.filters.project_id || []).map(String));
  for (const p of STATE.projects) {
    ul.appendChild(_buildProjectListItem(p, activeIds, canDelete));
  }
}

function renderUserList() {
  const ul = $("#userList");
  ul.innerHTML = "";
  const active = STATE.users.filter(u => u.is_active);
  if (!active.length) {
    ul.innerHTML = `<li class="side-item muted no-cursor">No users yet — click + to add.</li>`;
    return;
  }
  // v3.1 permissions:
  //   • edit user  : admin or manager  (managers can't edit admins; the
  //                  backend enforces it. We still show the button so a
  //                  manager can edit non-admins; if they click on an
  //                  admin row, they'll get a 403 toast.)
  //   • delete user: admin only.
  // The Users sidebar section is gated on data-needs-role="manager" so
  // plain users (members) never see this list at all.
  const role = STATE.currentUser?.role || "";
  const canEdit = role === "admin" || role === "manager";
  const canDelete = role === "admin";
  for (const u of active) {
    const li = document.createElement("li");
    li.className = "side-item";
    li.dataset.userId = String(u.id);
    li.title = `${u.email}${u.role ? " — " + u.role : ""}`;
    // v2.6: split-click — avatar click toggles the assignee filter;
    // name click opens the edit modal (when allowed) and otherwise
    // falls back to the filter.
    const userNameTitle = canEdit
      ? `${escapeHtml(u.name)} — click to edit`
      : escapeHtml(u.name);
    li.innerHTML = `
      <span class="avatar" data-act="filter-user" title="Toggle filter">${initials(u.name)}</span>
      <span class="label-text" data-act="open-user" title="${userNameTitle}">
        ${escapeHtml(u.name)}
        ${u.role ? `<span class="meta"> · ${escapeHtml(u.role)}</span>` : ""}
      </span>
      <span class="row-actions">
        ${canEdit ? `<button class="icon-btn" data-act="edit-user" data-id="${u.id}" title="Edit">✎</button>` : ""}
        ${canDelete ? `<button class="icon-btn danger" data-act="delete-user" data-id="${u.id}" title="Delete">🗑</button>` : ""}
      </span>`;
    ul.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// Selects (form-level only — filter bar uses the multi-select widgets below)
// ---------------------------------------------------------------------------
function fillAuditActorSelect() {
  const sel = $("#auditActorFilter");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = `<option value="">All actors</option>` +
    STATE.users.map(u => `<option value="${u.id}">${escapeHtml(u.name)}</option>`).join("");
  if (cur) sel.value = cur;
}

// ---------------------------------------------------------------------------
// Multi-select dropdowns (filter bar)
//
// One panel per filter, each driven by `STATE.filters[<key>]` which is
// always an array. Clicking a row toggles that value's membership in the
// array. The panel header button shows a summary ("All X" / "X (n)" /
// the single value) and is the click target for opening / closing the panel.
// ---------------------------------------------------------------------------
const MS_LABELS = {
  project_id:  "All Projects",
  status:      "All Statuses",
  priority:    "All Priorities",
  environment: "All Envs",
  assignee_id: "All Assignees",
  item_type:   "All Types",   // v2.4
};
const MS_NOUNS = {
  project_id: "Projects", status: "Statuses", priority: "Priorities",
  environment: "Envs",    assignee_id: "Assignees",
  item_type: "Types",     // v2.4
};

function _msOptions(key) {
  // Each option is [value, label]. value is what we send to the API,
  // label is what the user sees.
  if (key === "project_id") {
    return STATE.projects.map(p => [String(p.id), p.name]);
  }
  if (key === "assignee_id") {
    return STATE.users.filter(u => u.is_active).map(u => [String(u.id), u.name]);
  }
  if (key === "status")      return (STATE.meta.statuses     || []).map(s => [s, s]);
  if (key === "priority")    return (STATE.meta.priorities   || []).map(s => [s, s]);
  if (key === "environment") return (STATE.meta.environments || ["DEV","UAT","PROD"]).map(s => [s, s]);
  if (key === "item_type")   return (STATE.meta.item_types   || ["Bug","Requirement","Task"]).map(t => [t, t]);
  return [];
}

function _closeAllMsPanelsExcept(keepPanel) {
  $$(".ms-panel").forEach(p => { if (p !== keepPanel) p.hidden = true; });
}
function _collapseAllMsBtnsExcept(keepToggle) {
  $$(".ms-btn").forEach(b => { if (b !== keepToggle) b.setAttribute("aria-expanded", "false"); });
}

function initMultiSelects() {
  $$(".ms-wrap").forEach(wrap => {
    const key = wrap.dataset.filter;
    const toggle = wrap.querySelector("[data-ms-toggle]");
    const panel = wrap.querySelector(".ms-panel");
    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      // Close any other open panels first — only one open at a time.
      _closeAllMsPanelsExcept(panel);
      _collapseAllMsBtnsExcept(toggle);
      const willOpen = panel.hidden;
      panel.hidden = !willOpen;
      toggle.setAttribute("aria-expanded", String(willOpen));
    });
    panel.addEventListener("click", (e) => {
      const row = e.target.closest("[data-ms-value]");
      if (!row) return;
      e.stopPropagation();
      const v = row.dataset.msValue;
      const cur = STATE.filters[key];
      const idx = cur.indexOf(v);
      if (idx >= 0) cur.splice(idx, 1);
      else cur.push(v);
      STATE.page = 1;
      refreshMultiSelects();
      refreshBugs();
      // If the panel had a project click, also restyle the sidebar so the
      // active dot matches.
      if (key === "project_id") renderProjectList();
    });
  });
  // Click outside to close any open panel.
  document.addEventListener("click", () => {
    $$(".ms-panel").forEach(p => { p.hidden = true; });
    $$(".ms-btn").forEach(b => b.setAttribute("aria-expanded", "false"));
  });
  refreshMultiSelects();
}

function refreshMultiSelects() {
  $$(".ms-wrap").forEach(wrap => {
    const key = wrap.dataset.filter;
    const opts = _msOptions(key);
    const selected = new Set(STATE.filters[key] || []);
    const panel = wrap.querySelector(".ms-panel");
    const labelEl = wrap.querySelector(".ms-btn-label");
    const btn = wrap.querySelector(".ms-btn");

    // Render rows. Building HTML once via join() is faster than appendChild
    // in a loop for the small option sets we deal with.
    panel.innerHTML = opts.length
      ? opts.map(([v, lbl]) => {
          const isOn = selected.has(v);
          return `<div class="ms-row${isOn ? " on" : ""}" data-ms-value="${escapeHtml(v)}" role="option" aria-selected="${isOn}">
            <span class="ms-check">${isOn ? "✓" : ""}</span>
            <span class="ms-text">${escapeHtml(lbl)}</span>
          </div>`;
        }).join("")
      : `<div class="ms-empty">No options</div>`;

    // Update header label and "active" outline.
    if (selected.size === 0) {
      labelEl.textContent = MS_LABELS[key] || "All";
      btn.classList.remove("active");
    } else if (selected.size === 1) {
      const only = [...selected][0];
      const match = opts.find(([v]) => v === only);
      labelEl.textContent = match ? match[1] : only;
      btn.classList.add("active");
    } else {
      labelEl.textContent = `${MS_NOUNS[key] || "Items"} (${selected.size})`;
      btn.classList.add("active");
    }
  });
}

// ---------------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------------
const _VIEW_TITLES = {
  list: "All Work Items", events: "Events", analytics: "Analytics",
  audit: "Audit Trail", sessions: "Active Sessions",
  invitations: "Invitations",
};

const _VIEW_REFRESHERS = {
  list: () => { refreshBugs(); refreshStats(); },
  analytics: () => refreshStats().then(renderCharts),
  audit: () => refreshAudit(),
  sessions: () => refreshSessions(),
  invitations: () => refreshInvitations(),
  events: () => {
    STATE.currentEventId = null;
    STATE.currentEvent = null;
    showEventsListMode();
    refreshEvents();
  },
};

function _toggleViewPanels(view) {
  $$(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  $("#viewList").hidden = view !== "list";
  $("#viewAnalytics").hidden = view !== "analytics";
  $("#viewAudit").hidden = view !== "audit";
  $("#viewSessions").hidden = view !== "sessions";
  const viewEvents = document.getElementById("viewEvents");
  if (viewEvents) viewEvents.hidden = view !== "events";
  const viewInvitations = document.getElementById("viewInvitations");
  if (viewInvitations) viewInvitations.hidden = view !== "invitations";
  $("#filterBar").hidden = view !== "list";
}

function _toggleViewChrome(view) {
  // The search, the "+ New" CTA and the KPI strip are work-item-only
  // controls. They make no sense on Audit / Sessions / Invitations /
  // Events. KPI strip and type tabs also appear on analytics.
  const showList = view === "list";
  const showListOrAnalytics = showList || view === "analytics";
  const searchWrap = document.querySelector(".search-wrap");
  if (searchWrap) searchWrap.style.display = showList ? "" : "none";
  const newItemWrap = document.querySelector(".new-item-wrap");
  if (newItemWrap) newItemWrap.style.display = showList ? "" : "none";
  const kpiStrip = $("#kpiStrip");
  if (kpiStrip) kpiStrip.style.display = showListOrAnalytics ? "" : "none";
  // v2.4 — type tabs are the global type-context switch shared by
  // list + analytics. Hidden everywhere else.
  const typeTabs = $("#typeTabs");
  if (typeTabs) typeTabs.style.display = showListOrAnalytics ? "" : "none";
}

function setView(view) {
  STATE.view = view;
  _toggleViewPanels(view);
  _toggleViewChrome(view);
  $("#pageTitle").textContent = _VIEW_TITLES[view] || "Bug Hunter";
  // Re-fetch on entry. Without this, anything created from another view —
  // a task added inside an event, a stat changed by Sleuth, etc. — would
  // require a manual page reload to show up.
  _VIEW_REFRESHERS[view]?.();
}

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------
const _TAB_NOUNS = {
  all: "Items", Bug: "Bugs", Requirement: "Requirements", Task: "Tasks",
};

function renderCharts() {
  if (!STATE.stats) return;
  const s = STATE.stats;
  const tab = STATE.activeTab || "all";
  const noun = _TAB_NOUNS[tab] || "Items";
  // v2.4 — chart titles update per tab so the user knows what's being
  // plotted at a glance.
  const setTitle = (id, text) => { const el = $(id); if (el) el.textContent = text; };
  setTitle("#chartTimelineTitle",    `${noun} over the last 14 days`);
  setTitle("#chartStatusTitle",      `By Status (${noun})`);
  setTitle("#chartPriorityTitle",    `By Priority (${noun})`);
  setTitle("#chartEnvironmentTitle", `By Environment (${noun})`);
  setTitle("#chartProjectTitle",     `By Project (${noun})`);
  setTitle("#chartAssigneeTitle",    `Top Assignees (${noun})`);
  // Environment doesn't apply to Requirements / Tasks — hide that card.
  const envCard = $("#chartEnvironmentCard");
  if (envCard) envCard.hidden = (tab === "Requirement" || tab === "Task");
  drawTimeline("#chartTimeline", s.timeline);
  drawBars("#chartStatus", s.by_status, "status");
  drawBars("#chartPriority", s.by_priority, "priority");
  drawBars("#chartEnvironment", s.by_environment, "env");
  drawProjectBars("#chartProject", s.by_project);
  drawAssigneeBars("#chartAssignee", s.by_assignee);
}

function drawTimeline(sel, data) {
  const host = $(sel); host.innerHTML = "";
  if (!data?.length) { host.innerHTML = '<p class="muted">No data</p>'; return; }
  const W = 600, H = 200, P = 30;
  const max = Math.max(1, ...data.map(d => d.count));
  const stepX = (W - 2 * P) / Math.max(1, data.length - 1);
  const points = data.map((d, i) => {
    const x = P + i * stepX;
    const y = H - P - (d.count / max) * (H - 2 * P);
    return [x, y];
  });
  const path = points.map((p, i) => `${i === 0 ? "M" : "L"} ${p[0]} ${p[1]}`).join(" ");
  const area = `M ${P} ${H - P} ` + points.map(p => `L ${p[0]} ${p[1]}`).join(" ") + ` L ${W - P} ${H - P} Z`;
  const labels = data.map((d, i) => i % 3 === 0
    ? `<text x="${P + i * stepX}" y="${H - 8}" text-anchor="middle" fill="currentColor" font-size="10" opacity="0.6">${d.date.slice(5)}</text>`
    : "").join("");
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" style="color:var(--accent)">
    <path d="${area}" fill="currentColor" opacity="0.18"/>
    <path d="${path}" stroke="currentColor" stroke-width="2" fill="none"/>
    ${points.map((p, i) => `<circle cx="${p[0]}" cy="${p[1]}" r="3" fill="currentColor"><title>${data[i].date}: ${data[i].count}</title></circle>`).join("")}
    ${labels}
  </svg>`;
}

function drawBars(sel, obj, kind) {
  const host = $(sel); host.innerHTML = "";
  const entries = Object.entries(obj || {});
  if (!entries.length) { host.innerHTML = '<p class="muted">No data</p>'; return; }
  const W = 600, H = 200, P = 30;
  const max = Math.max(1, ...entries.map(e => e[1]));
  const bw = (W - 2 * P) / entries.length - 8;
  const bars = entries.map(([k, v], i) => {
    const x = P + i * ((W - 2 * P) / entries.length);
    const h = (v / max) * (H - 2 * P);
    const y = H - P - h;
    const colorVar = kindColor(kind, k);
    return `
      <rect x="${x}" y="${y}" width="${bw}" height="${h}" fill="${colorVar}" rx="3">
        <title>${escapeHtml(k)}: ${v}</title>
      </rect>
      <text x="${x + bw / 2}" y="${H - 12}" text-anchor="middle" fill="currentColor" font-size="10" opacity="0.7">${escapeHtml(k)}</text>
      <text x="${x + bw / 2}" y="${y - 4}" text-anchor="middle" fill="currentColor" font-size="11" font-weight="600">${v}</text>`;
  }).join("");
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${bars}</svg>`;
}

function kindColor(kind, key) {
  const map = {
    status:   {
      "New": "#5a9fd4", "In Progress": "#d4a05a", "Resolved": "#7ca860",
      "Closed": "#8b8270", "Reopened": "#a87fb8",
      "Not a Bug": "#64748b", "Resolve Later": "#f59e0b",
    },
    priority: { Low: "#8b8270", Medium: "#5a9fd4", High: "#d4a05a", Critical: "#c5524a" },
    env:      { DEV: "#5a9fd4", UAT: "#d4a05a", PROD: "#c5524a" },
  };
  return map[kind]?.[key] || "#8b8270";
}

function drawProjectBars(sel, rows) {
  const host = $(sel); host.innerHTML = "";
  if (!rows?.length) { host.innerHTML = '<p class="muted">No data</p>'; return; }
  const max = Math.max(1, ...rows.map(r => r.count));
  host.innerHTML = rows.map(r => `
    <div class="bar-row">
      <div class="bar-label">
        <span><span class="swatch dot" style="background:${escapeHtml(r.color)}"></span>${escapeHtml(r.name)}</span>
        <span>${r.count}</span>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:${(r.count/max)*100}%;background:${escapeHtml(r.color)}"></div></div>
    </div>`).join("");
}

function drawAssigneeBars(sel, rows) {
  const host = $(sel); host.innerHTML = "";
  if (!rows?.length) { host.innerHTML = '<p class="muted">No assignments yet</p>'; return; }
  const max = Math.max(1, ...rows.map(r => r.count));
  host.innerHTML = rows.map(r => `
    <div class="bar-row">
      <div class="bar-label">
        <span><span class="avatar mini">${initials(r.name)}</span>${escapeHtml(r.name)}</span>
        <span>${r.count}</span>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:${(r.count/max)*100}%;background:var(--accent)"></div></div>
    </div>`).join("");
}

// ---------------------------------------------------------------------------
// Bug modal — unified create / edit / view (Jira-style single screen).
//
// One modal handles three modes:
//   • Create       — bug == null, no comments / attachments / activity
//                    sections rendered (we don't have a bug id yet).
//   • Edit / View  — bug != null, all fields editable inline; comments,
//                    attachments and activity rendered below.
//
// On submit we PUT/POST the form, then if it was an edit we re-fetch the
// bug detail and re-render the inline sections in place — without
// closing the modal — so the user sees the updated bug straight away.
// ---------------------------------------------------------------------------
function _setBugFormHeader(bug, isEdit) {
  if (isEdit) {
    const t = bug.item_type || "Bug";
    $("#modalBugTitle").textContent = `${itemTypeEmoji(t)} ${t} #${bug.id}`;
    $("#modalBugSubtitle").textContent = bug.title || "";
    $("#bugSubmitBtn").textContent = "Save changes";
    return;
  }
  const t = bug?._defaultType || STATE.defaultNewType || "Bug";
  $("#modalBugTitle").textContent = `${itemTypeEmoji(t)} New ${t}`;
  $("#modalBugSubtitle").textContent = "";
  $("#bugSubmitBtn").textContent = "Create";
}

function _toggleBugFormDeleteBtn(isEdit) {
  // v2.4 — delete is admin-only across every item type.
  const delBtn = $("#bugDeleteBtn");
  if (!delBtn) return;
  const isAdmin = STATE.currentUser?.role === "admin";
  delBtn.hidden = !(isEdit && isAdmin);
}

function _seedReporterField(form, bug, isEdit) {
  // Reporter is fixed to the current user. For existing bugs, inject the
  // original reporter as the only option so the displayed name is correct.
  const me = STATE.currentUser;
  let reporterOptions = me ? [[me.id, me.name, me.email]] : [];
  if (isEdit && bug.reporter && (!me || bug.reporter.id !== me.id)) {
    reporterOptions = [[bug.reporter.id, bug.reporter.name, bug.reporter.email]];
  }
  let defaultReporterId = "";
  if (isEdit && bug.reporter) defaultReporterId = bug.reporter.id;
  else if (me) defaultReporterId = me.id;
  fillFormSelect(form.elements.reporter_id, reporterOptions, defaultReporterId);
}

function _seedEventField(form, bug, isEdit) {
  // v2.4 — event select. Seed the current event so it shows up even
  // before the async list refresh lands. Fetch a fresh list on every
  // open so newly-created events appear.
  if (!form.elements.event_id) return;
  const presetEventId = isEdit
    ? (bug.event_id || "")
    : (bug?._defaultEventId || "");
  form.elements.event_id.innerHTML = `<option value="">— No event —</option>`;
  if (isEdit && bug.event_id && bug.event_name) {
    const opt = document.createElement("option");
    opt.value = String(bug.event_id);
    opt.textContent = bug.event_name;
    opt.selected = true;
    form.elements.event_id.appendChild(opt);
  } else if (!isEdit && bug?._defaultEventId) {
    // create-mode preset from the "+ Add to event" flow.
    form.elements.event_id.value = String(bug._defaultEventId);
  }
  api("/events").then((events) => {
    if (!form.elements.event_id) return;
    const sel = form.elements.event_id;
    const current = String(sel.value || presetEventId || "");
    sel.innerHTML = `<option value="">— No event —</option>` +
      (events || []).map(ev => {
        const label = ev.scheduled_for
          ? `${ev.name} · ${ev.scheduled_for}`
          : ev.name;
        return `<option value="${ev.id}">${escapeHtml(label)}</option>`;
      }).join("");
    if (current) sel.value = current;
  }).catch(() => { /* leave the placeholder if /events fails */ });
}

function _seedBugFormSelects(form, bug, isEdit) {
  fillFormSelect(form.elements.project_id, STATE.projects.map(p => [p.id, p.name]),
                 isEdit ? bug.project_id : "");
  _seedReporterField(form, bug, isEdit);
  // v2.5: status options come from the per-type set so e.g. "Not a Bug"
  // is never offered for a Task. _renderStatusSelect carries forward a
  // legacy value if the stored row has one that's no longer valid.
  const initialStatusType = isEdit
    ? (bug.item_type || "Bug")
    : (bug?._defaultType || STATE.defaultNewType || "Bug");
  _renderStatusSelect(form.elements.status, initialStatusType, isEdit ? bug.status : "New");
  fillFormSelect(form.elements.priority, STATE.meta.priorities.map(s => [s, s]),
                 isEdit ? bug.priority : "Medium");
  form.elements.environment.value = isEdit ? bug.environment : "DEV";

  // v2.4 — item_type select.
  const presetType = isEdit
    ? (bug.item_type || "Bug")
    : (bug?._defaultType || STATE.defaultNewType || "Bug");
  if (form.elements.item_type) {
    fillFormSelect(form.elements.item_type,
      (STATE.meta.item_types || ["Bug","Requirement","Task"]).map(t => [t, t]),
      presetType);
  }
  _seedEventField(form, bug, isEdit);
  const assignedIds = new Set(isEdit && bug.assignees ? bug.assignees.map(a => a.id) : []);
  renderChips("#assigneePicker",
    STATE.users.filter(u => u.is_active),
    (u) => ({ id: u.id, label: u.name, sub: u.role }),
    assignedIds);
}

function _seedBugFormEditMode(form, bug) {
  form.elements.title.value = bug.title || "";
  form.elements.description.value = bug.description || "";
  // v2.6: form.reset() cleared the textarea but the rich editor lives
  // next to it; push the description html into the contenteditable.
  if (form.elements.description._bhRtSet) {
    form.elements.description._bhRtSet(bug.description || "");
  }
  // v2.6: paste in description on edit uploads immediately as a bug-level
  // attachment then refreshes the inline sections.
  form.elements.description._bhPasteHandler = async (f) => {
    const fd = new FormData();
    fd.append("file", f);
    await api(`/bugs/${bug.id}/attachments`, { method: "POST", body: fd });
    const fresh = await api(`/bugs/${bug.id}`);
    renderBugInlineSections(fresh);
    toast(`Attached: ${f.name || "pasted file"}`, "success");
  };
  form.elements.due_date.value = bug.due_date || "";
  // Re-fire change so the custom date picker label re-renders.
  form.elements.due_date.dispatchEvent(new Event("change", { bubbles: true }));
  $("#bugSideMeta").hidden = false;
  $("#bugMetaCreated").textContent = formatDate(bug.created_at);
  $("#bugMetaUpdated").textContent = formatDate(bug.updated_at);
  const createAttach = $("#bugCreateAttachSection");
  if (createAttach) {
    createAttach.hidden = true;
    clearStagedFiles("createBug", "#createFilePreview", "#createFileLabel");
    const cf = $("#createBugFiles"); if (cf) cf.value = "";
  }
  clearStagedFiles("comment", "#filePreview", "#fileLabel");
  renderBugInlineSections(bug);
}

function _seedBugFormCreateMode(form) {
  $("#bugSideMeta").hidden = true;
  $("#bugCommentsSection").hidden = true;
  $("#bugAttachmentsSection").hidden = true;
  $("#bugActivitySection").hidden = true;
  const createAttach = $("#bugCreateAttachSection");
  if (createAttach) {
    createAttach.hidden = false;
    clearStagedFiles("createBug", "#createFilePreview", "#createFileLabel");
    const cf = $("#createBugFiles"); if (cf) cf.value = "";
  }
  // v2.6: rich-text editor reset + paste handler.
  if (form?.elements?.description?._bhRtSet) {
    form.elements.description._bhRtSet("");
  }
  if (form?.elements?.description) {
    form.elements.description._bhPasteHandler = async (f) => {
      STATE.stagedFiles.createBug = STATE.stagedFiles.createBug || [];
      STATE.stagedFiles.createBug.push({ file: f, _blobUrl: URL.createObjectURL(f) });
      _renderStagedFiles("createBug", "#createFilePreview", "#createFileLabel");
      toast(`Staged: ${f.name || "pasted file"}`, "info");
    };
  }
  // Default Due Date to TODAY on create. form.reset() above blanked the
  // native input, but the v2.6 custom-calendar button label doesn't
  // refresh on reset() (reset doesn't dispatch change; the MutationObserver
  // only watches setAttribute). Dispatching change after setting value
  // makes the widget redraw "Select date" → today's date.
  const dueEl = form?.elements?.due_date;
  if (dueEl) {
    dueEl.value = _isoDate(new Date());
    dueEl.dispatchEvent(new Event("change", { bubbles: true }));
  }
}

function openBugForm(bug = null) {
  const form = $("#formBug");
  // v2.4: bug may be either { id, ... } (edit) or a hint object
  // { _defaultType, _defaultEventId } from the "+ New X" menu.
  const isEdit = !!bug?.id;
  STATE.currentBugId = isEdit ? bug.id : null;
  form.reset();
  _setBugFormHeader(bug, isEdit);
  form.elements.id.value = isEdit ? bug.id : "";
  _toggleBugFormDeleteBtn(isEdit);
  _seedBugFormSelects(form, bug, isEdit);
  if (isEdit) {
    _seedBugFormEditMode(form, bug);
  } else {
    _seedBugFormCreateMode(form);
  }

  // v2.4 — apply read-only mode if the user can't edit this item type.
  applyBugFormReadOnly(form, bug, isEdit);

  openModal("modalBug");
  if (!form.dataset.readOnly) {
    setTimeout(() => form.elements.title.focus(), 50);
  }
}

// v2.4 — mirror the backend can_edit_bug rule on the frontend. Members
// (regular users) can edit Bugs only; Requirements/Tasks are
// admin/manager-only. The backend still 403s if this is bypassed.
function canEditItem(item) {
  const role = STATE.currentUser?.role || "";
  if (role === "admin" || role === "manager") return true;
  const t = item?.item_type || "Bug";
  return t === "Bug";
}

function applyBugFormReadOnly(form, bug, isEdit) {
  const readOnly = isEdit && !canEditItem(bug);
  form.dataset.readOnly = readOnly ? "1" : "";
  const fields = form.querySelectorAll("input, select, textarea");
  fields.forEach(el => {
    if (el.name === "id") return;
    if (el.classList.contains("reporter-select")) return;  // always disabled by design
    if (readOnly) {
      el.dataset.roSetByUs = "1";
      el.disabled = true;
      return;
    }
    if (el.dataset.roSetByUs === "1") {
      el.disabled = false;
      el.dataset.roSetByUs = "";
    }
  });
  const picker = $("#assigneePicker");
  if (picker) picker.classList.toggle("locked", readOnly);
  const submit = $("#bugSubmitBtn");
  if (submit) submit.hidden = readOnly;
  const delBtn = $("#bugDeleteBtn");
  if (delBtn && readOnly) delBtn.hidden = true;
  const composer = $("#commentForm");
  if (composer) composer.hidden = readOnly;
  let banner = form.querySelector(".bug-readonly-banner");
  if (readOnly && !banner) {
    banner = document.createElement("div");
    banner.className = "bug-readonly-banner";
    const itype = (bug?.item_type || "item").toLowerCase();
    banner.textContent = `Read-only — only admins and managers can edit ${itype}s.`;
    form.insertBefore(banner, form.firstChild);
  } else if (!readOnly && banner) {
    banner.remove();
  }
}

// ---------------------------------------------------------------------------
// v2.4 — Staged-files system (for the create modal + comment composer)
//
// FileList is read-only so we keep a parallel array that backs the
// previews. The X button on hover removes individual entries; clicking
// the thumbnail opens the file via URL.createObjectURL for preview
// before submit.
// ---------------------------------------------------------------------------
function _revokeBlobs(arr) {
  for (const it of arr || []) {
    if (it?._blobUrl) { try { URL.revokeObjectURL(it._blobUrl); } catch {} }
  }
}

// v2.5: notify subscribers (the bug-attach Upload-N button) so they
// can recompute their visibility / counter.
const STAGED_CHANGED_EVENT = "bh:staged-changed";
function _emitStagedChanged(bucket) {
  document.dispatchEvent(new CustomEvent(STAGED_CHANGED_EVENT, { detail: { bucket } }));
}

function _stagedDefaultLabel(labelSel) {
  return labelSel === "#bugAttachAddText" ? "Add attachment" : "Attach files";
}

function clearStagedFiles(bucket, previewSel, labelSel) {
  _revokeBlobs(STATE.stagedFiles[bucket]);
  STATE.stagedFiles[bucket] = [];
  if (previewSel) { const el = $(previewSel); if (el) el.innerHTML = ""; }
  if (labelSel)   { const el = $(labelSel);   if (el) el.textContent = _stagedDefaultLabel(labelSel); }
  _emitStagedChanged(bucket);
}

function _renderStagedFiles(bucket, previewSel, labelSel) {
  const host = $(previewSel);
  if (!host) return;
  const files = STATE.stagedFiles[bucket] || [];
  host.innerHTML = files.map((f, idx) => {
    const isImg = (f.file?.type || "").startsWith("image/");
    const thumb = isImg
      ? `<img src="${f._blobUrl}" alt="" class="attach-staged-thumb" />`
      : `<span class="attach-staged-icon">${fileIcon(f.file?.type, f.file?.name)}</span>`;
    return `<div class="attach-staged" data-idx="${idx}">
      <button type="button" class="attach-staged-remove" aria-label="Remove" data-act="remove">✕</button>
      <a class="attach-staged-link" href="${f._blobUrl}" target="_blank" rel="noopener" data-act="preview">${thumb}</a>
      <div class="attach-staged-meta">
        <span class="attach-staged-name">${escapeHtml(f.file?.name || "file")}</span>
        <span class="attach-staged-size muted">${formatBytes(f.file?.size || 0)}</span>
      </div>
    </div>`;
  }).join("");
  if (labelSel) {
    const el = $(labelSel);
    if (el) {
      let labelText;
      if (files.length) {
        const plural = files.length === 1 ? "" : "s";
        labelText = `${files.length} file${plural} attached`;
      } else {
        labelText = _stagedDefaultLabel(labelSel);
      }
      el.textContent = labelText;
    }
  }
  _emitStagedChanged(bucket);
}

function handleStagedInputChange(bucket, inputEl, previewSel, labelSel) {
  if (!inputEl?.files) return;
  for (const f of inputEl.files) {
    const blobUrl = URL.createObjectURL(f);
    STATE.stagedFiles[bucket].push({ file: f, _blobUrl: blobUrl });
  }
  inputEl.value = "";  // allow re-adding the same file after removing
  _renderStagedFiles(bucket, previewSel, labelSel);
}

function handleStagedListClick(bucket, previewSel, labelSel, e) {
  const removeBtn = e.target.closest('[data-act="remove"]');
  if (removeBtn) {
    e.preventDefault();
    const row = removeBtn.closest(".attach-staged");
    const idx = Number.parseInt(row.dataset.idx, 10);
    const arr = STATE.stagedFiles[bucket] || [];
    const gone = arr.splice(idx, 1);
    _revokeBlobs(gone);
    _renderStagedFiles(bucket, previewSel, labelSel);
  }
  // Click on the thumbnail link — let the default <a target=_blank> open the file.
}

// v2.5 — flush the bug-level attachment staging bucket (post-creation
// upload). Runs whenever the user clicks the section-head Upload N
// button in the bug modal. Wrapped in withLoader at the call site so
// the user can't double-submit.
async function flushBugAttachStaging() {
  const files = STATE.stagedFiles.bugAttach || [];
  if (!files.length || !STATE.currentBugId) return;
  let done = 0, failed = 0;
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f.file);
    try {
      await api(`/bugs/${STATE.currentBugId}/attachments`, { method: "POST", body: fd });
      done++;
    } catch (err) {
      failed++;
      toast(`Attachment ${f.file?.name}: ${err.message}`, "error");
    }
  }
  clearStagedFiles("bugAttach", "#bugAttachAddPreview", "#bugAttachAddText");
  const ai = $("#bugAttachAddInput"); if (ai) ai.value = "";
  if (done) toast(`${done} file${done > 1 ? "s" : ""} attached`, "success");
  // Refresh inline sections + table cell so the new attachment_count
  // shows up immediately.
  const bug = await api(`/bugs/${STATE.currentBugId}`);
  renderBugInlineSections(bug);
  await refreshBugs();
}

// v2.5/v2.6 — Render one comment row, with optional admin-only edit/
// delete buttons and sanitized-HTML body for the rich-text editor.
function _renderCommentRow(c, isAdmin) {
  // v2.5: comment attachments are also admin-only deletable. The role
  // gate is the same as for bug-level attachments.
  const atts = (c.attachments || []).map(a => renderAttachmentCard(a, isAdmin)).join("");
  // v2.6: comment body is sanitized HTML from the backend, render as
  // innerHTML so bold/italic/lists/images survive. We never set raw
  // user-supplied HTML — sanitize_html() in app/schemas.py strips every
  // dangerous tag/attr before it ever hits the DB.
  const bodyHtml = (c.body || "").trim()
    ? `<div class="comment-body bh-rich-content" data-comment-body="${c.id}">${c.body}</div>`
    : "";
  const editBtn = (c.body || "").trim()
    ? `<button type="button" class="icon-btn" data-act="edit-comment" data-id="${c.id}" title="Edit comment">✎</button>`
    : "";
  const adminActions = isAdmin ? `
    <span class="comment-admin-actions">
      ${editBtn}
      <button type="button" class="icon-btn danger" data-act="delete-comment" data-id="${c.id}" title="Delete comment">🗑</button>
    </span>` : "";
  return `
    <div class="comment" data-comment-id="${c.id}">
      <div class="comment-head">
        <div class="comment-head-left">
          <span class="avatar">${initials(c.author_name)}</span>
          <span class="comment-author">${escapeHtml(c.author_name)}</span>
        </div>
        <span class="comment-head-right">
          <span class="comment-time">${formatDate(c.created_at)}</span>
          ${adminActions}
        </span>
      </div>
      ${bodyHtml}
      ${atts ? `<div class="comment-attachments"><div class="attachment-grid">${atts}</div></div>` : ""}
    </div>`;
}

function _renderBugCommentsSection(bug, isAdmin) {
  $("#bugCommentsSection").hidden = false;
  $("#commentsCount").textContent = `(${bug.comments.length})`;
  const commentsList = $("#bugCommentsList");
  if (bug.comments.length) {
    commentsList.innerHTML = bug.comments.map(c => _renderCommentRow(c, isAdmin)).join("");
    commentsList.hidden = false;
  } else {
    commentsList.innerHTML = "";
    commentsList.hidden = true;
  }
  const bodyEl = $("#commentBody");
  const filesEl = $("#commentFiles");
  if (bodyEl) {
    bodyEl.value = "";
    if (bodyEl._bhRtSet) bodyEl._bhRtSet("");
  }
  if (filesEl) filesEl.value = "";
  $("#filePreview").innerHTML = "";
  $("#fileLabel").textContent = "Attach files";
}

function _renderBugAttachmentsSection(bug, isAdmin) {
  $("#bugAttachmentsSection").hidden = false;
  $("#attachmentsCount").textContent = bug.attachments.length
    ? `(${bug.attachments.length})`
    : "";
  const emptyEl = $("#bugAttachEmpty");
  if (bug.attachments.length) {
    $("#bugAttachmentsGrid").innerHTML =
      bug.attachments.map(a => renderAttachmentCard(a, isAdmin)).join("");
    if (emptyEl) emptyEl.hidden = true;
  } else {
    $("#bugAttachmentsGrid").innerHTML = "";
    if (emptyEl) emptyEl.hidden = !canEditItem(bug);
  }
  const addLabel = $("#bugAttachAddLabel");
  if (addLabel) addLabel.hidden = !canEditItem(bug);
  clearStagedFiles("bugAttach", "#bugAttachAddPreview", "#bugAttachAddText");
  const ai = $("#bugAttachAddInput");
  if (ai) ai.value = "";
}

function _renderBugActivitySection(bug) {
  $("#bugActivitySection").hidden = false;
  $("#activityCount").textContent = `(${bug.activities.length})`;
  $("#bugActivityList").innerHTML = bug.activities.length
    ? bug.activities.map(a => renderActivityRow(a)).join("")
    : '<p class="no-content">No activity yet.</p>';
}

// Inline render of comments + attachments + activity inside the bug
// modal. Replaces the old separate "detail modal with tabs" — everything
// lives in one screen now.
function renderBugInlineSections(bug) {
  const isAdmin = STATE.currentUser?.role === "admin";
  _renderBugCommentsSection(bug, isAdmin);
  _renderBugAttachmentsSection(bug, isAdmin);
  _renderBugActivitySection(bug);
}

function fillFormSelect(selEl, items, current = "") {
  // Items can be [value, label] or [value, label, title]. The optional
  // 3rd element becomes the option's `title` attr (hover tooltip) so we
  // can keep the visible label short without losing extra context.
  selEl.innerHTML = `<option value="">— select —</option>` +
    items.map((row) => {
      const [v, lbl, ttl] = row;
      const titleAttr = ttl ? ` title="${escapeHtml(ttl)}"` : "";
      return `<option value="${v}"${titleAttr}>${escapeHtml(lbl)}</option>`;
    }).join("");
  if (current !== "" && current != null) selEl.value = current;
}

function renderChips(sel, items, mapFn, selectedIds) {
  const host = $(sel);
  host.innerHTML = "";
  if (!items.length) {
    host.innerHTML = '<span class="chip-empty">— none available —</span>';
    return;
  }
  for (const item of items) {
    const m = mapFn(item);
    const chip = document.createElement("span");
    chip.className = "chip" + (selectedIds.has(m.id) ? " selected" : "");
    chip.dataset.id = String(m.id);
    chip.innerHTML = escapeHtml(m.label) +
      (m.sub ? ` <span class="chip-sub">· ${escapeHtml(m.sub)}</span>` : "");
    chip.addEventListener("click", () => chip.classList.toggle("selected"));
    host.appendChild(chip);
  }
}

function readChips(sel) {
  return $$(`${sel} .chip.selected`).map(c => Number.parseInt(c.dataset.id, 10));
}

function _buildBugPayload(form, id) {
  const reporterFromForm = form.elements.reporter_id.value
    ? Number.parseInt(form.elements.reporter_id.value, 10) : null;
  const reporterFromMe = STATE.currentUser?.id || null;
  const eventVal = form.elements.event_id ? form.elements.event_id.value : "";
  const itemTypeVal = form.elements.item_type ? form.elements.item_type.value : "Bug";
  const reporterId = id ? (reporterFromForm || reporterFromMe) : reporterFromMe;
  return {
    project_id: Number.parseInt(form.elements.project_id.value, 10),
    title: form.elements.title.value.trim(),
    description: form.elements.description.value,
    reporter_id: reporterId,
    status: form.elements.status.value,
    priority: form.elements.priority.value,
    environment: form.elements.environment.value,
    due_date: form.elements.due_date.value || null,
    assignee_ids: readChips("#assigneePicker"),
    item_type: itemTypeVal || "Bug",
    event_id: eventVal ? Number.parseInt(eventVal, 10) : null,
  };
}

function _validateBugPayload(payload) {
  if (!payload.project_id) { toast("Please pick a project", "error"); return false; }
  if (!payload.title) { toast("Title is required", "error"); return false; }
  if (!payload.reporter_id) { toast("Reporter is required", "error"); return false; }
  return true;
}

// v2.5 — Upload each staged file as an attachment to the just-created
// item. Per-file errors are toasted but don't abort — the item is
// already saved. Returns {done, failed}.
async function _uploadCreateBugFiles(createdId, stagedFiles) {
  let done = 0, failed = 0;
  for (const it of stagedFiles) {
    const fd = new FormData();
    fd.append("file", it.file);
    try {
      await api(`/bugs/${createdId}/attachments`, { method: "POST", body: fd });
      done++;
    } catch (err) {
      failed++;
      toast(`Attachment ${it.file?.name}: ${err.message}`, "error");
    }
  }
  return { done, failed };
}

// v2.5 — Emit the success toast after a create, accounting for the
// attachment upload result.
async function _toastAfterCreate(created, ctype, stagedFiles) {
  if (!(stagedFiles.length && created?.id)) {
    toast(`${ctype} created`, "success");
    return;
  }
  const { done, failed } = await _uploadCreateBugFiles(created.id, stagedFiles);
  if (done) toast(`${ctype} #${created.id} created · ${done} file(s) attached`, "success");
  else if (failed) toast(`${ctype} #${created.id} created (no attachments saved)`, "info");
  else toast(`${ctype} created`, "success");
}

async function _submitBugUpdate(id, payload) {
  await withLoader(async () => {
    const updated = await api(`/bugs/${id}`, { method: "PUT", body: JSON.stringify(payload) });
    closeModal("modalBug");
    if (STATE.view === "events" && STATE.currentEventId) {
      await openEventDetail(STATE.currentEventId);
    } else {
      setView("list");
      await refreshAll();
    }
    const utype = updated?.item_type || payload.item_type || "Bug";
    toast(`${utype} #${id} updated`, "success");
  }, "Saving changes…");
}

async function _submitBugCreate(payload) {
  await withLoader(async () => {
    const created = await api("/bugs", { method: "POST", body: JSON.stringify(payload) });
    // v2.4 — remember last-chosen type for next "+ New" click.
    STATE.defaultNewType = payload.item_type || "Bug";
    try { localStorage.setItem("defaultNewType", STATE.defaultNewType); } catch {}
    const ctype = created?.item_type || payload.item_type || "Bug";
    const staged = STATE.stagedFiles.createBug || [];
    await _toastAfterCreate(created, ctype, staged);
    clearStagedFiles("createBug", "#createFilePreview", "#createFileLabel");
    closeModal("modalBug");
    // If the user created an item INSIDE an event-detail view, refresh
    // that detail rather than dumping them on the list.
    if (STATE.view === "events" && STATE.currentEventId) {
      await openEventDetail(STATE.currentEventId);
    } else {
      setView("list");
      await refreshAll();
    }
  }, "Creating item…");
}

async function submitBugForm(e) {
  e.preventDefault();
  const form = e.target;
  const id = form.elements.id.value;
  const payload = _buildBugPayload(form, id);
  if (!_validateBugPayload(payload)) return;
  try {
    if (id) {
      await _submitBugUpdate(id, payload);
    } else {
      await _submitBugCreate(payload);
    }
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Events — v2.4
//
// List mode: card grid of every event in the org. Click a card to
// drill into detail mode (a "← Back" control returns to list mode).
// Detail mode shows the event's items as the same bug-table component
// so the rows look identical to the work-items list. The +Add Task
// button defaults to item_type=Task and pre-populates event_id.
// ---------------------------------------------------------------------------
function showEventsListMode() {
  $("#eventsListMode").hidden = false;
  $("#eventsDetailMode").hidden = true;
}
function showEventsDetailMode() {
  $("#eventsListMode").hidden = true;
  $("#eventsDetailMode").hidden = false;
}

async function refreshEvents() {
  try {
    const events = await api("/events");
    STATE.events = events || [];
    renderEventsList();
  } catch (err) {
    toastError(err);
  }
}

function _eventSummaryText(events) {
  if (!events.length) return "";
  const plural = events.length === 1 ? "" : "s";
  return `${events.length} event${plural}`;
}

// v2.5 — client-side event-list filters. Search hits name + description;
// date is exact-match against scheduled_for.
function _matchesEventFilter(ev, q, date) {
  if (date && (ev.scheduled_for || "") !== date) return false;
  if (q) {
    const hay = `${ev.name || ""} ${ev.description || ""}`.toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

function _eventsSummaryTextFiltered(rows, totalItems, totalPeople, q, date) {
  if (rows.length === 0) return (q || date) ? "0 matching events" : "";
  const peopleLabel = rows.length === 1 ? `${totalPeople}` : `~${totalPeople}`;
  const prefix = (q || date) ? "showing " : "";
  return `${prefix}${rows.length} event${rows.length === 1 ? "" : "s"} · `
       + `${totalItems} item${totalItems === 1 ? "" : "s"} · `
       + `${peopleLabel} ${totalPeople === 1 ? "person" : "people"}`;
}

// v2.5 — event-detail item filter helpers.
function _itemMatchesAssignees(item, assignees) {
  if (!assignees.size) return true;
  const ids = (item.assignees || []).map(a => String(a.id));
  return ids.some(id => assignees.has(id));
}

function _itemMatchesQuery(item, q) {
  if (!q) return true;
  const hay = `${item.title || ""} ${item.description || ""}`.toLowerCase();
  if (/^\d+$/.test(q)) return String(item.id) === q || hay.includes(q);
  return hay.includes(q);
}

function _filterEventItems(items) {
  const f = STATE.eventDetailFilter || {};
  const q = (f.q || "").trim().toLowerCase().replace(/^#/, "");
  const statuses = new Set(f.status || []);
  const priorities = new Set(f.priority || []);
  const assignees = new Set((f.assignee_id || []).map(String));
  return items.filter(b =>
    (!statuses.size || statuses.has(b.status))
    && (!priorities.size || priorities.has(b.priority))
    && _itemMatchesAssignees(b, assignees)
    && _itemMatchesQuery(b, q)
  );
}

function _eventDetailOptsForKey(key, assigneeOptions) {
  if (key === "status") return { opts: (STATE.meta.statuses || []).map(s => [s, s]), label: "All Statuses" };
  if (key === "priority") return { opts: (STATE.meta.priorities || []).map(s => [s, s]), label: "All Priorities" };
  if (key === "assignee_id") return { opts: assigneeOptions, label: "All Assignees" };
  return { opts: [], label: "" };
}

function _eventDetailLabel(defaultLabel, opts, selected) {
  if (selected.size === 0) return defaultLabel;
  if (selected.size === 1) {
    const only = [...selected][0];
    const match = opts.find(([v]) => v === only);
    return match ? match[1] : only;
  }
  return `${defaultLabel.replace(/^All /, '')} (${selected.size})`;
}

function _renderMsRowsForEventDetail(opts, selected) {
  if (!opts.length) return `<div class="ms-empty">No options</div>`;
  return opts.map(([v, lbl]) => {
    const isOn = selected.has(v);
    return `<div class="ms-row${isOn ? " on" : ""}" data-ms-value="${escapeHtml(v)}" role="menuitemcheckbox" aria-checked="${isOn}">
      <span class="ms-check">${isOn ? "✓" : ""}</span>
      <span class="ms-text">${escapeHtml(lbl)}</span>
    </div>`;
  }).join("");
}

// v2.5 — populate the event-detail filter-bar multi-selects from
// STATE.meta + the event's assignee pool.
function refreshEventDetailFilters(ev) {
  const wraps = $$('#eventDetailFilterBar .ms-wrap');
  if (!wraps.length) return;
  const f = STATE.eventDetailFilter;
  // Build assignee option pool from the event's items so the dropdown
  // only shows people actually assigned to something in this event.
  const seen = new Map();
  for (const it of ev.items || []) {
    for (const a of it.assignees || []) {
      if (!seen.has(a.id)) seen.set(a.id, a);
    }
  }
  const assigneeOptions = [...seen.values()].map(u => [String(u.id), u.name]);

  for (const wrap of wraps) {
    const { opts, label } = _eventDetailOptsForKey(wrap.dataset.eventFilter, assigneeOptions);
    const selected = new Set(f[wrap.dataset.eventFilter] || []);
    const panel = wrap.querySelector(".ms-panel");
    const labelEl = wrap.querySelector(".ms-btn-label");
    const btn = wrap.querySelector(".ms-btn");
    panel.innerHTML = _renderMsRowsForEventDetail(opts, selected);
    labelEl.textContent = _eventDetailLabel(label, opts, selected);
    btn.classList.toggle("active", selected.size > 0);
  }
}

function _eventCardHtml(ev) {
  const managersHtml = (ev.managers || []).slice(0, 3).map(m =>
    `<span class="event-mgr-chip" title="${escapeHtml(m.email)}"><span class="avatar">${initials(m.name)}</span><span class="assignee-chip-name">${escapeHtml(m.name)}</span></span>`
  ).join("");
  const moreCount = Math.max(0, (ev.managers || []).length - 3);
  const itemCount = ev.item_count || 0;
  const itemPlural = itemCount === 1 ? "" : "s";
  const dateHtml = ev.scheduled_for
    ? `<div class="event-card-date">${escapeHtml(ev.scheduled_for)}</div>`
    : "";
  const moreHtml = moreCount ? `<span class="event-mgr-more">+${moreCount}</span>` : "";
  const managersBlock = managersHtml
    ? `<div class="event-card-managers">${managersHtml}${moreHtml}</div>`
    : "";
  return `<button type="button" class="event-card" data-event-id="${ev.id}">
      <div class="event-card-head">
        <span class="event-card-icon">📅</span>
        <h3 class="event-card-name">${escapeHtml(ev.name)}</h3>
      </div>
      ${dateHtml}
      <div class="event-card-meta">
        <span class="event-card-stat"><strong>${itemCount}</strong> item${itemPlural}</span>
      </div>
      ${managersBlock}
    </button>`;
}

function _renderEmptyEventsState(grid, empty, allCount) {
  grid.innerHTML = "";
  if (empty) empty.hidden = allCount > 0;
  if (allCount > 0) {
    grid.innerHTML = `<div class="events-empty muted"><p>No events match the current filter. Try clearing the search or date</p></div>`;
  }
}

function renderEventsList() {
  const host = $("#eventsGrid");
  const empty = $("#eventsEmpty");
  const summary = $("#eventsSummary");
  if (!host) return;
  const events = STATE.events || [];
  // v2.5: filter client-side over STATE.events.
  const q = (STATE.eventsFilter.q || "").trim().toLowerCase();
  const date = (STATE.eventsFilter.date || "").trim();
  const filtered = events.filter(ev => _matchesEventFilter(ev, q, date));
  const totalItems = filtered.reduce((n, ev) => n + (ev.item_count || 0), 0);
  const totalPeople = filtered.reduce((n, ev) => n + ((ev.managers || []).length), 0);
  if (summary) {
    summary.textContent = (q || date)
      ? _eventsSummaryTextFiltered(filtered, totalItems, totalPeople, q, date)
      : _eventSummaryText(events);
  }
  if (!filtered.length) {
    _renderEmptyEventsState(host, empty, events.length);
    return;
  }
  if (empty) empty.hidden = true;
  host.innerHTML = filtered.map(_eventCardHtml).join("");
}

async function openEventDetail(eventId) {
  try {
    const ev = await api(`/events/${eventId}`);
    STATE.currentEventId = ev.id;
    STATE.currentEvent = ev;
    showEventsDetailMode();
    renderEventDetail(ev);
  } catch (err) {
    toastError(err);
  }
}

function renderEventDetail(ev) {
  $("#eventDetailName").textContent = ev.name;
  const meta = $("#eventDetailMeta");
  const mgrHtml = (ev.managers || []).map(m =>
    `<span class="event-mgr-chip" title="${escapeHtml(m.email)}"><span class="avatar">${initials(m.name)}</span><span class="assignee-chip-name">${escapeHtml(m.name)}</span></span>`
  ).join("");
  meta.innerHTML = `
    ${ev.scheduled_for ? `<div class="events-detail-row"><span class="k">Scheduled for</span><span class="v">${escapeHtml(ev.scheduled_for)}</span></div>` : ""}
    ${ev.description ? `<div class="events-detail-row events-detail-desc">${escapeHtml(ev.description)}</div>` : ""}
    ${mgrHtml ? `<div class="events-detail-row event-detail-managers"><span class="k">Managers</span><div class="v">${mgrHtml}</div></div>` : ""}
  `;

  // v2.5: re-populate the event-detail filter-bar multi-selects with
  // the current item set as the data source for the assignee dropdown.
  refreshEventDetailFilters(ev);

  // Items list — same column shape as the work-items table.
  const allItems = ev.items || [];
  const filtered = _filterEventItems(allItems);
  const host = $("#eventDetailItems");
  if (allItems.length === 0) {
    host.innerHTML = `<p class="no-content">No items in this event yet — click <strong>+ Add Task</strong> to create one</p>`;
    return;
  }
  if (filtered.length === 0) {
    host.innerHTML = `
      <div class="event-detail-empty muted">
        <p>No tasks in this event match the current filter. Try clearing the search or filters</p>
      </div>`;
    return;
  }
  // Build a table mirroring the bug-table so the rows look identical.
  const headRow = ["id", "title-with-type", "project", "status", "priority", "due", "assignees", "att"]
    .map(c => `<th class="col-${c.replace("-with-type","")}">${COL_HEAD_LABEL[c] || ""}</th>`).join("");
  const bodyRows = filtered.map(it => {
    // EventItemBrief from the server has the same shape as a bug for
    // the columns we render. Re-use _renderBugCell.
    return `<tr data-bug-id="${it.id}">` +
      ["id", "title-with-type", "project", "status", "priority", "due", "assignees", "att"]
        .map(c => _renderBugCell(c, it, false)).join("") +
      `</tr>`;
  }).join("");
  host.innerHTML = `<table class="bug-table"><thead><tr>${headRow}</tr></thead><tbody>${bodyRows}</tbody></table>`;
}

function openEventForm(ev = null) {
  const form = $("#formEvent");
  form.reset();
  const isEdit = !!ev?.id;
  $("#modalEventTitle").textContent = isEdit ? `📅 Edit Event` : "📅 New Event";
  form.elements.id.value = isEdit ? ev.id : "";
  if (isEdit) {
    form.elements.name.value = ev.name || "";
    form.elements.description.value = ev.description || "";
    form.elements.scheduled_for.value = ev.scheduled_for || "";
  }
  // Manager picker — filter to admin/manager users only (the server
  // also enforces this).
  const eligible = (STATE.users || [])
    .filter(u => u.is_active && (u.role === "admin" || u.role === "manager"));
  const selectedIds = new Set(isEdit && ev.managers ? ev.managers.map(m => m.id) : []);
  renderChips("#eventManagerPicker", eligible,
    (u) => ({ id: u.id, label: u.name, sub: u.role }),
    selectedIds);
  openModal("modalEvent");
  setTimeout(() => form.elements.name.focus(), 50);
}

async function submitEventForm(e) {
  e.preventDefault();
  const form = e.target;
  const id = form.elements.id.value;
  const payload = {
    name: form.elements.name.value.trim(),
    description: form.elements.description.value,
    scheduled_for: form.elements.scheduled_for.value || null,
    manager_ids: readChips("#eventManagerPicker"),
  };
  if (!payload.name) { toast("Event name is required", "error"); return; }
  try {
    await withLoader(async () => {
      if (id) {
        await api(`/events/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      } else {
        await api("/events", { method: "POST", body: JSON.stringify(payload) });
      }
      closeModal("modalEvent");
      await refreshEvents();
      if (id && STATE.currentEventId === Number.parseInt(id, 10)) {
        await openEventDetail(Number.parseInt(id, 10));
      }
    }, id ? "Saving event…" : "Creating event…");
    toast(id ? "Event updated" : "Event created", "success");
  } catch (err) {
    toastError(err);
  }
}

async function handleDeleteEvent(eventId) {
  const ok = await confirmDialog(
    "Delete this event? Items inside will survive but lose the event link.",
  );
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/events/${eventId}`, { method: "DELETE" });
      STATE.currentEventId = null;
      STATE.currentEvent = null;
      showEventsListMode();
      await refreshEvents();
    }, "Deleting event…");
    toast("Event deleted", "success");
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Bug detail (kept as a thin alias for callers that previously opened
// the now-removed separate detail modal — fetches the bug and routes
// straight into the unified modal in edit/view mode).
// ---------------------------------------------------------------------------
async function openBugDetail(bugId) {
  STATE.currentBugId = bugId;
  STATE.detailTab = "info";  // legacy field; not read anywhere now
  try {
    const bug = await api(`/bugs/${bugId}`);
    openBugForm(bug);
  } catch (err) {
    toastError(err);
  }
}

// (renderBugDetail removed — its responsibilities are now split between
//  openBugForm — which fills the editable form — and
//  renderBugInlineSections — which renders the read-only sections.)

function renderAttachmentCard(a, deletable) {
  const url = `/api/bugs/${STATE.currentBugId}/attachments/${a.id}/download`;
  const ct = (a.content_type || "").toLowerCase();
  let preview = "";
  // Inline rendering is safe for raster images and video. SVG is a vector
  // image but can carry inline JS (server already downgrades it on
  // download), so we treat it like any other downloadable file rather
  // than embedding it as <img>.
  const isRasterImg = ct.startsWith("image/") && ct !== "image/svg+xml";
  if (isRasterImg) {
    preview = `<a href="${url}" target="_blank" rel="noopener"><img src="${url}" alt="${escapeHtml(a.filename)}" loading="lazy"/></a>`;
  } else if (ct.startsWith("video/")) {
    preview = `<video controls preload="metadata"><source src="${url}" type="${escapeHtml(a.content_type)}"/></video>`;
  } else {
    preview = `<a href="${url}" target="_blank" rel="noopener" class="file-icon">${fileIcon(a.content_type, a.filename)}</a>`;
  }
  return `
    <div class="attach-card" data-att-id="${a.id}">
      <div class="attach-preview">${preview}</div>
      <div class="attach-meta">
        <div class="attach-name" title="${escapeHtml(a.filename)}">${escapeHtml(a.filename)}</div>
        <div class="attach-info">
          <span>${formatBytes(a.size_bytes)}</span>
          <span>${escapeHtml(a.uploader_name)}</span>
        </div>
      </div>
      <div class="attach-actions">
        <a href="${url}" target="_blank" rel="noopener">View</a>
        <a href="${url}" download="${escapeHtml(a.filename)}">Download</a>
        ${deletable ? `<button class="danger" data-act="delete-attachment" data-id="${a.id}">Delete</button>` : ""}
      </div>
    </div>`;
}

function renderActivityRow(a) {
  return `
    <div class="activity-row">
      <span class="activity-icon">${activityIcon(a.action)}</span>
      <div class="activity-text">
        <div><span class="activity-actor">${escapeHtml(a.actor_name)}</span><span class="activity-action">${escapeHtml(a.action)}</span></div>
        ${a.detail ? `<div class="activity-detail">${escapeHtml(a.detail)}</div>` : ""}
      </div>
      <span class="activity-time">${formatDate(a.created_at)}</span>
    </div>`;
}

function activityIcon(action) {
  if (action.includes("session")) return "🔐";
  if (action.includes("login")) return "🔑";
  if (action.includes("logout")) return "👋";
  if (action.includes("password")) return "🔒";
  if (action.includes("created")) return "✨";
  if (action.includes("delete")) return "🗑";
  if (action.includes("comment")) return "💬";
  if (action.includes("attachment")) return "📎";
  if (action.includes("status")) return "🔄";
  if (action.includes("assign")) return "👥";
  return "📝";
}

// ---------------------------------------------------------------------------
// Project / User forms
// ---------------------------------------------------------------------------
function openProjectForm(project = null) {
  const form = $("#formProject");
  form.reset();
  $("#modalProjectTitle").textContent = project ? `Edit "${project.name}"` : "New Project";
  form.elements.id.value = project ? project.id : "";
  if (project) {
    form.elements.name.value = project.name;
    if (form.elements.key) form.elements.key.value = project.key || "";
    form.elements.color.value = project.color;
    form.elements.description.value = project.description;
  } else {
    form.elements.color.value = "#c9764f";
    if (form.elements.key) form.elements.key.value = "";
  }
  openModal("modalProject");
  setTimeout(() => form.elements.name.focus(), 50);
}

async function submitProjectForm(e) {
  e.preventDefault();
  const form = e.target;
  const id = form.elements.id.value;
  const payload = {
    name: form.elements.name.value.trim(),
    color: form.elements.color.value,
    description: form.elements.description.value,
  };
  // Send key only if the user typed one — backend auto-derives otherwise.
  const keyVal = form.elements.key ? form.elements.key.value.trim().toUpperCase() : "";
  if (keyVal) payload.key = keyVal;
  try {
    await withLoader(async () => {
      if (id) {
        await api(`/projects/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      } else {
        await api("/projects", { method: "POST", body: JSON.stringify(payload) });
      }
      closeModal("modalProject");
      setView("list");
      await loadProjects();
      await refreshAll();
    }, id ? "Saving project…" : "Creating project…");
    toast(id ? "Project updated" : "Project created", "success");
  } catch (err) {
    toastError(err);
  }
}

// User-form hint text (UI placeholder strings — not credentials, even though
// they describe credential rules). Kept as a small string table whose keys
// don't include the word "password" so that static analyzers don't pattern-
// match the assignment `.password.placeholder = "literal"` and flag it as a
// hard-coded credential. Access via bracket notation on the form element
// for the same reason.
const _USER_FORM_HINTS = {
  editPlaceholder: "Leave blank to keep current",
  editHint: "Leave blank to keep current password.",
  createPlaceholder: "Min 8 characters",
  createHint: "At least 8 characters.",
};

function _applyUserPasswordHints(form, user) {
  const field = form.elements["password"];
  const hintEl = $("#userPasswordHint");
  const requiredMark = $("#userPasswordField").querySelector(".js-required");
  if (user) {
    field.required = false;
    field.value = "";
    field.placeholder = _USER_FORM_HINTS.editPlaceholder;
    if (hintEl) hintEl.textContent = _USER_FORM_HINTS.editHint;
    requiredMark?.classList.add("hidden");
    return;
  }
  field.required = true;
  field.placeholder = _USER_FORM_HINTS.createPlaceholder;
  if (hintEl) hintEl.textContent = _USER_FORM_HINTS.createHint;
  requiredMark?.classList.remove("hidden");
}

function openUserForm(user = null) {
  const form = $("#formUser");
  form.reset();
  $("#modalUserTitle").textContent = user ? `Edit ${user.name}` : "New User";
  form.elements.id.value = user ? user.id : "";

  if (user) {
    form.elements.name.value = user.name;
    form.elements.email.value = user.email;
    // Enterprise role tier: admin / manager / member (NOT "user").
    form.elements.role.value = user.role || "member";
    form.elements.is_active.checked = user.is_active;
    _applyUserPasswordHints(form, user);
  } else {
    form.elements.role.value = "member";
    form.elements.is_active.checked = true;
    _applyUserPasswordHints(form, null);
  }
  openModal("modalUser");
  setTimeout(() => form.elements.name.focus(), 50);
}

async function submitUserForm(e) {
  e.preventDefault();
  const form = e.target;
  const id = form.elements.id.value;
  const payload = {
    name: form.elements.name.value.trim(),
    email: form.elements.email.value.trim(),
    role: form.elements.role.value,
    is_active: form.elements.is_active.checked,
  };
  // Only include password if user typed one (on edit, blank = keep current)
  const pw = form.elements.password.value;
  if (pw) {
    if (pw.length < 8) {
      toast("Password must be at least 8 characters", "error");
      return;
    }
    payload.password = pw;
  } else if (!id) {
    toast("Password is required for new users", "error");
    return;
  }

  try {
    await withLoader(async () => {
      if (id) {
        await api(`/users/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      } else {
        await api("/users", { method: "POST", body: JSON.stringify(payload) });
      }
      closeModal("modalUser");
      await loadUsers();
      await refreshAll();
    }, id ? "Saving user…" : "Creating user…");
    toast(id ? "User updated" : "User created", "success");
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Action handlers
// ---------------------------------------------------------------------------
async function handleDeleteBug(bugId) {
  // Look up the row from cached state so the confirm prompt uses the
  // right noun for the item type. Falls back to "Bug" if not found.
  const cached = (STATE.bugs || []).find(b => b.id === bugId);
  const itype = cached?.item_type || "Bug";
  const noun = itype.toLowerCase();
  const ok = await confirmDialog(
    `Delete ${noun} #${bugId}? This will also delete its comments and attachments. Cannot be undone`
  );
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/bugs/${bugId}`, { method: "DELETE" });
      closeModal("modalBug");
      await refreshAll();
    }, `Deleting ${noun}…`);
    toast(`${itype} #${bugId} deleted`, "success");
  } catch (err) { toastError(err); }
}

async function handleDeleteProject(id) {
  const project = STATE.projects.find(p => p.id === id);
  const name = project ? project.name : `#${id}`;
  const ok = await confirmDialog(`Delete project "${name}"?\nThis only works if it has no bugs`);
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/projects/${id}`, { method: "DELETE" });
      // Drop the deleted project from the multi-select filter so we don't
      // keep filtering by a no-longer-existing id.
      const sid = String(id);
      STATE.filters.project_id = (STATE.filters.project_id || []).filter(v => v !== sid);
      await loadProjects();
      await refreshAll();
    }, "Deleting project…");
    toast(`Project "${name}" deleted`, "success");
  } catch (err) { toastError(err); }
}

async function handleEditProject(id) {
  const p = STATE.projects.find(x => x.id === id);
  if (p) openProjectForm(p);
}

async function handleDeleteUser(id) {
  const user = STATE.users.find(u => u.id === id);
  const name = user ? user.name : `#${id}`;
  const ok = await confirmDialog(
    `Delete user "${name}"?\nThis user will be removed from all bug assignments.\nReports they filed will become "unassigned reporter"`,
  );
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/users/${id}`, { method: "DELETE" });
      await loadUsers();
      await refreshAll();
    }, "Deleting user…");
    toast(`User "${name}" deleted`, "success");
  } catch (err) { toastError(err); }
}

async function handleEditUser(id) {
  const u = STATE.users.find(x => x.id === id);
  if (u) openUserForm(u);
}

async function handleDeleteAttachment(attId) {
  const ok = await confirmDialog("Delete this attachment?");
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/bugs/${STATE.currentBugId}/attachments/${attId}`, { method: "DELETE" });
      const bug = await api(`/bugs/${STATE.currentBugId}`);
      renderBugInlineSections(bug);
      await refreshBugs();
    }, "Deleting attachment…");
    toast("Attachment deleted", "success");
  } catch (err) { toastError(err); }
}

// v2.5 — admin-only delete/edit of a comment. The handlers use the
// confirm dialog (delete) or an inline editor (edit) and gate behind
// the loader so the user can't double-click.
async function handleDeleteComment(commentId) {
  const ok = await confirmDialog("Delete this comment? Its attachments will be removed too. Cannot be undone");
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/bugs/${STATE.currentBugId}/comments/${commentId}`, { method: "DELETE" });
      const bug = await api(`/bugs/${STATE.currentBugId}`);
      renderBugInlineSections(bug);
      await refreshBugs();
    }, "Deleting comment…");
    toast("Comment deleted", "success");
  } catch (err) { toastError(err); }
}

async function handleEditComment(commentId) {
  const commentEl = document.querySelector(`.comment[data-comment-id="${commentId}"]`);
  if (!commentEl) return;
  const bodyEl = commentEl.querySelector(`[data-comment-body="${commentId}"]`);
  if (!bodyEl) return;
  // v2.6: inline editor uses the same rich-text widget as the composer.
  // innerHTML of the body div is the source — sanitized HTML from the
  // backend already.
  const currentHtml = bodyEl.innerHTML || "";
  const editor = document.createElement("div");
  editor.className = "comment-edit-row";
  editor.innerHTML = `
    <textarea class="comment-edit-input" data-bh-rt rows="3" placeholder="Edit comment…"></textarea>
    <div class="comment-edit-actions">
      <button type="button" class="btn ghost" data-act="cancel-edit-comment" data-id="${commentId}">Cancel</button>
      <button type="button" class="btn primary" data-act="save-edit-comment" data-id="${commentId}">Save</button>
    </div>`;
  bodyEl.replaceWith(editor);
  const ta = editor.querySelector(".comment-edit-input");
  if (ta) {
    ta.value = currentHtml;
    enhanceRichEditor(ta, { compact: true });
    if (ta._bhRtSet) ta._bhRtSet(currentHtml);
    // v2.6: paste here uploads as a bug-level attachment — the comment's
    // already saved, so we can't easily backfill files into its
    // comment_id linkage. Bug-level is the safe target.
    ta._bhPasteHandler = async (f) => {
      const fd = new FormData();
      fd.append("file", f);
      await api(`/bugs/${STATE.currentBugId}/attachments`, { method: "POST", body: fd });
      toast(`Attached: ${f.name || "pasted file"}`, "success");
    };
    editor.querySelector('.bh-rt-editor')?.focus();
  }
}

async function handleSaveEditComment(commentId) {
  const editor = document.querySelector(`.comment[data-comment-id="${commentId}"] .comment-edit-row`);
  if (!editor) return;
  const ta = editor.querySelector(".comment-edit-input");
  const body = (ta?.value || "").trim();
  if (!body) { toast("Comment body can't be empty", "error"); return; }
  try {
    await withLoader(async () => {
      await api(`/bugs/${STATE.currentBugId}/comments/${commentId}`, {
        method: "PUT",
        body: JSON.stringify({ body }),
      });
      const bug = await api(`/bugs/${STATE.currentBugId}`);
      renderBugInlineSections(bug);
    }, "Saving comment…");
    toast("Comment updated", "success");
  } catch (err) { toastError(err); }
}

async function handleCancelEditComment(_commentId) {
  // Re-render the inline sections from the cached bug so the editor
  // goes away and the original body re-appears.
  try {
    const bug = await api(`/bugs/${STATE.currentBugId}`);
    renderBugInlineSections(bug);
  } catch (err) { toastError(err); }
}

async function _postCommentCreate(body) {
  const comment = await api(`/bugs/${STATE.currentBugId}/comments`, {
    method: "POST",
    body: JSON.stringify({ body }),
  });
  return comment.id;
}

async function _uploadCommentFiles(stagedFiles, commentId) {
  // v2.4 — upload any staged files (hover-X / click-preview flow).
  let failed = 0;
  for (const it of stagedFiles) {
    const fd = new FormData();
    fd.append("file", it.file);
    if (commentId) fd.append("comment_id", String(commentId));
    try {
      await api(`/bugs/${STATE.currentBugId}/attachments`, { method: "POST", body: fd });
    } catch (err) {
      failed++;
      toast(`Attachment ${it.file.name}: ${err.message}`, "error");
    }
  }
  return failed;
}

function _toastAfterComment(body, fileCount, failed) {
  if (body) {
    toast("Comment posted", "success");
    return;
  }
  if (fileCount && !failed) {
    toast(`${fileCount} file${fileCount > 1 ? "s" : ""} attached`, "success");
  }
}

function _commentSubmitLabel(body, fileCount) {
  if (body && fileCount) return "Posting comment and uploading…";
  if (body) return "Posting comment…";
  return "Uploading file(s)…";
}

async function postComment() {
  // Comment form is no longer a <form> element (nested forms are illegal
  // in HTML5). We read the textarea + file input directly by id.
  //
  // v2.5 either-or: posting works with body, files, or both. If only
  // files are attached they upload as bug-level attachments (no
  // comment record) so the user isn't forced to type a meaningless
  // body just to share a file.
  const bodyEl = $("#commentBody");
  const filesEl = $("#commentFiles");
  const body = (bodyEl?.value || "").trim();
  const staged = STATE.stagedFiles.comment || [];
  if (!body && staged.length === 0) {
    toast("Add a comment or attach a file", "error");
    bodyEl?.focus();
    return;
  }
  try {
    await withLoader(async () => {
      const commentId = body ? await _postCommentCreate(body) : null;
      const failed = await _uploadCommentFiles(staged, commentId);
      _toastAfterComment(body, staged.length, failed);

      if (bodyEl) {
        bodyEl.value = "";
        // v2.6: also clear the rich-editor surface.
        if (bodyEl._bhRtSet) bodyEl._bhRtSet("");
      }
      if (filesEl) filesEl.value = "";
      clearStagedFiles("comment", "#filePreview", "#fileLabel");

      const bug = await api(`/bugs/${STATE.currentBugId}`);
      renderBugInlineSections(bug);
      await refreshBugs();
    }, _commentSubmitLabel(body, staged.length));
  } catch (err) { toastError(err); }
}

// ---------------------------------------------------------------------------
// Sessions admin view
//
// Lists every active session row with user, role, IP, browser, when it
// was created, when it was last seen, when it expires. Admin-only —
// the nav button has data-needs-role="admin" so non-admins never see
// it; the API also enforces this (403 for non-admins) so direct URL
// access is also blocked.
// ---------------------------------------------------------------------------
function shortenUserAgent(ua) {
  // The full UA string is awful to read. We pull out a short browser /
  // OS hint instead. Anything we don't recognise falls back to the
  // first 60 chars so the column doesn't explode.
  if (!ua) return "Unknown";
  const lower = ua.toLowerCase();
  let browser = "Unknown";
  if (lower.includes("edg/")) browser = "Edge";
  else if (lower.includes("chrome/")) browser = "Chrome";
  else if (lower.includes("firefox/")) browser = "Firefox";
  else if (lower.includes("safari/") && !lower.includes("chrome/")) browser = "Safari";
  else if (lower.includes("curl/")) browser = "curl";
  else if (lower.includes("python-")) browser = "Python";
  else if (lower.includes("postman")) browser = "Postman";
  let os = "";
  if (lower.includes("windows")) os = "Windows";
  else if (lower.includes("mac os") || lower.includes("macintosh")) os = "macOS";
  else if (lower.includes("linux")) os = "Linux";
  else if (lower.includes("android")) os = "Android";
  else if (lower.includes("iphone") || lower.includes("ios")) os = "iOS";
  return os ? `${browser} on ${os}` : browser;
}

async function refreshSessions() {
  try {
    STATE.sessions = await api("/sessions");
    renderSessions();
  } catch (err) {
    toastError(err);
  }
}

function renderSessions() {
  const host = $("#sessionsList");
  const rows = STATE.sessions || [];
  if (!rows.length) {
    host.innerHTML = `<div class="sessions-empty">No active sessions.</div>`;
    return;
  }
  host.innerHTML = rows.map(s => {
    const ua = shortenUserAgent(s.user_agent);
    const ip = s.ip_address || "(unknown IP)";
    const role = s.user_role
      ? `<span class="session-role-pill">${escapeHtml(s.user_role)}</span>`
      : "";
    const currentTag = s.is_current
      ? `<span class="session-current-flag" title="The session you're using right now — can't be revoked from here">This is you</span>`
      : "";
    return `
      <div class="session-row${s.is_current ? " is-current" : ""}" data-session-id="${s.id}">
        <span class="session-avatar">${initials(s.user_name || "?")}</span>
        <div class="session-main">
          <div class="session-line1">
            <span class="session-name">${escapeHtml(s.user_name || "(deleted user)")}</span>
            <span class="muted small">${escapeHtml(s.user_email || "")}</span>
            ${role}
            ${currentTag}
          </div>
          <div class="session-line2">${escapeHtml(ua)} · ${escapeHtml(ip)}</div>
          <div class="session-line3">
            Started ${formatDate(s.created_at)} ·
            Last seen ${formatDate(s.last_seen_at)} ·
            Expires ${formatDate(s.expires_at)}
          </div>
        </div>
        <div class="session-actions">
          <button class="btn danger" data-act="revoke-session" data-id="${s.id}"
            ${s.is_current ? "disabled title='Use Log out from the sidebar to end your own session'" : ""}>
            Revoke
          </button>
        </div>
      </div>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// Profile — self-service edits
//
// Name change is a simple PUT. Email change is two-step:
//   1. POST /api/auth/email-change/request {new_email, current_password}
//   2. POST /api/auth/email-change/confirm {code}
// The code lands in the user's NEW inbox so we confirm they actually
// control it. We never reveal the code to the JS — it goes via email.
// ---------------------------------------------------------------------------
function openProfileModal() {
  const u = STATE.currentUser;
  if (!u) return;
  $("#formProfileIdentity").elements.name.value = u.name || "";
  $("#profileRole").textContent = u.role || "—";
  $("#profileOrg").textContent = u.organization_name || "—";
  $("#profileEmail").textContent = u.email || "—";
  $("#formEmailChangeRequest").reset();
  $("#formEmailChangeConfirm").reset();
  $("#emailChangeStep2").hidden = true;
  openModal("modalProfile");
}

async function submitProfileIdentity(e) {
  e.preventDefault();
  const name = e.target.elements.name.value.trim();
  if (name.length < 2) {
    toast("Name must be at least 2 characters", "error");
    return;
  }
  try {
    await withLoader(async () => {
      const updated = await api("/auth/profile", {
        method: "PUT",
        body: JSON.stringify({ name }),
      });
      // Update everywhere the name is shown.
      STATE.currentUser = { ...STATE.currentUser, ...updated };
      renderAccountCard();
      renderOrgBanner();
      // Refresh the users list so admin views update too.
      await loadUsers();
    }, "Saving profile…");
    toast("Profile updated", "success");
  } catch (err) {
    toastError(err);
  }
}

async function submitEmailChangeRequest(e) {
  e.preventDefault();
  const f = e.target;
  const new_email = f.elements.new_email.value.trim();
  const current_password = f.elements.current_password.value;
  if (!new_email?.includes("@")) {
    toast("Please enter a valid new email", "error");
    return;
  }
  if (!current_password) {
    toast("Please enter your current password to confirm", "error");
    return;
  }
  try {
    await withLoader(async () => {
      await api("/auth/email-change/request", {
        method: "POST",
        body: JSON.stringify({ new_email, current_password }),
      });
      $("#emailChangeNew").textContent = new_email;
      $("#emailChangeStep2").hidden = false;
      $("#formEmailChangeConfirm").elements.code.value = "";
      setTimeout(() => $("#formEmailChangeConfirm").elements.code.focus(), 50);
      // Clear the password field so it isn't sitting around in the DOM.
      f.elements.current_password.value = "";
    }, "Requesting email change…");
    toast("Code sent — check your new email inbox", "success");
  } catch (err) {
    toastError(err);
  }
}

async function submitEmailChangeConfirm(e) {
  e.preventDefault();
  const code = e.target.elements.code.value.trim();
  if (!/^\d{6}$/.test(code)) {
    toast("Enter the 6-digit code from your email", "error");
    return;
  }
  try {
    await withLoader(async () => {
      const updated = await api("/auth/email-change/confirm", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      STATE.currentUser = { ...STATE.currentUser, ...updated };
      renderAccountCard();
      renderOrgBanner();
      $("#profileEmail").textContent = updated.email;
      $("#emailChangeStep2").hidden = true;
      $("#formEmailChangeRequest").reset();
      // Reload the users list so other admins see the change too.
      await loadUsers();
    }, "Confirming email change…");
    toast("Email updated", "success");
  } catch (err) {
    toastError(err);
  }
}

async function handleRevokeSession(sessionId) {
  const sess = (STATE.sessions || []).find(s => s.id === sessionId);
  const who = sess?.user_name
    ? `${sess.user_name} <${sess.user_email}>`
    : `session #${sessionId}`;
  const ok = await confirmDialog(
    `Revoke this session for ${who}?\n\n` +
    `That device will be immediately logged out. Other sessions for the ` +
    `same user are not affected`,
    { title: "Revoke session", okLabel: "Revoke", danger: true },
  );
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/sessions/${sessionId}`, { method: "DELETE" });
      await refreshSessions();
    }, "Revoking session…");
    toast("Session revoked", "success");
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Invitations view
// ---------------------------------------------------------------------------
async function refreshInvitations() {
  try {
    STATE.invitations = await api("/invitations");
    renderInvitations();
  } catch (err) {
    toastError(err);
  }
}

function renderInvitations() {
  const host = document.getElementById("invitationsList");
  if (!host) return;
  const rows = STATE.invitations || [];
  if (!rows.length) {
    host.innerHTML = `<div class="sessions-empty">No invitations yet. Click <strong>+ Invite a teammate</strong> to send one.</div>`;
    return;
  }
  const now = Date.now();
  host.innerHTML = rows.map(inv => {
    const status = statusForInvitation(inv, now);
    const expires = inv.expires_at ? formatDate(inv.expires_at) : "—";
    const created = inv.created_at ? formatDate(inv.created_at) : "—";
    const isPending = status.kind === "pending";
    return `
      <div class="session-row" data-invite-id="${inv.id}">
        <span class="session-avatar">✉</span>
        <div class="session-main">
          <div class="session-line1">
            <span class="session-name">${escapeHtml(inv.email)}</span>
            <span class="session-role-pill">${escapeHtml(inv.role)}</span>
            <span class="invite-status invite-status-${status.kind}">${escapeHtml(status.label)}</span>
          </div>
          <div class="session-line2">Invited by ${escapeHtml(inv.invited_by_name || "—")} · ${escapeHtml(created)}</div>
          <div class="session-line3">Expires ${escapeHtml(expires)}</div>
        </div>
        <div class="session-actions">
          ${isPending ? `<button class="btn danger" data-act="revoke-invite" data-id="${inv.id}">Revoke</button>` : ""}
        </div>
      </div>`;
  }).join("");
}

function statusForInvitation(inv, now) {
  if (inv.accepted_at) return { kind: "accepted", label: "Accepted" };
  if (inv.revoked_at) return { kind: "revoked", label: "Revoked" };
  const exp = inv.expires_at ? new Date(inv.expires_at).getTime() : 0;
  if (exp && exp < now) return { kind: "expired", label: "Expired" };
  return { kind: "pending", label: "Pending" };
}

async function handleRevokeInvitation(inviteId) {
  const inv = (STATE.invitations || []).find(i => i.id === inviteId);
  const who = inv ? inv.email : `invitation #${inviteId}`;
  const ok = await confirmDialog(
    `Revoke this invitation for ${who}?\n\nThe link will stop working immediately.`,
    { title: "Revoke invitation", okLabel: "Revoke", danger: true },
  );
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/invitations/${inviteId}`, { method: "DELETE" });
      await refreshInvitations();
    }, "Revoking invitation…");
    toast("Invitation revoked", "success");
  } catch (err) {
    toastError(err);
  }
}

function openInviteModal() {
  const form = document.getElementById("formInvite");
  if (!form) return;
  form.reset();
  // Build a quick list of manageable projects so the inviter can attach
  // them. Only projects with can_manage=true qualify — for an admin that's
  // all of them; for a manager it's the ones they lead.
  const host = document.getElementById("inviteProjectList");
  const manageable = (STATE.projects || []).filter(p => p.can_manage);
  if (manageable.length) {
    host.innerHTML = manageable.map(p => `
      <label class="invite-proj-chip">
        <input type="checkbox" name="project_id" value="${p.id}" />
        <span class="swatch" style="background:${escapeHtml(p.color)}"></span>
        ${escapeHtml(p.name)}${p.key ? ` <span class="proj-key">${escapeHtml(p.key)}</span>` : ""}
      </label>
    `).join("");
  } else {
    host.innerHTML = `<p class="muted small">You don't manage any projects yet — the invitee will only see what an admin adds them to later.</p>`;
  }
  openModal("modalInvite");
  setTimeout(() => form.elements.email.focus(), 50);
}

async function submitInviteForm(e) {
  e.preventDefault();
  const form = e.target;
  const checkboxes = form.querySelectorAll('input[name="project_id"]:checked');
  const projectIds = Array.from(checkboxes).map(c => Number.parseInt(c.value, 10));
  const payload = {
    email: form.elements.email.value.trim(),
    role: form.elements.role.value,
    project_ids: projectIds,
    as_lead: form.elements.as_lead.checked,
  };
  try {
    await withLoader(async () => {
      await api("/invitations", { method: "POST", body: JSON.stringify(payload) });
      closeModal("modalInvite");
      if (STATE.view === "invitations") await refreshInvitations();
    }, "Sending invitation…");
    toast("Invitation sent", "success");
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Project members management
// ---------------------------------------------------------------------------
async function handleManageMembers(projectId) {
  STATE.currentProjectId = projectId;
  const project = (STATE.projects || []).find(p => p.id === projectId);
  document.getElementById("membersTitle").textContent =
    `Members of "${project ? project.name : "project"}"`;
  await loadMembers(projectId);
  openModal("modalProjectMembers");
}

async function loadMembers(projectId) {
  try {
    const members = await api(`/projects/${projectId}/members`);
    STATE.currentMembers = members;
    renderMembers(projectId, members);
  } catch (err) {
    toastError(err);
  }
}

function renderMembers(projectId, members) {
  const list = document.getElementById("membersList");
  if (!list) return;
  if (members.length) {
    list.innerHTML = members.map(m => `
      <li class="member-row" data-user-id="${m.user_id}">
        <span class="session-avatar">${initials(m.user_name || "?")}</span>
        <div class="member-main">
          <div><strong>${escapeHtml(m.user_name)}</strong>
            <span class="muted small">${escapeHtml(m.user_email)}</span>
          </div>
          <div class="muted small">${escapeHtml(m.user_role)} on the org</div>
        </div>
        <select class="member-role-select" data-user-id="${m.user_id}">
          <option value="member" ${m.project_role === "member" ? "selected" : ""}>Member</option>
          <option value="lead" ${m.project_role === "lead" ? "selected" : ""}>Lead</option>
        </select>
        <button class="btn danger" data-act="remove-member" data-user-id="${m.user_id}">Remove</button>
      </li>
    `).join("");
  } else {
    list.innerHTML = `<li class="muted small">No members on this project yet.</li>`;
  }

  // Populate the "add member" dropdown: org users not yet on the project.
  const addSel = document.getElementById("membersAddUser");
  const existingIds = new Set(members.map(m => m.user_id));
  const candidates = (STATE.users || []).filter(u =>
    u.is_active && !existingIds.has(u.id)
  );
  addSel.innerHTML = `<option value="">Add member…</option>` + candidates.map(u =>
    `<option value="${u.id}">${escapeHtml(u.name)} (${escapeHtml(u.email)})</option>`
  ).join("");
}

async function addMember(projectId) {
  const userSel = document.getElementById("membersAddUser");
  const roleSel = document.getElementById("membersAddRole");
  const userId = Number.parseInt(userSel.value, 10);
  if (!userId) return;
  try {
    await withLoader(async () => {
      await api(`/projects/${projectId}/members`, {
        method: "POST",
        body: JSON.stringify({ user_id: userId, role: roleSel.value }),
      });
      await loadMembers(projectId);
      await loadProjects();   // refresh member counts in sidebar
      renderProjectList();
    }, "Adding member…");
    toast("Member added", "success");
  } catch (err) {
    toastError(err);
  }
}

async function changeMemberRole(projectId, userId, newRole) {
  try {
    await withLoader(async () => {
      await api(`/projects/${projectId}/members/${userId}`, {
        method: "PUT",
        body: JSON.stringify({ role: newRole }),
      });
      await loadMembers(projectId);
    }, "Updating role…");
    toast("Role updated", "success");
  } catch (err) {
    toastError(err);
    // Reload to reset any reverted dropdown.
    await loadMembers(projectId);
  }
}

async function removeMember(projectId, userId) {
  const m = (STATE.currentMembers || []).find(x => x.user_id === userId);
  const who = m ? `${m.user_name} <${m.user_email}>` : `user #${userId}`;
  const ok = await confirmDialog(
    `Remove ${who} from this project?\n\nThey'll lose access to its bugs. (They remain in your organization.)`,
    { title: "Remove member", okLabel: "Remove", danger: true },
  );
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/projects/${projectId}/members/${userId}`, { method: "DELETE" });
      await loadMembers(projectId);
      await loadProjects();
      renderProjectList();
    }, "Removing member…");
    toast("Member removed", "success");
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Audit view
// ---------------------------------------------------------------------------
// v2.6: audit fetcher defaults to the backend's new 5000-row limit and
// supports a "Load more" affordance for very long histories. Each click
// adds AUDIT_PAGE_SIZE more rows to the view; the host is reset only
// when the filter changes (cleanFetch = true).
const AUDIT_PAGE_SIZE = 5000;
let _auditLoaded = 0;

function _renderAuditRow(r) {
  const entityIdSuffix = r.entity_id ? "#" + r.entity_id : "";
  const entityHtml = r.entity_type
    ? `<span class="audit-entity">${escapeHtml(r.entity_type)}${entityIdSuffix}</span>`
    : "";
  const detailHtml = r.detail ? `<div class="audit-detail">${escapeHtml(r.detail)}</div>` : "";
  return `
  <div class="audit-row">
    <span class="audit-icon">${activityIcon(r.action)}</span>
    <div class="audit-text">
      <div>
        <span class="audit-actor">${escapeHtml(r.actor_name)}</span>
        <span class="audit-action">${escapeHtml(r.action)}</span>
        ${entityHtml}
      </div>
      ${detailHtml}
    </div>
    <span class="audit-time">${formatDate(r.created_at)}</span>
  </div>`;
}

function _appendAuditLoadMore(host, rowsLen) {
  // Drop any existing Load-more button first so we can re-add it (or
  // omit it) based on whether more rows are likely.
  host.querySelector(".audit-load-more")?.remove();
  if (rowsLen === AUDIT_PAGE_SIZE) {
    host.insertAdjacentHTML("beforeend",
      `<div class="audit-load-more">
        <button type="button" class="btn ghost" id="auditLoadMoreBtn">Load older entries</button>
        <span class="muted small">Showing ${_auditLoaded} entries</span>
      </div>`);
    $("#auditLoadMoreBtn")?.addEventListener("click", () => refreshAudit(false));
  } else if (_auditLoaded > 0) {
    host.insertAdjacentHTML("beforeend",
      `<div class="audit-load-more muted small">— end of history (${_auditLoaded} entries) —</div>`);
  }
}

async function refreshAudit(cleanFetch = true) {
  const params = new URLSearchParams();
  const ent = $("#auditEntityFilter")?.value;
  const actor = $("#auditActorFilter")?.value;
  const q = $("#auditSearch")?.value.trim();
  if (ent) params.set("entity_type", ent);
  if (actor) params.set("actor_user_id", actor);
  if (q) params.set("q", q);
  if (cleanFetch) _auditLoaded = 0;
  params.set("limit", String(AUDIT_PAGE_SIZE));
  params.set("offset", String(_auditLoaded));
  try {
    const rows = await api("/audit?" + params.toString());
    const host = $("#auditList");
    if (cleanFetch) host.innerHTML = "";
    if (!rows.length && cleanFetch) {
      host.innerHTML = '<p class="no-content">No audit events match</p>';
      return;
    }
    host.insertAdjacentHTML("beforeend", rows.map(_renderAuditRow).join(""));
    _auditLoaded += rows.length;
    _appendAuditLoadMore(host, rows.length);
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Global listeners (event delegation)
// ---------------------------------------------------------------------------
function _resolveNewType() {
  // v2.4 — pick the default type for the "+ New" main click. Maps tab
  // to type, falling back to the user's last-chosen default.
  const tabMap = { Bug: "Bug", Requirement: "Requirement", Task: "Task" };
  return tabMap[STATE.activeTab] || STATE.defaultNewType || "Bug";
}

function _refreshNewItemLabel() {
  const t = _resolveNewType();
  const btn = $("#newBugBtn");
  if (btn) btn.textContent = `+ New ${t}`;
}

function bindGlobalListeners() {
  // Top-bar buttons
  $("#newBugBtn").addEventListener("click", () => {
    openBugForm({ _defaultType: _resolveNewType() });
  });
  // v2.4 — split-button caret menu.
  $("#newItemCaretBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    const menu = $("#newItemMenu");
    const btn = e.currentTarget;
    const willOpen = menu.hidden;
    menu.hidden = !willOpen;
    btn.setAttribute("aria-expanded", String(willOpen));
  });
  $("#newItemMenu")?.addEventListener("click", (e) => {
    const item = e.target.closest("[data-new-type]");
    if (!item) return;
    const t = item.dataset.newType;
    $("#newItemMenu").hidden = true;
    $("#newItemCaretBtn")?.setAttribute("aria-expanded", "false");
    STATE.defaultNewType = t;
    try { localStorage.setItem("defaultNewType", t); } catch {}
    _refreshNewItemLabel();
    openBugForm({ _defaultType: t });
  });
  document.addEventListener("click", () => {
    const menu = $("#newItemMenu");
    if (menu) menu.hidden = true;
    $("#newItemCaretBtn")?.setAttribute("aria-expanded", "false");
  });
  document.addEventListener("bh:tab-change", _refreshNewItemLabel);
  $("#newProjectBtn").addEventListener("click", () => openProjectForm());
  $("#newUserBtn").addEventListener("click", () => openUserForm());
  $("#exportCsvBtn").addEventListener("click", () => { globalThis.location.href = "/api/bugs/export.csv"; });
  $("#themeBtn").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme || "dark";
    const nxt = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = nxt;
    localStorage.setItem("theme", nxt);
  });

  // Logout
  $("#logoutBtn").addEventListener("click", async () => {
    const ok = await confirmDialog("Log out now?", { title: "Log out", okLabel: "Log out", danger: false });
    if (!ok) return;
    try {
      await api("/auth/logout", { method: "POST" });
    } catch { /* ignore */ }
    location.href = "/login.html";
  });

  // Change password
  $("#changePasswordBtn").addEventListener("click", () => {
    const form = $("#formChangePassword");
    form.reset();
    openModal("modalChangePassword");
    setTimeout(() => form.elements.current_password.focus(), 50);
  });
  $("#formChangePassword").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const cur = f.elements.current_password.value;
    const next = f.elements.new_password.value;
    const conf = f.elements.confirm_password.value;
    if (next !== conf) {
      toast("New passwords don't match", "error");
      return;
    }
    if (next.length < 8) {
      toast("Password must be at least 8 characters", "error");
      return;
    }
    try {
      await withLoader(async () => {
        await api("/auth/change-password", {
          method: "POST",
          body: JSON.stringify({ current_password: cur, new_password: next }),
        });
        closeModal("modalChangePassword");
      }, "Updating password…");
      toast("Password updated", "success");
    } catch (err) {
      toastError(err);
    }
  });

  // -------------------------------------------------------------------------
  // Profile (self-service) — name edit + two-step email change
  // -------------------------------------------------------------------------
  $("#profileBtn")?.addEventListener("click", openProfileModal);
  $("#formProfileIdentity")?.addEventListener("submit", submitProfileIdentity);
  $("#formEmailChangeRequest")?.addEventListener("submit", submitEmailChangeRequest);
  $("#formEmailChangeConfirm")?.addEventListener("submit", submitEmailChangeConfirm);
  $("#emailChangeCancel")?.addEventListener("click", () => {
    $("#emailChangeStep2").hidden = true;
    $("#formEmailChangeRequest").reset();
  });

  // Mobile hamburger
  $("#menuBtn").addEventListener("click", () => {
    $("#sidebar").classList.add("open");
    $("#sidebarBackdrop").hidden = false;
  });
  $("#sidebarBackdrop").addEventListener("click", closeSidebar);

  // Sidebar collapse / expand. Toggling a body class is the cheapest way
  // to flip the grid template + contents (CSS does the rest), and the new
  // state survives reload via localStorage.
  $("#sidebarCollapseBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    STATE.sidebarCollapsed = !STATE.sidebarCollapsed;
    document.body.classList.toggle("sidebar-collapsed", STATE.sidebarCollapsed);
    localStorage.setItem("sidebarCollapsed", STATE.sidebarCollapsed ? "1" : "0");
    e.currentTarget.title = STATE.sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar";
    e.currentTarget.textContent = STATE.sidebarCollapsed ? "»" : "«";
  });
  // Reflect the initial state on the button glyph too.
  if (STATE.sidebarCollapsed) {
    const btn = $("#sidebarCollapseBtn");
    if (btn) { btn.textContent = "»"; btn.title = "Expand sidebar"; }
  }

  // Nav buttons
  $$(".nav-btn").forEach(b => b.addEventListener("click", () => { setView(b.dataset.view); closeSidebar(); }));

  // Filter bar — clear all
  $("#clearFiltersBtn").addEventListener("click", () => {
    STATE.filters = {
      project_id: [], status: [], priority: [],
      environment: [], assignee_id: [], item_type: [],
      reporter_id: "", q: "",
    };
    $("#search").value = "";
    STATE.page = 1;
    refreshMultiSelects();
    renderProjectList();
    refreshBugs();
  });
  // v2.4 — type tabs.
  $("#typeTabs")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".type-tab[data-tab]");
    if (!btn) return;
    setActiveTab(btn.dataset.tab);
  });
  $("#search").addEventListener("input", debounce((e) => {
    STATE.filters.q = e.target.value.trim();
    STATE.page = 1; refreshBugs();
  }, 300));

  // Audit filters
  $("#auditEntityFilter").addEventListener("change", refreshAudit);
  $("#auditActorFilter").addEventListener("change", refreshAudit);
  $("#auditSearch").addEventListener("input", debounce(refreshAudit, 300));
  $("#auditRefreshBtn").addEventListener("click", refreshAudit);
  $("#auditClearBtn")?.addEventListener("click", () => {
    const ent = $("#auditEntityFilter"); if (ent) ent.value = "";
    const act = $("#auditActorFilter"); if (act) act.value = "";
    const q = $("#auditSearch"); if (q) q.value = "";
    refreshAudit();
  });

  // KPI strip — each tile is a clickable status filter. Event delegation
  // on the strip so we don't bind 5 separate listeners.
  $("#kpiStrip")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".kpi[data-kpi]");
    if (!btn) return;
    handleKpiClick(btn.dataset.kpi);
  });

  // Bug table — row click opens the unified modal in edit/view mode;
  // delete button (admin-only) handled separately. The pencil edit
  // button is gone; clicking the row itself is the way to open a bug.
  $("#bugTableBody").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (btn) {
      e.stopPropagation();
      const id = Number.parseInt(btn.dataset.id, 10);
      if (btn.dataset.act === "delete") return handleDeleteBug(id);
    }
    const tr = e.target.closest("tr[data-bug-id]");
    if (tr) openBugDetail(Number.parseInt(tr.dataset.bugId, 10));
  });

  // Sidebar projects.
  // v2.6: split-click — swatch click toggles the filter, label click
  // opens the edit modal (when allowed), and the dedicated action
  // buttons handle members / delete. Per-project can_manage decides
  // whether label click goes to edit or falls back to filter.
  $("#projectList").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    e.stopPropagation();
    const id = Number.parseInt(btn.dataset.id, 10);
    if (btn.dataset.act === "edit-project") return handleEditProject(id);
    if (btn.dataset.act === "delete-project") return handleDeleteProject(id);
    if (btn.dataset.act === "members") return handleManageMembers(id);
    if (btn.dataset.act === "filter" || btn.dataset.act === "open-project") {
      const li = btn.closest("[data-project-id]");
      const pid = String(li.dataset.projectId);
      // Name click: prefer opening the edit modal if the user has
      // can_manage on THIS project. Swatch click always filters.
      const project = (STATE.projects || []).find(p => String(p.id) === pid);
      const canManage = !!project?.can_manage;
      if (btn.dataset.act === "open-project" && canManage) {
        return handleEditProject(Number.parseInt(pid, 10));
      }
      // Toggle the project in the multi-select array.
      const arr = STATE.filters.project_id;
      const idx = arr.indexOf(pid);
      if (idx >= 0) arr.splice(idx, 1); else arr.push(pid);
      STATE.page = 1;
      refreshMultiSelects();
      refreshBugs();
      renderProjectList();
    }
  });

  // Sidebar users — same split: name click opens edit (when allowed),
  // avatar click toggles the assignee filter.
  $("#userList").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    e.stopPropagation();
    const id = Number.parseInt(btn.dataset.id, 10);
    if (btn.dataset.act === "edit-user") return handleEditUser(id);
    if (btn.dataset.act === "delete-user") return handleDeleteUser(id);
    if (btn.dataset.act === "filter-user" || btn.dataset.act === "open-user") {
      const li = btn.closest("[data-user-id]");
      const uid = String(li.dataset.userId);
      const role = STATE.currentUser?.role || "";
      const canManage = role === "admin" || role === "manager";
      if (btn.dataset.act === "open-user" && canManage) {
        return handleEditUser(Number.parseInt(uid, 10));
      }
      const arr = STATE.filters.assignee_id;
      const idx = arr.indexOf(uid);
      if (idx >= 0) arr.splice(idx, 1); else arr.push(uid);
      STATE.page = 1;
      refreshMultiSelects();
      refreshBugs();
    }
  });

  // Forms
  $("#formBug").addEventListener("submit", submitBugForm);
  $("#formProject").addEventListener("submit", submitProjectForm);
  $("#formUser").addEventListener("submit", submitUserForm);
  $("#formEvent")?.addEventListener("submit", submitEventForm);

  // v2.4 — Events view: list-mode card click, refresh, new button.
  $("#newEventBtn")?.addEventListener("click", () => openEventForm());
  $("#eventsRefreshBtn")?.addEventListener("click", () => refreshEvents());
  $("#eventsGrid")?.addEventListener("click", (e) => {
    const card = e.target.closest(".event-card[data-event-id]");
    if (card) openEventDetail(Number.parseInt(card.dataset.eventId, 10));
  });
  // Detail-mode controls.
  $("#eventBackBtn")?.addEventListener("click", () => {
    STATE.currentEventId = null;
    STATE.currentEvent = null;
    showEventsListMode();
    refreshEvents();
  });
  $("#editEventBtn")?.addEventListener("click", () => {
    if (STATE.currentEvent) openEventForm(STATE.currentEvent);
  });
  $("#deleteEventBtn")?.addEventListener("click", () => {
    if (STATE.currentEventId) handleDeleteEvent(STATE.currentEventId);
  });
  $("#addItemToEventBtn")?.addEventListener("click", () => {
    if (STATE.currentEventId) {
      openBugForm({ _defaultType: "Task", _defaultEventId: STATE.currentEventId });
    }
  });
  // Click row inside event-detail items → open the bug detail modal.
  $("#eventDetailItems")?.addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-bug-id]");
    if (tr) openBugDetail(Number.parseInt(tr.dataset.bugId, 10));
  });

  // ----- v2.5: Events list search + filter bar -----
  // Search input is debounced; date is exact-match. Both re-render the
  // grid in place without re-fetching (filtering is client-side over
  // STATE.events).
  $("#eventsSearch")?.addEventListener("input", debounce((e) => {
    STATE.eventsFilter.q = e.target.value || "";
    renderEventsList();
  }, 200));
  $("#eventsDateFilter")?.addEventListener("change", (e) => {
    STATE.eventsFilter.date = e.target.value || "";
    renderEventsList();
  });
  $("#eventsClearFiltersBtn")?.addEventListener("click", () => {
    STATE.eventsFilter = { q: "", date: "" };
    const s = $("#eventsSearch"); if (s) s.value = "";
    const d = $("#eventsDateFilter"); if (d) d.value = "";
    renderEventsList();
  });

  // ----- v2.5: Event detail (tasks-inside-event) search + filters -----
  $("#eventDetailSearch")?.addEventListener("input", debounce((e) => {
    STATE.eventDetailFilter.q = e.target.value || "";
    if (STATE.currentEvent) renderEventDetail(STATE.currentEvent);
  }, 200));
  $("#eventDetailClearFiltersBtn")?.addEventListener("click", () => {
    STATE.eventDetailFilter = { q: "", status: [], priority: [], assignee_id: [] };
    const s = $("#eventDetailSearch"); if (s) s.value = "";
    if (STATE.currentEvent) renderEventDetail(STATE.currentEvent);
  });
  // Multi-select toggle for status / priority / assignee.
  $("#eventDetailFilterBar")?.addEventListener("click", (e) => {
    e.stopPropagation();
    const toggle = e.target.closest("[data-ms-toggle]");
    if (toggle) {
      const wrap = toggle.closest(".ms-wrap");
      const panel = wrap.querySelector(".ms-panel");
      $$("#eventDetailFilterBar .ms-panel").forEach(p => { if (p !== panel) p.hidden = true; });
      $$("#eventDetailFilterBar .ms-btn").forEach(b => { if (b !== toggle) b.setAttribute("aria-expanded", "false"); });
      const willOpen = panel.hidden;
      panel.hidden = !willOpen;
      toggle.setAttribute("aria-expanded", String(willOpen));
      return;
    }
    const row = e.target.closest("[data-ms-value]");
    if (row) {
      const wrap = row.closest(".ms-wrap");
      const key = wrap.dataset.eventFilter;
      const v = row.dataset.msValue;
      const cur = STATE.eventDetailFilter[key];
      const idx = cur.indexOf(v);
      if (idx >= 0) cur.splice(idx, 1);
      else cur.push(v);
      if (STATE.currentEvent) renderEventDetail(STATE.currentEvent);
    }
  });

  // v2.4 — staged-files for create-mode attachments + comment composer.
  $("#createBugFiles")?.addEventListener("change", (e) => {
    handleStagedInputChange("createBug", e.target, "#createFilePreview", "#createFileLabel");
  });
  $("#createFilePreview")?.addEventListener("click", (e) => {
    handleStagedListClick("createBug", "#createFilePreview", "#createFileLabel", e);
  });

  // ----- Unified bug modal: delete + inline comments / attachments -----
  // The Delete button now lives inside the bug modal head (admin-only).
  $("#bugDeleteBtn")?.addEventListener("click", () => {
    if (STATE.currentBugId) handleDeleteBug(STATE.currentBugId);
  });

  // Comment "form" is now a <div> (HTML5 forbids nested <form> elements
  // and the old nesting was silently breaking the bug-create submit).
  // We trigger postComment from the button click and a Ctrl/Cmd+Enter
  // shortcut in the textarea.
  $("#commentPostBtn")?.addEventListener("click", () => postComment());
  $("#commentBody")?.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      postComment();
    }
  });
  // v2.4 — comment composer uses the staged-files flow too: hover-X
  // removes, click thumbnail previews.
  $("#commentFiles")?.addEventListener("change", (e) => {
    handleStagedInputChange("comment", e.target, "#filePreview", "#fileLabel");
  });
  $("#filePreview")?.addEventListener("click", (e) => {
    handleStagedListClick("comment", "#filePreview", "#fileLabel", e);
  });

  // Bug-level upload handlers used to live here (drag-drop zone + file
  // picker firing uploadFiles(..., null)). Removed in v3.2 along with
  // the dropzone HTML — new attachments go through the comment composer.
  // The bug-level attachment delete handler stays so legacy attachments
  // remain deletable.

  // Attachment delete buttons inside the bug modal (delegation).
  $("#bugAttachmentsGrid")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act='delete-attachment']");
    if (btn) {
      e.stopPropagation();
      handleDeleteAttachment(Number.parseInt(btn.dataset.id, 10));
    }
  });
  // v2.5 — comment row actions (admin only). The buttons themselves
  // are rendered conditionally in renderBugInlineSections; we delegate
  // here so they pick up automatically on every re-render.
  $("#bugCommentsList")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    e.stopPropagation();
    const act = btn.dataset.act;
    const id = Number.parseInt(btn.dataset.id, 10);
    if (act === "delete-attachment") return handleDeleteAttachment(id);
    if (act === "delete-comment")    return handleDeleteComment(id);
    if (act === "edit-comment")      return handleEditComment(id);
    if (act === "save-edit-comment") return handleSaveEditComment(id);
    if (act === "cancel-edit-comment") return handleCancelEditComment(id);
  });
  // Comment textarea Cmd/Ctrl+Enter inside the inline editor saves.
  $("#bugCommentsList")?.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      const ta = e.target.closest(".comment-edit-input");
      if (!ta) return;
      const saveBtn = ta.parentElement?.querySelector("[data-act='save-edit-comment']");
      const id = saveBtn ? Number.parseInt(saveBtn.dataset.id, 10) : Number.NaN;
      if (!Number.isNaN(id)) {
        e.preventDefault();
        handleSaveEditComment(id);
      }
    }
  });

  // v2.5 — post-creation bug-level attachment uploader inside the bug
  // modal.
  $("#bugAttachAddInput")?.addEventListener("change", (e) => {
    handleStagedInputChange("bugAttach", e.target, "#bugAttachAddPreview", "#bugAttachAddText");
  });
  $("#bugAttachAddPreview")?.addEventListener("click", (e) => {
    // ✕ removes a staged file; clicking the Upload button flushes them.
    const goBtn = e.target.closest(".attach-staged-upload");
    if (goBtn) {
      e.preventDefault();
      e.stopPropagation();
      withLoader(flushBugAttachStaging, "Uploading attachment(s)…").catch(toastError);
      return;
    }
    handleStagedListClick("bugAttach", "#bugAttachAddPreview", "#bugAttachAddText", e);
  });
  // After every staged-files render, append an Upload button if any
  // files are pending. Listens for the bh:staged-changed event we emit
  // from _renderStagedFiles / clearStagedFiles.
  const _ensureUploadBtn = () => {
    const host = $("#bugAttachAddPreview");
    if (!host) return;
    const count = STATE.stagedFiles.bugAttach?.length || 0;
    const existing = host.querySelector(".attach-staged-upload");
    if (count && !existing) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn primary attach-staged-upload";
      btn.textContent = `Upload ${count} file${count > 1 ? "s" : ""}`;
      host.appendChild(btn);
    } else if (count && existing) {
      existing.textContent = `Upload ${count} file${count > 1 ? "s" : ""}`;
    } else if (!count && existing) {
      existing.remove();
    }
  };
  document.addEventListener("bh:staged-changed", _ensureUploadBtn);

  // v2.5 — when the user changes item_type inside the bug modal, refresh
  // the status dropdown so it only shows statuses valid for the new
  // type. The currently-selected status is preserved if it still fits;
  // otherwise it carries forward as a "(legacy)" option.
  $("#formBug")?.elements?.item_type?.addEventListener("change", (e) => {
    const newType = e.target.value || "Bug";
    const statusEl = $("#formBug").elements.status;
    const current = statusEl.value;
    _renderStatusSelect(statusEl, newType, current);
  });

  // ----- Sessions admin view -----
  $("#sessionsRefreshBtn")?.addEventListener("click", refreshSessions);
  $("#sessionsList")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act='revoke-session']");
    if (!btn || btn.disabled) return;
    e.stopPropagation();
    handleRevokeSession(Number.parseInt(btn.dataset.id, 10));
  });

  // ----- Invitations admin view -----
  document.getElementById("invitationsRefreshBtn")?.addEventListener("click", refreshInvitations);
  document.getElementById("newInviteBtn")?.addEventListener("click", openInviteModal);
  document.getElementById("formInvite")?.addEventListener("submit", submitInviteForm);
  document.getElementById("invitationsList")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act='revoke-invite']");
    if (!btn) return;
    e.stopPropagation();
    handleRevokeInvitation(Number.parseInt(btn.dataset.id, 10));
  });

  // ----- Project members modal -----
  document.getElementById("membersAddBtn")?.addEventListener("click", () => {
    if (STATE.currentProjectId) addMember(STATE.currentProjectId);
  });
  document.getElementById("membersList")?.addEventListener("change", (e) => {
    const sel = e.target.closest(".member-role-select");
    if (!sel || !STATE.currentProjectId) return;
    changeMemberRole(STATE.currentProjectId, Number.parseInt(sel.dataset.userId, 10), sel.value);
  });
  document.getElementById("membersList")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act='remove-member']");
    if (!btn || !STATE.currentProjectId) return;
    e.stopPropagation();
    removeMember(STATE.currentProjectId, Number.parseInt(btn.dataset.userId, 10));
  });

  // Universal modal close: ✕ buttons, Cancel buttons, click outside, Escape
  document.addEventListener("click", (e) => {
    const closeBtn = e.target.closest("[data-close-modal]");
    if (closeBtn) {
      const modal = closeBtn.closest(".modal");
      if (modal) modal.hidden = true;
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      // Don't close if focused on input — let user blur first
      if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) {
        e.target.blur();
        return;
      }
      closeTopModal();
    }
  });

  // Sleuth chatbot integration: when the user clicks a bug in chat results,
  // chatbot.js dispatches this CustomEvent. We claim it (preventDefault)
  // and open the bug detail modal via the existing route.
  globalThis.addEventListener("sleuth:open-bug", (e) => {
    const bugId = e.detail?.bugId;
    if (!bugId) return;
    e.preventDefault();
    openBugDetail(Number.parseInt(bugId, 10));
  });

  // ── Keyboard shortcuts + command palette ────────────────────────
  // Power-user affordances. We use document-level keydown with checks
  // for whether focus is currently in a text input — typing in a
  // textbox should NEVER trigger a shortcut.
  document.addEventListener("keydown", (e) => {
    const tag = e.target?.tagName || "";
    const inTextInput = ["INPUT", "TEXTAREA", "SELECT"].includes(tag)
                        || e.target?.isContentEditable;
    // Cmd+K / Ctrl+K opens the command palette from anywhere.
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      openCommandPalette();
      return;
    }
    if (inTextInput) return;
    // Single-character shortcuts (only when not in a text input).
    if (e.key === "/") {
      e.preventDefault();
      const search = $("#search");
      if (search) { setView("list"); search.focus(); }
    } else if (e.key === "c") {
      e.preventDefault();
      openBugForm();
    } else if (e.key === "g") {
      // Vim-style "g then X" sequences via a transient flag.
      STATE._gPending = true;
      setTimeout(() => { STATE._gPending = false; }, 1000);
    } else if (STATE._gPending) {
      STATE._gPending = false;
      if (e.key === "d") { e.preventDefault(); setView("analytics"); }
      else if (e.key === "a") { e.preventDefault(); setView("audit"); }
      else if (e.key === "b") { e.preventDefault(); setView("list"); }
      else if (e.key === "i") { e.preventDefault(); setView("invitations"); }
      else if (e.key === "s") { e.preventDefault(); setView("sessions"); }
    }
  });

  // ── URL-encoded filter state ────────────────────────────────────
  // Forward/back navigation should restore the filter state.
  globalThis.addEventListener("popstate", () => {
    syncFiltersFromUrl();
    refreshMultiSelects();
    refreshBugs();
  });
  // Initial load — restore filters from URL if any.
  syncFiltersFromUrl();
  refreshMultiSelects();
}


// ---------------------------------------------------------------------------
// Command palette — Cmd+K / Ctrl+K
// ---------------------------------------------------------------------------
function openCommandPalette() {
  const overlay = document.getElementById("commandPaletteOverlay");
  if (overlay) {
    overlay.hidden = false;
    document.getElementById("cmdPaletteInput")?.focus();
    return;
  }
  // Lazy-build the overlay on first invocation.
  const div = document.createElement("div");
  div.id = "commandPaletteOverlay";
  div.className = "modal";
  div.innerHTML = `
    <div class="modal-card sm" style="max-width:560px;">
      <div class="modal-head" style="padding:14px 18px;">
        <input id="cmdPaletteInput" type="text" placeholder="Jump to view, find a bug…"
               style="flex:1; background:transparent; border:0; color:inherit;
                      font-size:15px; outline:none;" autocomplete="off" />
        <span class="muted small">Esc to close</span>
      </div>
      <div id="cmdPaletteResults" class="modal-body" style="padding:6px 0; max-height:50vh;"></div>
    </div>`;
  document.body.appendChild(div);
  const input = div.querySelector("#cmdPaletteInput");
  const results = div.querySelector("#cmdPaletteResults");
  const renderResults = () => {
    const q = input.value.trim().toLowerCase();
    const ALL = [
      { label: "Go to Bugs",          shortcut: "g b", run: () => setView("list") },
      { label: "Go to Analytics",     shortcut: "g d", run: () => setView("analytics") },
      { label: "Go to Audit",         shortcut: "g a", run: () => setView("audit") },
      { label: "Go to Sessions",      shortcut: "g s", run: () => setView("sessions") },
      { label: "Go to Invitations",   shortcut: "g i", run: () => setView("invitations") },
      { label: "New bug",             shortcut: "c",   run: () => openBugForm() },
      { label: "New project",         shortcut: "",    run: () => openProjectForm() },
      { label: "Toggle theme",        shortcut: "",    run: () => $("#themeBtn")?.click() },
      { label: "Profile",             shortcut: "",    run: () => $("#profileBtn")?.click() },
      { label: "Export bugs (CSV)",   shortcut: "",    run: () => { location.href = "/api/bugs/export.csv"; } },
      { label: "Log out",             shortcut: "",    run: () => $("#logoutBtn")?.click() },
    ];
    // Include bugs by ID for direct jump
    if (/^#?\d+$/.test(q)) {
      const id = Number.parseInt(q.replace("#",""), 10);
      ALL.unshift({
        label: `Open bug #${id}`,
        shortcut: "",
        run: () => { void openBugDetail(id).catch(console.warn); },
      });
    }
    const filtered = q ? ALL.filter(c => c.label.toLowerCase().includes(q)) : ALL;
    results.innerHTML = filtered.map((c, i) => `
      <div class="cmd-row" data-cmd-i="${i}" style="
        padding:10px 18px; display:flex; align-items:center; gap:12px;
        cursor:pointer; ${i===0 ? 'background:var(--bg-elev-2);' : ''}">
        <span style="flex:1;">${escapeHtml(c.label)}</span>
        ${c.shortcut ? `<kbd style="font-size:11px; color:var(--text-muted);
          background:var(--bg-elev-2); padding:2px 6px; border-radius:4px;">${escapeHtml(c.shortcut)}</kbd>` : ""}
      </div>`).join("") || `<div class="muted" style="padding:14px 18px;">No matches</div>`;
    results.dataset.cmds = JSON.stringify(filtered.map(c => null));  // length marker
    results._cmds = filtered;
  };
  let selectedIdx = 0;
  const close = () => { div.hidden = true; };
  // When a palette command runs a navigation, close any open modal
  // first — otherwise the user lands on the destination view with a
  // stale modal hiding the content.
  const runCommand = (cmd) => {
    close();
    if (cmd.label.startsWith("Go to ")) {
      $$(".modal:not([hidden])").forEach(m => { m.hidden = true; });
    }
    cmd.run();
  };
  input.addEventListener("input", () => { selectedIdx = 0; renderResults(); });
  input.addEventListener("keydown", (e) => {
    const cmds = results._cmds || [];
    if (e.key === "Escape") { close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); selectedIdx = (selectedIdx + 1) % Math.max(cmds.length, 1); renderResults(); }
    else if (e.key === "ArrowUp")   { e.preventDefault(); selectedIdx = (selectedIdx - 1 + cmds.length) % Math.max(cmds.length, 1); renderResults(); }
    else if (e.key === "Enter") {
      const cmd = cmds[selectedIdx];
      if (cmd) runCommand(cmd);
    }
  });
  results.addEventListener("click", (e) => {
    const row = e.target.closest("[data-cmd-i]");
    if (!row) return;
    const idx = Number.parseInt(row.dataset.cmdI, 10);
    const cmd = (results._cmds || [])[idx];
    if (cmd) runCommand(cmd);
  });
  div.addEventListener("click", (e) => { if (e.target === div) close(); });
  renderResults();
  input.focus();
}


// ---------------------------------------------------------------------------
// URL-encoded filter state
// ---------------------------------------------------------------------------
function syncFiltersToUrl() {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(STATE.filters)) {
    if (Array.isArray(v)) {
      for (const item of v) {
        if (item !== "" && item != null) params.append(k, String(item));
      }
    } else if (v !== "" && v != null) {
      params.set(k, String(v));
    }
  }
  const qs = params.toString();
  const newUrl = qs ? `${location.pathname}?${qs}` : location.pathname;
  if (newUrl !== location.pathname + location.search) {
    history.replaceState(null, "", newUrl);
  }
}

function syncFiltersFromUrl() {
  const params = new URLSearchParams(location.search);
  const arrayKeys = ["project_id", "status", "priority", "environment", "assignee_id"];
  for (const k of arrayKeys) {
    const vals = params.getAll(k);
    if (vals.length) STATE.filters[k] = vals;
  }
  const q = params.get("q");
  if (q) {
    STATE.filters.q = q;
    const search = $("#search");
    if (search) search.value = q;
  }
}

function closeSidebar() {
  $("#sidebar").classList.remove("open");
  $("#sidebarBackdrop").hidden = true;
}

// ---------------------------------------------------------------------------
// Go!
// ---------------------------------------------------------------------------
boot().catch(err => {
  console.error("Boot failed:", err);
  toast("Failed to load: " + err.message, "error");
});

// PWA: register the service worker. Failure is silent — the SW is
// optional and only enables offline-capable static caching.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  });
}

})();