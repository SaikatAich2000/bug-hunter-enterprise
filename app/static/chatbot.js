/* =========================================================================
 * Sleuth — Bug Hunter chatbot widget
 *
 * Self-contained vanilla JS module that mounts a floating bottom-right
 * chat launcher and panel. It is deliberately wrapped in its own IIFE
 * so it shares NOTHING with the main SPA except the network — no
 * polluted globals, no shared event listeners, no shared state.
 *
 * Why isolated? The main SPA already manages a lot (modals, navigation,
 * filters). Coupling the chatbot to it would make either side harder to
 * change. The chatbot only needs three things:
 *
 *   1. The user must already be logged in (session cookie supplies auth).
 *   2. The /api/chat/ask endpoint to talk to.
 *   3. Either a "go to bug" event or a fallback to set window.location
 *      when the user clicks a bug ID.
 *
 * Security:
 *   - All server-supplied text is escaped before being inserted in the
 *     DOM. We never set innerHTML with raw response data; the only
 *     sanctioned places are markdown-formatting transforms below, each
 *     of which start from already-escaped strings.
 *   - File downloads go through /api/chat/download/<token> which
 *     enforces the same session cookie as the rest of the app.
 * ===================================================================== */
(() => {
"use strict";

// Don't double-mount if the script is included twice (e.g. dev hot-reload).
if (window.__sleuthMounted) return;
window.__sleuthMounted = true;

// ---------------------------------------------------------------------------
// Tiny helpers
// ---------------------------------------------------------------------------
const $ = (sel, root = document) => root.querySelector(sel);

const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

const formatBytes = (n) => {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
};

/**
 * Convert a tiny markdown subset to HTML. Operates on an already-escaped
 * string so user text can never inject tags. Supported:
 *    **bold**         -> <strong>
 *    *italic*         -> <em>
 *    `code`           -> <code>
 *    [text](url)      -> <a>
 *    - bullet list    -> <ul><li>...</li></ul>
 *    Newlines         -> <br> (within a paragraph)
 *
 * Anything else is left as-is. Order matters: bold before italic so
 * "**foo**" doesn't get half-eaten.
 */
function mdLite(escaped) {
  let s = escaped;
  // Code spans first (so we don't transform markdown inside them).
  s = s.replace(/`([^`]+)`/g, (_m, code) => `<code>${code}</code>`);
  // Bold and italic — bold first.
  s = s.replace(/\*\*([^*]+)\*\*/g, (_m, b) => `<strong>${b}</strong>`);
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, (_m, pre, it) => `${pre}<em>${it}</em>`);
  // Links — only http(s)://, mailto:, or fragment hashes for safety.
  s = s.replace(/\[([^\]]+)\]\(((?:https?:\/\/|mailto:|#)[^)]+)\)/g,
    (_m, txt, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${txt}</a>`);
  // Bulleted lists (lines starting with "- "). Process line-by-line.
  const lines = s.split(/\n/);
  const out = [];
  let inList = false;
  for (const line of lines) {
    const m = line.match(/^- (.*)$/);
    if (m) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${m[1]}</li>`);
    } else {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(line);
    }
  }
  if (inList) out.push("</ul>");
  // Wrap consecutive plain lines into paragraphs separated by blank lines.
  const joined = out.join("\n").replace(/\n{2,}/g, "</p><p>").replace(/\n/g, "<br>");
  return `<p>${joined}</p>`;
}

// ---------------------------------------------------------------------------
// Build the DOM. We do this in JS rather than baking it into index.html
// so the widget is a single self-contained drop-in — adding it to a new
// page is just <script src="chatbot.js"></script>.
// ---------------------------------------------------------------------------
function buildDom() {
  const fab = document.createElement("button");
  fab.className = "sleuth-fab";
  fab.type = "button";
  fab.id = "sleuthFab";
  fab.setAttribute("aria-label", "Open Sleuth chatbot");
  fab.setAttribute("aria-expanded", "false");
  fab.title = "Ask Sleuth";
  fab.innerHTML = '<img src="/static/sleuth.svg" alt="" draggable="false" class="sleuth-fab-icon">';

  const panel = document.createElement("aside");
  panel.className = "sleuth-panel";
  panel.id = "sleuthPanel";
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Sleuth chatbot");
  panel.innerHTML = `
    <header class="sleuth-header">
      <div class="sleuth-avatar" aria-hidden="true"><img src="/static/sleuth.svg" alt="" class="sleuth-avatar-icon"></div>
      <div class="sleuth-title-block">
        <div class="sleuth-title">Sleuth</div>
        <div class="sleuth-status">Your Bug Hunter assistant</div>
      </div>
      <div class="sleuth-header-actions">
        <button type="button" class="sleuth-icon-btn" id="sleuthClear" title="Clear conversation"
                aria-label="Clear conversation">↻</button>
        <button type="button" class="sleuth-icon-btn" id="sleuthClose" title="Close" aria-label="Close">✕</button>
      </div>
    </header>
    <div class="sleuth-body" id="sleuthBody" aria-live="polite" aria-relevant="additions"></div>
    <div class="sleuth-input-row">
      <textarea id="sleuthInput"
                class="sleuth-input"
                placeholder="Ask anything — e.g. open bugs assigned to me"
                rows="1"
                maxlength="2000"
                autocomplete="off"></textarea>
      <button type="button" class="sleuth-send" id="sleuthSend">Send</button>
    </div>
  `;

  document.body.appendChild(fab);
  document.body.appendChild(panel);
  return { fab, panel };
}

// ---------------------------------------------------------------------------
// Renderers — turn server response blocks into DOM elements.
// All text first goes through escapeHtml(); only known transforms get
// to add tags afterwards.
// ---------------------------------------------------------------------------
function renderTextBlock(block) {
  const div = document.createElement("div");
  const safe = escapeHtml(block.payload.text || "");
  div.innerHTML = mdLite(safe);

  // Special case: the bug-detail handler emits an "Open in Bug Hunter"
  // pseudo-link that we intercept and turn into a click handler that
  // opens the existing modal in the SPA. Falling back to navigation if
  // the SPA isn't present (e.g. the widget gets embedded on a static
  // help page in the future).
  if (block.payload.open_bug_id) {
    div.querySelectorAll(`a[href="#open-bug-${block.payload.open_bug_id}"]`).forEach(a => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        openBugInSpa(block.payload.open_bug_id);
      });
    });
  }
  return div;
}

function renderTableBlock(block) {
  const wrap = document.createElement("div");
  wrap.className = "sleuth-table-wrap";
  const tbl = document.createElement("table");
  tbl.className = "sleuth-table";

  const thead = document.createElement("thead");
  const trh = document.createElement("tr");
  for (const h of (block.payload.headers || [])) {
    const th = document.createElement("th");
    th.textContent = h;          // textContent = automatic escaping
    trh.appendChild(th);
  }
  thead.appendChild(trh);
  tbl.appendChild(thead);

  const tbody = document.createElement("tbody");
  const ids = block.payload.row_bug_ids || [];
  (block.payload.rows || []).forEach((row, idx) => {
    const tr = document.createElement("tr");
    if (ids[idx]) {
      tr.classList.add("clickable");
      tr.dataset.bugId = String(ids[idx]);
      tr.addEventListener("click", () => openBugInSpa(ids[idx]));
      tr.title = "Open this bug";
    }
    for (const cell of row) {
      const td = document.createElement("td");
      td.textContent = String(cell ?? "");
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  });
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
  return wrap;
}

function renderFileBlock(block) {
  const wrap = document.createElement("div");
  wrap.className = "sleuth-file";

  const icon = document.createElement("div");
  icon.className = "sleuth-file-icon";
  icon.textContent = "📊";
  wrap.appendChild(icon);

  const info = document.createElement("div");
  info.className = "sleuth-file-info";
  const name = document.createElement("div");
  name.className = "sleuth-file-name";
  name.textContent = block.payload.filename || "bugs.xlsx";
  const meta = document.createElement("div");
  meta.className = "sleuth-file-meta";
  const rowCount = block.payload.row_count;
  meta.textContent = [
    rowCount != null ? (rowCount + " row" + (rowCount === 1 ? "" : "s")) : "",
    formatBytes(block.payload.size_bytes),
  ].filter(Boolean).join(" · ");
  info.appendChild(name);
  info.appendChild(meta);
  wrap.appendChild(info);

  const btn = document.createElement("a");
  btn.className = "sleuth-file-btn";
  btn.textContent = "Download";
  btn.href = "/api/chat/download/" + encodeURIComponent(block.payload.download_token);
  btn.download = block.payload.filename || "bugs.xlsx";
  // We don't open in a new tab — the browser will handle the download
  // and stay on the current page. This makes screen-reader behavior
  // predictable too.
  wrap.appendChild(btn);
  return wrap;
}

function renderSuggestionsBlock(block) {
  const wrap = document.createElement("div");
  wrap.className = "sleuth-suggestions";
  for (const s of (block.payload.items || [])) {
    // Items can be plain strings (legacy) or {label, send} objects.
    const label = (typeof s === "string") ? s : (s.label || "");
    const send  = (typeof s === "string") ? s : (s.send || s.label || "");
    if (!label) continue;
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.addEventListener("click", () => {
      sendMessage(send);
    });
    wrap.appendChild(b);
  }
  return wrap;
}

// Confirmation blocks are emitted whenever Sleuth needs explicit user
// approval before performing a write. Two buttons: Yes (sends "yes" so
// the executor pops the staged action and runs it) and Cancel (sends
// "no" to clear it). We intentionally render this as a row of large
// buttons so users on touch devices can hit them without aiming.
function renderConfirmBlock(block) {
  const wrap = document.createElement("div");
  wrap.className = "sleuth-confirm";
  const summary = document.createElement("div");
  summary.className = "sleuth-confirm-summary";
  summary.textContent = block.payload.summary || "Confirm action";
  wrap.appendChild(summary);

  const row = document.createElement("div");
  row.className = "sleuth-confirm-actions";

  const yes = document.createElement("button");
  yes.type = "button";
  yes.className = "sleuth-confirm-yes";
  yes.textContent = block.payload.yes_label || "Yes, do it";
  yes.addEventListener("click", () => {
    // Disable both buttons immediately so an over-eager user can't
    // double-click and accidentally fire two yeses (the second would
    // hit the now-empty pending slot and respond "nothing pending",
    // but it still wastes a round-trip).
    yes.disabled = true; no.disabled = true;
    sendMessage("yes");
  });

  const no = document.createElement("button");
  no.type = "button";
  no.className = "sleuth-confirm-no";
  no.textContent = block.payload.no_label || "Cancel";
  no.addEventListener("click", () => {
    yes.disabled = true; no.disabled = true;
    sendMessage("no");
  });

  row.appendChild(yes);
  row.appendChild(no);
  wrap.appendChild(row);
  return wrap;
}

// ---------------------------------------------------------------------------
// Bug-open shim — the chatbot exists as a sibling of the SPA, not a part
// of it. When the user clicks a bug row we'd LOVE to open the existing
// modal directly, but the SPA's open function is scoped inside its own
// IIFE. We dispatch a custom event the SPA can opt into; if no listener
// handles it, fall back to a query-string navigation.
// ---------------------------------------------------------------------------
function openBugInSpa(bugId) {
  const ev = new CustomEvent("sleuth:open-bug", {
    detail: { bugId },
    cancelable: true,
  });
  const handled = !window.dispatchEvent(ev);
  if (handled || ev.defaultPrevented) return;
  // Fallback: hash-based deep link. The SPA can read this on first load.
  // If we're already on the app, we still trigger by setting hash and
  // forcing a hashchange listener (which the SPA registers below).
  if (location.pathname === "/") {
    location.hash = "bug-" + bugId;
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } else {
    location.href = "/#bug-" + bugId;
  }
}

// ---------------------------------------------------------------------------
// State + DOM bookkeeping
// ---------------------------------------------------------------------------
const state = {
  open: false,
  inFlight: false,
};

let bodyEl, inputEl, sendBtn, fabEl, panelEl;

// Local message log so a "clear" doesn't lose the welcome message but
// does flush user/bot exchanges. We do NOT persist across reloads —
// chat history can contain sensitive bug info, and on a shared device
// the user shouldn't have to remember to clear it.
let renderedAnyUserMsg = false;

function appendRaw(node) {
  bodyEl.appendChild(node);
  bodyEl.scrollTop = bodyEl.scrollHeight;
}

function appendUserMessage(text) {
  const div = document.createElement("div");
  div.className = "sleuth-msg user";
  div.textContent = text;     // user text never gets HTML interpretation
  appendRaw(div);
  renderedAnyUserMsg = true;
}

function appendBotBlocks(blocks) {
  // We collect all blocks for ONE response inside a single .sleuth-msg
  // bubble so they read as one reply, then push to the body.
  const wrap = document.createElement("div");
  wrap.className = "sleuth-msg bot";
  for (const b of (blocks || [])) {
    let node = null;
    try {
      if (b.kind === "text") node = renderTextBlock(b);
      else if (b.kind === "table") node = renderTableBlock(b);
      else if (b.kind === "file") node = renderFileBlock(b);
      else if (b.kind === "suggestions") node = renderSuggestionsBlock(b);
      else if (b.kind === "confirm") node = renderConfirmBlock(b);
      else continue;
    } catch (err) {
      console.error("Sleuth render error:", err);
      continue;
    }
    if (node) wrap.appendChild(node);
  }
  // If for some reason the response had no renderable blocks, show a
  // safe default so the UI doesn't go silent.
  if (!wrap.children.length) {
    const p = document.createElement("p");
    p.textContent = "(no answer)";
    wrap.appendChild(p);
  }
  appendRaw(wrap);
}

function appendBotError(message) {
  const wrap = document.createElement("div");
  wrap.className = "sleuth-msg bot error";
  const p = document.createElement("p");
  p.textContent = message || "Something went wrong.";
  wrap.appendChild(p);
  appendRaw(wrap);
}

let typingNode = null;
function showTyping() {
  if (typingNode) return;
  typingNode = document.createElement("div");
  typingNode.className = "sleuth-typing";
  typingNode.setAttribute("aria-label", "Sleuth is thinking");
  typingNode.innerHTML = "<span></span><span></span><span></span>";
  appendRaw(typingNode);
}
function hideTyping() {
  if (typingNode) {
    typingNode.remove();
    typingNode = null;
  }
}

function showWelcome() {
  bodyEl.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "sleuth-msg bot";
  wrap.innerHTML = mdLite(
    "Hi! I'm **Sleuth** — your Bug Hunter assistant.\n\n" +
    "I can **answer questions** like:\n" +
    "- *show open bugs assigned to alice*\n" +
    "- *how many critical bugs in PROD?*\n" +
    "- *export bugs in apollo to excel*\n" +
    "- *bug 42*  ·  *summary*\n\n" +
    "I can also **do things** (with your confirmation):\n" +
    "- *close bug 5*  ·  *reopen #12*\n" +
    "- *assign bug 7 to bob*\n" +
    "- *set bug 3 priority to high*\n" +
    "- *comment on #5: looks fixed*\n" +
    "- *create a bug titled \"Login broken\" in project Apollo*\n\n" +
    "Type **help** for the full guide."
  );
  appendRaw(wrap);
  renderedAnyUserMsg = false;
}

// ---------------------------------------------------------------------------
// Network
// ---------------------------------------------------------------------------
async function callAsk(message) {
  const res = await fetch("/api/chat/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
    credentials: "include",
  });
  if (res.status === 401) {
    // Session has expired in the background. Bounce — but only if we're
    // still on a page that expects auth. The widget gets removed by the
    // navigation either way.
    location.replace("/login.html");
    const err = new Error("Not authenticated");
    err.silent = true;
    throw err;
  }
  if (!res.ok) {
    let detail = "HTTP " + res.status;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch { /* not JSON */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return await res.json();
}

async function sendMessage(text) {
  text = (text || "").trim();
  if (!text || state.inFlight) return;
  state.inFlight = true;
  sendBtn.disabled = true;

  appendUserMessage(text);
  showTyping();
  try {
    const data = await callAsk(text);
    hideTyping();
    appendBotBlocks(data.blocks || []);
  } catch (err) {
    hideTyping();
    if (err && err.silent) return;          // navigation already underway
    appendBotError(err && err.message ? err.message : "Network error.");
  } finally {
    state.inFlight = false;
    sendBtn.disabled = false;
    inputEl.focus();
  }
}

// ---------------------------------------------------------------------------
// Open / close
// ---------------------------------------------------------------------------
function openPanel() {
  if (state.open) return;
  state.open = true;
  panelEl.hidden = false;
  fabEl.setAttribute("aria-expanded", "true");
  fabEl.title = "Hide Sleuth";
  if (!renderedAnyUserMsg) {
    // First open in this session — show the welcome banner.
    showWelcome();
  }
  setTimeout(() => inputEl.focus(), 50);
}
function closePanel() {
  if (!state.open) return;
  state.open = false;
  panelEl.hidden = true;
  fabEl.setAttribute("aria-expanded", "false");
  fabEl.title = "Ask Sleuth";
  fabEl.focus();
}

function toggle() {
  state.open ? closePanel() : openPanel();
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
function wire() {
  fabEl.addEventListener("click", (e) => {
    e.stopPropagation();
    toggle();
  });
  $("#sleuthClose").addEventListener("click", closePanel);
  $("#sleuthClear").addEventListener("click", () => {
    showWelcome();
  });

  // Auto-grow textarea up to the CSS max-height.
  function autosize() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + "px";
  }

  // Single send path — read the current value, clear the field, then
  // dispatch. We do this in the click and the Enter key listeners.
  function sendCurrent() {
    const text = inputEl.value;
    if (!text.trim() || state.inFlight) return;
    inputEl.value = "";
    autosize();
    sendMessage(text);
  }

  sendBtn.addEventListener("click", sendCurrent);
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendCurrent();
    }
  });
  inputEl.addEventListener("input", autosize);

  // Escape closes when the panel is open and focus is anywhere inside.
  panelEl.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      closePanel();
    }
  });

  // Keyboard shortcut to summon Sleuth: Ctrl+/  (or Cmd+/ on Mac)
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "/") {
      e.preventDefault();
      openPanel();
    }
  });
}

// ---------------------------------------------------------------------------
// Boot — only mount if there's a session. We're conservative: if the page
// is the login page or password-reset page, the chatbot doesn't appear.
// (Those pages don't load this script anyway, but defense-in-depth.)
// ---------------------------------------------------------------------------
function boot() {
  if (location.pathname.startsWith("/login") ||
      location.pathname.startsWith("/reset")) {
    return;
  }
  const dom = buildDom();
  fabEl = dom.fab;
  panelEl = dom.panel;
  bodyEl = $("#sleuthBody");
  inputEl = $("#sleuthInput");
  sendBtn = $("#sleuthSend");
  wire();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}

})();
