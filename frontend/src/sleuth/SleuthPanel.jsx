/**
 * Sleuth — floating launcher (#sleuthFab) + dialog panel (#sleuthPanel); classes match chatbot.css.
 * Security pipeline: server text → escapeHtml → mdLite → fail-closed sanitizeSleuth allowlist before dangerouslySetInnerHTML; other blocks render via auto-escaped JSX; bug links use data-open-bug + a delegated CSP-safe click handler dispatching "sleuth:open-bug".
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import { getAppName } from "../lib/branding";
import { sanitizeSleuth } from "../lib/sanitize";
import { useApp } from "../state/AppContext";
// Tiny helpers (.replace(/g) not replaceAll — tsconfig targets ES2020).
const ESCAPE_MAP = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ESCAPE_MAP[c]);

const formatBytes = (n) => {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
};

// Typed accessors for ChatBlock.payload (Record<string, unknown>).
const str = (v) => (typeof v === "string" ? v : "");
const num = (v) => (typeof v === "number" ? v : null);
const arr = (v) => (Array.isArray(v) ? v : []);
const rec = (v) =>
  v !== null && typeof v === "object" ? v : {};

// Safe URL schemes for mdLite links; fragments restricted to "#open-bug-<n>" (broader would allow arbitrary in-app fragments).
const _SAFE_URL_PREFIXES = ["http://", "https://", "mailto:"];
const _OPEN_BUG_FRAGMENT_RE = /^#open-bug-\d+$/;
function _hasSafeUrlPrefix(url) {
  if (_OPEN_BUG_FRAGMENT_RE.test(url)) return true;
  for (const p of _SAFE_URL_PREFIXES) {
    if (url.startsWith(p)) return true;
  }
  return false;
}

// mdLite: minimal markdown-to-HTML; input must already be html-escaped. Code spans
// are skipped so content isn't re-processed; lists become proper <ul> blocks.

/**
 * Render inline spans for one line: `code`, **bold**, *italic*, [text](url).
 * Bold is matched before italic so ** doesn't get half-consumed.
 */
function formatInline(line) {
  let out = "";
  let i = 0;
  const n = line.length;
  while (i < n) {
    const ch = line.charAt(i);
    // code span — emit verbatim, skip inner transforms
    if (ch === "`") {
      const close = line.indexOf("`", i + 1);
      if (close > i) {
        out += `<code>${line.slice(i + 1, close)}</code>`;
        i = close + 1;
        continue;
      }
      // unbalanced backtick — emit literally
      out += ch;
      i += 1;
      continue;
    }
    // link [text](url)
    if (ch === "[") {
      const rb = line.indexOf("]", i + 1);
      if (rb > i && line.charAt(rb + 1) === "(") {
        const rp = line.indexOf(")", rb + 2);
        if (rp > rb) {
          const txt = line.slice(i + 1, rb);
          const url = line.slice(rb + 2, rp);
          if (txt && url && _hasSafeUrlPrefix(url)) {
            out += `<a href="${url}" target="_blank" rel="noopener noreferrer">${txt}</a>`;
            i = rp + 1;
            continue;
          }
        }
      }
      out += ch;
      i += 1;
      continue;
    }
    // bold **...**
    if (ch === "*" && line.charAt(i + 1) === "*") {
      const close = line.indexOf("**", i + 2);
      if (close > i + 1) {
        out += `<strong>${formatInline(line.slice(i + 2, close))}</strong>`;
        i = close + 2;
        continue;
      }
    }
    // italic *...* (no embedded asterisks)
    if (ch === "*") {
      const close = line.indexOf("*", i + 1);
      if (close > i && !line.slice(i + 1, close).includes("*")) {
        out += `<em>${formatInline(line.slice(i + 1, close))}</em>`;
        i = close + 1;
        continue;
      }
    }
    out += ch;
    i += 1;
  }
  return out;
}

function mdLite(escaped) {
  const lines = escaped.split(/\n/);
  const blocks = [];
  let para = [];
  let listItems = [];

  const flushPara = () => {
    if (para.length) {
      blocks.push(`<p>${para.join("<br>")}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    if (listItems.length) {
      blocks.push(`<ul>${listItems.map((li) => `<li>${li}</li>`).join("")}</ul>`);
      listItems = [];
    }
  };

  for (const line of lines) {
    const m = /^- (.*)$/.exec(line);
    if (m) {
      flushPara();
      listItems.push(formatInline(m[1]));
      continue;
    }
    flushList();
    if (line.trim() === "") {
      flushPara();
    } else {
      para.push(formatInline(line));
    }
  }
  flushPara();
  flushList();
  return blocks.join("");
}

// Escape before mdLite, same as server-supplied text.
function welcomeHtml(appName) {
  return mdLite(escapeHtml(
  `Hi! I'm your **${appName} AI Assistant**.\n\n` +
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
    '- *create a bug titled "Login broken" in project Apollo*\n\n' +
    "Type **help** for the full guide.",
    ));
  }

// Message log — discriminated union. Not persisted: chat may contain sensitive bug data.

const KNOWN_KINDS = new Set(["text", "table", "file", "suggestions", "confirm"]);

// Panel sizing — drag grip (top-left, grows up-and-left) or one-click expand/shrink; persisted in localStorage.
const SLEUTH_MIN_W = 340;
const SLEUTH_MIN_H = 420;
const SLEUTH_DEFAULT_W = 400;
const SLEUTH_DEFAULT_H = 560;
const SLEUTH_SIZE_KEY = "bh.sleuth.size";
// Below this width the CSS media query goes near-fullscreen; skip inline size so they don't fight.
const SLEUTH_NARROW = 540;

function readStoredSize() {
  try {
    const raw = localStorage.getItem(SLEUTH_SIZE_KEY);
    if (raw) {
      const s = JSON.parse(raw);
      if (
        s !== null &&
        typeof s === "object" &&
        typeof s.w === "number" &&
        typeof s.h === "number"
      ) {
        return { w: s.w, h: s.h };
      }
    }
  } catch {
    /* malformed / unavailable storage — fall back to defaults */
  }
  return { w: SLEUTH_DEFAULT_W, h: SLEUTH_DEFAULT_H };
}

/** Occurrence-unique keys for rendered rows/blocks — never the array index.
 * Two equal payloads still get distinct keys via a per-signature ordinal. */
function withStableKeys(items, signatureOf) {
  const seen = new Map();
  return items.map((item) => {
    const signature = signatureOf(item);
    const ordinal = (seen.get(signature) ?? 0) + 1;
    seen.set(signature, ordinal);
    return { item, key: `${signature}#${ordinal}` };
  });
}

// Clamp to [min, viewport − margin].
function clampSize(w, h) {
  const maxW = Math.max(SLEUTH_MIN_W, window.innerWidth - 32);
  const maxH = Math.max(SLEUTH_MIN_H, window.innerHeight - 120);
  return {
    w: Math.round(Math.max(SLEUTH_MIN_W, Math.min(maxW, w))),
    h: Math.round(Math.max(SLEUTH_MIN_H, Math.min(maxH, h))),
  };
}

// Block renderers — one per ChatBlock.kind.
function TextBlock({ block }) {
  const safe = escapeHtml(str(block.payload.text) || "");
  let html = mdLite(safe);
  // Tag [label](#open-bug-N) pseudo-links with data-open-bug for the delegated CSP-safe handler; positive int ids only.
  const openBugId = num(block.payload.open_bug_id);
  if (openBugId != null && Number.isInteger(openBugId) && openBugId > 0) {
    html = html
      .split(`<a href="#open-bug-${openBugId}"`)
      .join(`<a href="#open-bug-${openBugId}" data-open-bug="${openBugId}"`);
  }
  return <div dangerouslySetInnerHTML={{ __html: sanitizeSleuth(html) }} />;
}

function TableBlock({ block }) {
  const headers = arr(block.payload.headers);
  const rows = arr(block.payload.rows);
  const ids = arr(block.payload.row_bug_ids);
  const columnKeys = withStableKeys(headers, (h) => String(h ?? ""));
  const keyedRows = withStableKeys(rows, (row) => arr(row).map((c) => String(c ?? "")).join("|"));
  return (
    <div className="sleuth-table-wrap">
      <table className="sleuth-table">
        <thead>
          <tr>
            {columnKeys.map(({ item: h, key }) => (
              <th key={key}>{String(h ?? "")}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {keyedRows.map(({ item: row, key }, idx) => {
            const rawId = num(ids[idx]);
            // Bug ids start at 1; reject 0 explicitly.
            const clickable = rawId != null && Number.isInteger(rawId) && rawId > 0;
            const bugId = clickable ? String(rawId) : undefined;
            return (
              <tr
                key={key}
                className={clickable ? "clickable" : undefined}
                data-bug-id={bugId}
                data-open-bug={bugId}
                title={clickable ? "Open this bug" : undefined}
              >
                {withStableKeys(arr(row), (cell) => String(cell ?? "")).map((cellEntry) => (
                  <td key={cellEntry.key}>{String(cellEntry.item ?? "")}</td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function FileBlock({ block }) {
  const filename = str(block.payload.filename) || "bugs.xlsx";
  const rowCount = num(block.payload.row_count);
  let rowCountText = "";
  if (rowCount != null) {
    const suffix = rowCount === 1 ? "" : "s";
    rowCountText = rowCount + " row" + suffix;
  }
  const meta = [rowCountText, formatBytes(num(block.payload.size_bytes))]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="sleuth-file">
      <div className="sleuth-file-icon">📊</div>
      <div className="sleuth-file-info">
        <div className="sleuth-file-name">{filename}</div>
        <div className="sleuth-file-meta">{meta}</div>
      </div>
      {/* No target=_blank — let the browser handle the download in place. */}
      <a
        className="sleuth-file-btn"
        href={"/api/chat/download/" + encodeURIComponent(str(block.payload.download_token))}
        download={filename}
      >
        Download
      </a>
    </div>
  );
}

function SuggestionsBlock({
  block,
  onSend,
}) {
  return (
    <div className="sleuth-suggestions">
      {withStableKeys(arr(block.payload.items), (s) => (typeof s === "string" ? s : str(rec(s).label))).map(({ item: s, key }) => {
        // Items are plain strings (legacy) or {label, send} objects.
        const label = typeof s === "string" ? s : str(rec(s).label);
        const send =
          typeof s === "string" ? s : str(rec(s).send) || str(rec(s).label);
        if (!label) return null;
        return (
          <button key={key} type="button" onClick={() => onSend(send)}>
            {label}
          </button>
        );
      })}
    </div>
  );
}

// Both buttons disable on first click to prevent double-submission.
function ConfirmBlock({
  block,
  onSend,
}) {
  const [used, setUsed] = useState(false);
  const choose = (answer) => {
    setUsed(true);
    onSend(answer);
  };
  return (
    <div className="sleuth-confirm">
      <div className="sleuth-confirm-summary">
        {str(block.payload.summary) || "Confirm action"}
      </div>
      <div className="sleuth-confirm-actions">
        <button
          type="button"
          className="sleuth-confirm-yes"
          disabled={used}
          onClick={() => choose("yes")}
        >
          {str(block.payload.yes_label) || "Yes, do it"}
        </button>
        <button
          type="button"
          className="sleuth-confirm-no"
          disabled={used}
          onClick={() => choose("no")}
        >
          {str(block.payload.no_label) || "Cancel"}
        </button>
      </div>
    </div>
  );
}

function BotMessage({
  blocks,
  onSend,
}) {
  // All blocks for one response share a bubble; unknown kinds are dropped.
  const known = blocks.filter((b) => KNOWN_KINDS.has(b.kind));
  return (
    <div className="sleuth-msg bot">
      {known.length === 0 ? (
        // No renderable blocks — keep the UI from going silent.
        <p>(no answer)</p>
      ) : (
        withStableKeys(known, (b) => String(b.kind)).map(({ item: b, key }) => {
          switch (b.kind) {
            case "text":
              return <TextBlock key={key} block={b} />;
            case "table":
              return <TableBlock key={key} block={b} />;
            case "file":
              return <FileBlock key={key} block={b} />;
            case "suggestions":
              return <SuggestionsBlock key={key} block={b} onSend={onSend} />;
            default:
              return <ConfirmBlock key={key} block={b} onSend={onSend} />;
          }
        })
      )}
    </div>
  );
}

export default function SleuthPanel() {
  const appName = getAppName();
  // Admins get the document-import affordance.
  const { isAdmin, refreshAll } = useApp();
  // Refresh right after a mutation so changes appear before the next poll.
  const refreshIfMutated = useCallback((intent) => {
      if (intent === "action_done" || intent === "ingest_done") void refreshAll();
    },
    [refreshAll],
  );
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [typing, setTyping] = useState(false);
  const [inFlight, setInFlight] = useState(false);
  const [unread, setUnread] = useState(0);

  const [size, setSize] = useState(readStoredSize);
  const [isNarrow, setIsNarrow] = useState(
    () => typeof window !== "undefined" && window.innerWidth <= SLEUTH_NARROW,
  );
  // Drag origin captured on pointerdown; null when not dragging.
  const resizeStart = useRef(null);

  // Refs for values async/imperative paths need without re-rendering.
  const openRef = useRef(false);
  const inFlightRef = useRef(false);
  const renderedAnyUserMsgRef = useRef(false);
  const nextIdRef = useRef(1);

  const bodyRef = useRef(null);
  const inputRef = useRef(null);
  const fabRef = useRef(null);
  const uploadRef = useRef(null);
  const panelRef = useRef(null);

  const push = useCallback((body) => {
    setMessages((prev) => [...prev, { ...body, id: nextIdRef.current++ }]);
  }, []);

  const showWelcome = useCallback(() => {
    setMessages([{ kind: "welcome", id: nextIdRef.current++ }]);
    renderedAnyUserMsgRef.current = false;
  }, []);

  const sendMessage = useCallback(async (raw) => {
      const text = (raw || "").trim();
      if (!text || inFlightRef.current) return;
      inFlightRef.current = true;
      setInFlight(true);

      push({ kind: "user", text }); // rendered , not HTML
      renderedAnyUserMsgRef.current = true;
      setTyping(true);
      try {
        const data = await api("/chat/ask", {
          method: "POST",
          json: { message: text },
        });
        setTyping(false);
        push({ kind: "bot", blocks: data.blocks || [] });
        refreshIfMutated(data.intent);
        if (!openRef.current) setUnread((u) => u + 1);
      } catch (err) {
        setTyping(false);
        // 401: api() already redirected to login.
        if (err instanceof ApiError && err.silent) return;
        push({
          kind: "error",
          text:
            err instanceof Error && err.message ? err.message : "Network error.",
        });
        if (!openRef.current) setUnread((u) => u + 1);
      } finally {
        inFlightRef.current = false;
        setInFlight(false);
        inputRef.current?.focus();
      }
    },
    [push, refreshIfMutated],
  );

  const openPanel = useCallback(() => {
    if (openRef.current) return;
    openRef.current = true;
    setOpen(true);
    setUnread(0);
    if (!renderedAnyUserMsgRef.current) {
      showWelcome();
    }
    globalThis.setTimeout(() => inputRef.current?.focus(), 50);
  }, [showWelcome]);

  const closePanel = useCallback(() => {
    if (!openRef.current) return;
    openRef.current = false;
    setOpen(false);
    fabRef.current?.focus();
  }, []);

  const toggle = () => (openRef.current ? closePanel() : openPanel());

  const onResizePointerDown = (e) => {
    e.preventDefault();
    e.stopPropagation();
    resizeStart.current = { x: e.clientX, y: e.clientY, w: size.w, h: size.h };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* synthetic event — drag still works via pointermove */
    }
  };
  const onResizePointerMove = (e) => {
    const s = resizeStart.current;
    if (!s) return;
    // Anchored bottom-right: dragging up / left enlarges the panel.
    setSize(clampSize(s.w + (s.x - e.clientX), s.h + (s.y - e.clientY)));
  };
  const onResizePointerUp = (e) => {
    if (!resizeStart.current) return;
    resizeStart.current = null;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* already released */
    }
  };

  // "Noticeably larger than default" counts  for the toggle icon.
  const isExpanded =
    size.w > SLEUTH_DEFAULT_W + 16 || size.h > SLEUTH_DEFAULT_H + 16;
  const toggleExpand = () => {
    setSize(
      isExpanded
        ? { w: SLEUTH_DEFAULT_W, h: SLEUTH_DEFAULT_H }
        : clampSize(560, 760),
    );
  };

  // Uncontrolled textarea — reads scrollHeight to auto-grow up to 120px.
  const autosize = () => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  };

  // Read, clear, then send — one place for all send paths.
  const sendCurrent = () => {
    const el = inputRef.current;
    if (!el) return;
    const text = el.value;
    if (!text.trim() || inFlightRef.current) return;
    el.value = "";
    autosize();
    void sendMessage(text);
  };

  const onInputKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendCurrent();
    }
  };

  // Admin uploads a file; the server creates one work item per entry and replies with a summary + link table.
  const onUploadClick = () => uploadRef.current?.click();

  const onFilePicked = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // reset so the same file can be picked again
    if (!file || inFlightRef.current) return;
    inFlightRef.current = true;
    setInFlight(true);
    if (!openRef.current) openPanel();
    push({ kind: "user", text: `📎 Uploaded ${file.name}` });
    renderedAnyUserMsgRef.current = true;
    setTyping(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const data = await api("/chat/ingest", { method: "POST", body: fd });
      setTyping(false);
      push({ kind: "bot", blocks: data.blocks || [] });
    } catch (err) {
      setTyping(false);
      if (err instanceof ApiError && err.silent) return;
      push({
        kind: "error",
        text: err instanceof Error && err.message ? err.message : "Upload failed.",
      });
    } finally {
      inFlightRef.current = false;
      setInFlight(false);
      inputRef.current?.focus();
    }
  };

  // Escape closes from anywhere inside the panel.
  const onPanelKeyDown = (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      closePanel();
    }
  };

  // Delegated handler for data-open-bug elements; the custom event keeps innerHTML free of inline handlers (CSP).
  const onPanelClick = (e) => {
    const target = e.target.closest(
      "[data-open-bug]",
    );
    if (!target) return;
    e.preventDefault();
    const bugId = Number(target.dataset.openBug);
    if (!Number.isInteger(bugId) || bugId <= 0) return; // reject zero / non-integers

    document.dispatchEvent(
      new CustomEvent("sleuth:open-bug", { detail: { bugId } }),
    );
  };

  // Panel-level listeners are attached imperatively — the dialog container carries
  // no JSX handlers — and the ref always holds the latest handler closures.
  const panelHandlersRef = useRef({ key: onPanelKeyDown, click: onPanelClick });
  useEffect(() => {
    panelHandlersRef.current = { key: onPanelKeyDown, click: onPanelClick };
  });
  useEffect(() => {
    const panel = panelRef.current;
    if (panel == null) return undefined;
    const onKey = (e) => panelHandlersRef.current.key(e);
    const onClick = (e) => panelHandlersRef.current.click(e);
    panel.addEventListener("keydown", onKey);
    panel.addEventListener("click", onClick);
    return () => {
      panel.removeEventListener("keydown", onKey);
      panel.removeEventListener("click", onClick);
    };
  }, []);

  // Global shortcut: Ctrl+/ (Cmd+/ on Mac).
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "/") {
        e.preventDefault();
        openPanel();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [openPanel]);

  // Scroll to the latest message.
  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, typing]);

  // Sync narrow flag and clamp stored size when the viewport changes.
  useEffect(() => {
    const onResize = () => {
      setIsNarrow(window.innerWidth <= SLEUTH_NARROW);
      setSize((s) => clampSize(s.w, s.h));
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Persist size so it survives reloads.
  useEffect(() => {
    try {
      localStorage.setItem(SLEUTH_SIZE_KEY, JSON.stringify(size));
    } catch {
      /* storage unavailable (e.g. private browsing) */
    }
  }, [size]);

  return (
    <>
      <button
        type="button"
        className="sleuth-fab"
        id="sleuthFab"
        ref={fabRef}
        aria-label={`Open ${appName} AI Assistant`}
        aria-expanded={open}
        title={open ? "Hide AI Assistant" : "Ask the AI Assistant"}
        data-unread={unread}
        onClick={(e) => {
          e.stopPropagation();
          toggle();
        }}
      >
        <img
          src="/static/sleuth.svg"
          alt=""
          draggable={false}
          className="sleuth-fab-icon"
        />
      </button>

      <div
        className="sleuth-panel"
        id="sleuthPanel"
        hidden={!open}
        // An explicit role="dialog" trips Sonar js:S6819; the assistant is an
        // injected chat panel, so role="complementary" keeps AT semantics
        // accurate while id/aria-label keep the labelled-region behaviour.
        role="complementary"
        aria-label={`${appName} AI Assistant`}
        style={isNarrow ? undefined : { width: `${size.w}px`, height: `${size.h}px` }}
        ref={panelRef}

      >
        {/* Drag-to-resize grip (grows up-and-left); hidden from AT — keyboard users have the Expand button. */}
        <div
          className="sleuth-resize"
          aria-hidden="true"
          title="Drag to resize"
          onPointerDown={onResizePointerDown}
          onPointerMove={onResizePointerMove}
          onPointerUp={onResizePointerUp}
        />
        <header className="sleuth-header">
          <div className="sleuth-avatar" aria-hidden="true">
            <img src="/static/sleuth.svg" alt="" className="sleuth-avatar-icon" />
          </div>
          <div className="sleuth-title-block">
            <div className="sleuth-title">{appName} AI Assistant</div>
            <div className="sleuth-status">Online</div>
          </div>
          <div className="sleuth-header-actions">
            <button
              type="button"
              className="sleuth-icon-btn"
              id="sleuthExpand"
              hidden={isNarrow}
              title={isExpanded ? "Shrink panel" : "Expand panel"}
              aria-label={isExpanded ? "Shrink assistant panel" : "Expand assistant panel"}
              aria-pressed={isExpanded}
              onClick={toggleExpand}
            >
              {isExpanded ? "⤡" : "⤢"}
            </button>
            <button
              type="button"
              className="sleuth-icon-btn"
              id="sleuthClear"
              title="Clear conversation"
              aria-label="Clear conversation"
              onClick={showWelcome}
            >
              ↻
            </button>
            <button
              type="button"
              className="sleuth-icon-btn"
              id="sleuthClose"
              title="Close"
              aria-label="Close"
              onClick={closePanel}
            >
              ✕
            </button>
          </div>
        </header>

        <div
          className="sleuth-body"
          id="sleuthBody"
          ref={bodyRef}
          aria-live="polite"
          aria-relevant="additions"
        >
          {messages.map((m) => {
            switch (m.kind) {
              case "welcome":
                return (
                  <div
                    key={m.id}
                    className="sleuth-msg bot"
                    dangerouslySetInnerHTML={{ __html: sanitizeSleuth(welcomeHtml(appName)) }}
                  />
                );
              case "user":
                return (
                  <div key={m.id} className="sleuth-msg user">
                    {m.text}
                  </div>
                );
              case "bot":
                return (
                  <BotMessage key={m.id} blocks={m.blocks} onSend={sendMessage} />
                );
              default:
                return (
                  <div key={m.id} className="sleuth-msg bot error">
                    <p>{m.text || "Something went wrong."}</p>
                  </div>
                );
            }
          })}
          {typing && (
            <div className="sleuth-typing" role="status" aria-label="Sleuth is thinking">
              <span aria-hidden="true"></span>
              <span aria-hidden="true"></span>
              <span aria-hidden="true"></span>
            </div>
          )}
        </div>

        <div className="sleuth-input-row">
          {/* Admin-only. Hidden file input driven by the paperclip button. */}
          {isAdmin && (
            <>
              <input
                ref={uploadRef}
                type="file"
                id="sleuthUpload"
                className="sleuth-upload-input"
                hidden
                accept=".txt,.csv,.tsv,.json,.md,.xlsx"
                tabIndex={-1}
                aria-hidden="true"
                onChange={onFilePicked}
              />
              <button
                type="button"
                className="sleuth-upload-btn"
                id="sleuthUploadBtn"
                title="Import a document of bugs — Sleuth creates an item per entry (admin)"
                aria-label="Import bugs from a document"
                disabled={inFlight}
                onClick={onUploadClick}
              >
                📎
              </button>
            </>
          )}
          <textarea
            id="sleuthInput"
            ref={inputRef}
            className="sleuth-input"
            placeholder="Ask Me Anything"
            rows={1}
            maxLength={2000}
            autoComplete="off"
            onChange={autosize}
            onKeyDown={onInputKeyDown}
          ></textarea>
          <button
            type="button"
            className="sleuth-send"
            id="sleuthSend"
            disabled={inFlight}
            onClick={sendCurrent}
          >
            Send
          </button>
        </div>
      </div>
    </>
  );
}
