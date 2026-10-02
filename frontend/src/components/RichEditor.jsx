/**
 * RichEditor — contentEditable rich-text editor with a custom toolbar.
 * The surface is uncontrolled (React renders only the shell); formatting is direct
 * DOM manipulation with execCommand as a last-resort fallback. Styling: .bh-rt-* in styles.css.
 */

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
} from "react";
import { fileTooLargeMessage } from "../lib/upload";
import { toast } from "../lib/toast";

// Unsafe-file filter: blocks executable extensions/MIME types from the attachment grid.
const UNSAFE_EXTS = new Set([
  "exe", "bat", "cmd", "com", "scr", "pif", "msi", "msp", "msc",
  "jar", "vbs", "vbe", "js", "jse", "wsf", "wsh",
  "ps1", "psm1", "psd1", "ps1xml", "psc1",
  "sh", "bash", "csh", "zsh", "ksh",
  "app", "deb", "rpm", "dmg", "appimage",
  "reg", "lnk", "inf", "ins", "isp",
  "ade", "adp", "cpl", "mde", "shs", "sct", "vb", "cer", "hta",
]);
const UNSAFE_MIMES = new Set([
  "application/x-msdownload",
  "application/x-msdos-program",
  "application/x-msi",
  "application/vnd.microsoft.portable-executable",
  "application/x-elf",
  "application/x-mach-binary",
  "application/x-sh",
  "application/x-shellscript",
]);
const EXT_RE = /\.([a-z0-9]+)$/;

function isUnsafeExt(filename) {
  if (!filename) return false;
  const m = EXT_RE.exec(filename.toLowerCase());
  const ext = m?.[1];
  return ext !== undefined && UNSAFE_EXTS.has(ext);
}

function isUnsafeMime(mime) {
  if (!mime) return false;
  const first = mime.toLowerCase().split(";")[0];
  return first !== undefined && UNSAFE_MIMES.has(first.trim());
}

function describeError(err) {
  return err instanceof Error && err.message ? err.message : String(err);
}

// execCommand("bold") can silently no-op in some ancestors, so inline toggles are pure DOM.
const INLINE_TOGGLE = {
  bold: "b",
  italic: "i",
  underline: "u",
  strikeThrough: "s",
};

// Zero-width space to arm inline-style state on a collapsed caret.
const ZWSP = "​";

// Word chars for auto word-select: alphanumerics plus Latin-extended, Greek, CJK ranges.
const WORD_RE = /[A-Za-z0-9_'\-À-ɏͰ-῿一-鿿]/;

const MAX_HISTORY = 100;

const RichEditor = forwardRef(function RichEditor(
  {
    initialHtml = "",
    onChange,
    onPasteFile,
    placeholder,
    ariaLabel,
    disabled = false,
    // false = plain-text field (item descriptions): no toolbar, no formatting keys.
    formatting = true,
    name,
    textareaId,
    onCtrlEnter,
  },
  ref,
) {
  const editorRef = useRef(null);
  const toolbarRef = useRef(null);
  const nativeRef = useRef(null);

  // Last in-editor selection; toolbar buttons restore it before running commands.
  const savedRangeRef = useRef(null);

  // Snapshot-based undo/redo: toolbar DOM mutations never hit the browser's native undo stack.
  const historyRef = useRef({
    stack: [],
    idx: -1,
    debounce: null,
    restoring: false,
  });

  // initialHtml read once on mount; later prop changes ignored (use setHtml() or remount).
  const initialHtmlRef = useRef(initialHtml);

  // Latest-prop refs so DOM-driven callbacks never close over stale values.
  const onChangeRef = useRef(onChange);
  const onPasteFileRef = useRef(onPasteFile);
  const onCtrlEnterRef = useRef(onCtrlEnter);
  useEffect(() => {
    onChangeRef.current = onChange;
    onPasteFileRef.current = onPasteFile;
    onCtrlEnterRef.current = onCtrlEnter;
  });

  // Treats bare <br>/<div>/<p> scaffolding and ZWSP wrappers  (trim doesn't strip ZWSP).
  const computeHtml = () => {
    const editor = editorRef.current;
    if (!editor) return "";
    const html = editor.innerHTML;
    const hasImg = editor.querySelector("img") !== null;
    const text = (editor.textContent ?? "").replaceAll("\u200B", "").trim();
    if (!hasImg && text === "") return "";
    return html.replaceAll("\u200B", "");
  };

  const sync = () => {
    const html = computeHtml();
    // Native textarea is the form-submission source of truth.
    if (nativeRef.current && nativeRef.current.value !== html) {
      nativeRef.current.value = html;
    }
    onChangeRef.current?.(html);
  };

  const getCaretOffset = ()=> {
    const editor = editorRef.current;
    if (!editor) return 0;
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return 0;
    const r = s.getRangeAt(0);
    if (!editor.contains(r.endContainer)) return 0;
    const pre = document.createRange();
    pre.selectNodeContents(editor);
    pre.setEnd(r.endContainer, r.endOffset);
    return pre.toString().length;
  };

  const setCaretOffset = (offset) => {
    const editor = editorRef.current;
    if (!editor) return;
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
    let remaining = offset;
    let target = null;
    let targetOffset = 0;
    let node = walker.nextNode();
    while (node) {
      const len = (node.nodeValue ?? "").length;
      if (remaining <= len) {
        target = node;
        targetOffset = remaining;
        break;
      }
      remaining -= len;
      node = walker.nextNode();
    }
    const range = document.createRange();
    if (target) {
      range.setStart(target, targetOffset);
    } else {
      // Offset past end — caret at the editor's end.
      range.selectNodeContents(editor);
      range.collapse(false);
    }
    range.collapse(true);
    const s = window.getSelection();
    if (!s) return;
    s.removeAllRanges();
    s.addRange(range);
  };

  const snapshot = useCallback(()=> {
    const editor = editorRef.current;
    const h = historyRef.current;
    if (!editor || h.restoring) return;
    const html = editor.innerHTML;
    if (h.idx >= 0 && h.stack[h.idx]?.html === html) return;
    if (h.idx < h.stack.length - 1) {
      // Drop the redo tail on new push.
      h.stack.length = h.idx + 1;
    }
    h.stack.push({ html, caret: getCaretOffset() });
    if (h.stack.length > MAX_HISTORY) {
      h.stack.shift();
    } else {
      h.idx++;
    }
  }, []);

  // Debounced snapshot: avoid one entry per keystroke.
  const scheduleSnapshot = ()=> {
    const h = historyRef.current;
    if (h.debounce !== null) window.clearTimeout(h.debounce);
    h.debounce = window.setTimeout(() => {
      h.debounce = null;
      snapshot();
    }, 600);
  };

  const flushSnapshot = ()=> {
    const h = historyRef.current;
    if (h.debounce !== null) {
      window.clearTimeout(h.debounce);
      h.debounce = null;
      snapshot();
    }
  };

  const restore = (entry) => {
    const editor = editorRef.current;
    if (!editor) return;
    const h = historyRef.current;
    h.restoring = true;
    editor.innerHTML = entry.html || "";
    try {
      setCaretOffset(entry.caret || 0);
    } catch {
      /* selection can fail if editor is detached */
    }
    h.restoring = false;
    sync();
  };

  const undoEdit = ()=> {
    flushSnapshot();
    const h = historyRef.current;
    if (h.idx <= 0) return false;
    h.idx--;
    const entry = h.stack[h.idx];
    if (entry) restore(entry);
    return true;
  };

  const redoEdit = ()=> {
    flushSnapshot();
    const h = historyRef.current;
    if (h.idx >= h.stack.length - 1) return false;
    h.idx++;
    const entry = h.stack[h.idx];
    if (entry) restore(entry);
    return true;
  };

  const resetHistory = useCallback(()=> {
    const h = historyRef.current;
    h.stack = [];
    h.idx = -1;
    if (h.debounce !== null) {
      window.clearTimeout(h.debounce);
      h.debounce = null;
    }
    snapshot();
  }, [snapshot]);

  const captureSelection = ()=> {
    const editor = editorRef.current;
    if (!editor) return;
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return;
    const r = s.getRangeAt(0);
    if (editor.contains(r.commonAncestorContainer)) savedRangeRef.current = r.cloneRange();
  };

  const ensureCaret = ()=> {
    const editor = editorRef.current;
    if (!editor) return;
    const s = window.getSelection();
    if (!s) return;
    // Stale savedRange (caret left the editor) → drop a fresh caret at the end so formatting acts on our editor.
    const saved = savedRangeRef.current;
    if (saved && editor.contains(saved.commonAncestorContainer)) {
      s.removeAllRanges();
      s.addRange(saved);
      return;
    }
    savedRangeRef.current = null;
    const r = document.createRange();
    r.selectNodeContents(editor);
    r.collapse(false);
    s.removeAllRanges();
    s.addRange(r);
    savedRangeRef.current = r.cloneRange();
  };

  // Returns true if the current selection is inside one of the given tags.
  const inAncestor = (tagNames) => {
    const editor = editorRef.current;
    if (!editor) return false;
    const wanted = new Set(tagNames.map((t) => t.toUpperCase()));
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    let n = s.getRangeAt(0).startContainer;
    while (n && n !== editor) {
      if (n.nodeType === Node.ELEMENT_NODE && wanted.has(n.tagName)) return true;
      n = n.parentNode;
    }
    return false;
  };

  // Collapsed-caret typing state is unreliable, so wrap a ZWSP placeholder and place the caret inside it.
  const toggleInlineAtCaret = (tag) => {
    const editor = editorRef.current;
    if (!editor) return false;
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (!editor.contains(r.commonAncestorContainer)) return false;
    if (!r.collapsed) return false; // non-empty selection: use the wrap path
    // Existing wrapper: exit by inserting a ZWSP after it (setStartAfter alone lets browsers greedily re-extend it).
    let n = r.startContainer;
    while (n && n !== editor) {
      if (n.nodeType === Node.ELEMENT_NODE && n.tagName.toLowerCase() === tag) {
        const sep = document.createTextNode(ZWSP);
        n.parentNode?.insertBefore(sep, n.nextSibling);
        const after = document.createRange();
        after.setStart(sep, 1); // caret after the ZWSP
        after.collapse(true);
        s.removeAllRanges();
        s.addRange(after);
        return true;
      }
      n = n.parentNode;
    }
    // No wrapper — insert `<tag>ZWSP</tag>` with the caret inside it.
    const wrapEl = document.createElement(tag);
    const zw = document.createTextNode(ZWSP);
    wrapEl.appendChild(zw);
    r.insertNode(wrapEl);
    const inside = document.createRange();
    inside.setStart(zw, 1); // after the ZWSP
    inside.collapse(true);
    s.removeAllRanges();
    s.addRange(inside);
    return true;
  };

  // Auto-select the word at the caret so a Bold/Italic click with no selection formats that word.
  const selectWordAtCaret = ()=> {
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (!r.collapsed) return true;
    // Caret in an ELEMENT node: descend into the adjacent text node so the char walk can run.
    let node = r.startContainer;
    let pos = r.startOffset;
    if (node.nodeType === Node.ELEMENT_NODE) {
      const kids = node.childNodes;
      const before = pos > 0 ? kids[pos - 1] : undefined;
      const at = pos < kids.length ? kids[pos] : undefined;
      if (before && before.nodeType === Node.TEXT_NODE) {
        node = before;
        pos = (node.textContent || "").length;
      } else if (at && at.nodeType === Node.TEXT_NODE) {
        node = at;
        pos = 0;
      } else {
        return false;
      }
    }
    if (!node || node.nodeType !== Node.TEXT_NODE) return false;
    const text = node.textContent || "";
    const left = pos > 0 ? text.charAt(pos - 1) : "";
    const right = pos < text.length ? text.charAt(pos) : "";
    if (!WORD_RE.test(left) && !WORD_RE.test(right)) return false;
    let start = pos;
    while (start > 0 && WORD_RE.test(text.charAt(start - 1))) start--;
    let end = pos;
    while (end < text.length && WORD_RE.test(text.charAt(end))) end++;
    if (start === end) return false;
    const wordRange = document.createRange();
    wordRange.setStart(node, start);
    wordRange.setEnd(node, end);
    s.removeAllRanges();
    s.addRange(wordRange);
    return true;
  };

  // Wrap/unwrap inline styles — pure DOM (execCommand("bold") can silently no-op in some ancestors).
  const applyInlineWrap = (tag) => {
    const editor = editorRef.current;
    if (!editor) return false;
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (r.collapsed) return false;
    if (!editor.contains(r.commonAncestorContainer)) return false;
    // Both ends sharing the same tag ancestor means the selection is fully inside a wrapper — unwrap.
    const findWrapper = (start) => {
      let n = start;
      while (n && n !== editor) {
        if (n.nodeType === Node.ELEMENT_NODE && n.tagName.toLowerCase() === tag) {
          return n;
        }
        n = n.parentNode;
      }
      return null;
    };
    const startAnc = findWrapper(r.startContainer);
    const endAnc = findWrapper(r.endContainer);
    if (startAnc && startAnc === endAnc) {
      const wrapEl = startAnc;
      const parent = wrapEl.parentNode;
      if (!parent) return false;
      // Capture first/last refs before moving children out — the re-selection uses them.
      const firstMoved = wrapEl.firstChild;
      const lastMoved = wrapEl.lastChild;
      while (wrapEl.firstChild) parent.insertBefore(wrapEl.firstChild, wrapEl);
      wrapEl.remove();
      // Re-select moved content so the user can re-toggle without reselecting.
      if (firstMoved && lastMoved) {
        const re = document.createRange();
        re.setStartBefore(firstMoved);
        re.setEndAfter(lastMoved);
        s.removeAllRanges();
        s.addRange(re);
      }
      return true;
    }
    // Wrap: surroundContents for well-formed ranges, extract+insert for cross-block selections.
    const wrapEl = document.createElement(tag);
    try {
      r.surroundContents(wrapEl);
    } catch {
      const frag = r.extractContents();
      wrapEl.appendChild(frag);
      r.insertNode(wrapEl);
    }
    // Re-select wrapped content so chained toggles work.
    const inside = document.createRange();
    inside.selectNodeContents(wrapEl);
    s.removeAllRanges();
    s.addRange(inside);
    return true;
  };

  // Toggle ul/ol on the block containing the caret (unwrap if already that type, else wrap).
  const applyList = (listTag) => {
    const editor = editorRef.current;
    if (!editor) return false;
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (!editor.contains(r.commonAncestorContainer)) return false;
    // Enclosing block element.
    const block = (()=> {
      let n = r.startContainer;
      while (n && n !== editor) {
        if (
          n.nodeType === Node.ELEMENT_NODE &&
          /^(LI|P|DIV|BLOCKQUOTE|PRE|H[1-6])$/.test(n.tagName)
        ) {
          return n;
        }
        n = n.parentNode;
      }
      return null;
    })();
    // Matching LI: unwrap to <p>; preceding items keep the list, following ones move to a new sibling list.
    if (block && block.tagName === "LI") {
      const list = block.parentElement;
      if (list && list.tagName === listTag.toUpperCase()) {
        const parent = list.parentNode;
        const p = document.createElement("p");
        while (block.firstChild) p.appendChild(block.firstChild);

        // LIs following `block` in this list.
        const after = [];
        let sib = block.nextSibling;
        while (sib) {
          after.push(sib);
          sib = sib.nextSibling;
        }

        if (parent) {
          parent.insertBefore(p, list.nextSibling);
          if (after.length) {
            const rest = document.createElement(listTag);
            for (const node of after) rest.appendChild(node);
            parent.insertBefore(rest, p.nextSibling);
          }
        }
        block.remove();
        if (!list.firstChild) list.remove(); // drop the list if now empty

        const re = document.createRange();
        re.setStart(p, 0);
        re.collapse(true);
        s.removeAllRanges();
        s.addRange(re);
        return true;
      }
    }
    // Wrap: (a) caret in a block → replace with <ul/ol><li>; (b) selection at root → extract into <li>; (c) collapsed caret at root → wrap all children.
    const list = document.createElement(listTag);
    const li = document.createElement("li");
    if (block && block !== editor) {
      while (block.firstChild) li.appendChild(block.firstChild);
      list.appendChild(li);
      block.parentNode?.replaceChild(list, block);
    } else if (!r.collapsed) {
      // (b)
      const frag = r.extractContents();
      li.appendChild(frag);
      list.appendChild(li);
      r.insertNode(list);
    } else {
      // (c)
      while (editor.firstChild) li.appendChild(editor.firstChild);
      list.appendChild(li);
      editor.appendChild(list);
    }
    // Empty <li> has no bullet in some browsers; a <br> makes it visible.
    if (!li.firstChild) li.appendChild(document.createElement("br"));
    const re = document.createRange();
    re.selectNodeContents(li);
    re.collapse(false);
    s.removeAllRanges();
    s.addRange(re);
    return true;
  };

  // Toggle a blockquote/pre around the current block.
  const applyBlockWrap = (tag) => {
    const editor = editorRef.current;
    if (!editor) return false;
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (!editor.contains(r.commonAncestorContainer)) return false;
    const existing = (()=> {
      let n = r.startContainer;
      while (n && n !== editor) {
        if (n.nodeType === Node.ELEMENT_NODE && n.tagName.toLowerCase() === tag) {
          return n;
        }
        n = n.parentNode;
      }
      return null;
    })();
    if (existing) {
      // Unwrap: hoist children into a <p> and remove the wrapper.
      const p = document.createElement("p");
      while (existing.firstChild) p.appendChild(existing.firstChild);
      existing.parentNode?.replaceChild(p, existing);
      const re = document.createRange();
      re.selectNodeContents(p);
      re.collapse(false);
      s.removeAllRanges();
      s.addRange(re);
      return true;
    }
    const block = (()=> {
      let n = r.startContainer;
      while (n && n !== editor) {
        if (
          n.nodeType === Node.ELEMENT_NODE &&
          /^(P|DIV|BLOCKQUOTE|PRE|H[1-6])$/.test(n.tagName)
        ) {
          return n;
        }
        n = n.parentNode;
      }
      return null;
    })();
    const wrapEl = document.createElement(tag);
    if (block && block !== editor) {
      while (block.firstChild) wrapEl.appendChild(block.firstChild);
      block.parentNode?.replaceChild(wrapEl, block);
    } else {
      // No block — wrap all editor content.
      while (editor.firstChild) wrapEl.appendChild(editor.firstChild);
      editor.appendChild(wrapEl);
    }
    const re = document.createRange();
    re.selectNodeContents(wrapEl);
    re.collapse(false);
    s.removeAllRanges();
    s.addRange(re);
    return true;
  };

  // Restore the last in-editor range, run the command, re-capture so chained clicks build on the right anchor.
  const runCmd = (cmd, arg) => {
    const editor = editorRef.current;
    if (!editor) return;
    editor.focus();
    ensureCaret();
    const inlineTag = INLINE_TOGGLE[cmd];
    if (inlineTag !== undefined) {
      const s = window.getSelection();
      if (s && s.rangeCount > 0 && s.getRangeAt(0).collapsed) {
        if (!selectWordAtCaret()) {
          // No word at caret — arm typing state with a ZWSP wrapper.
          if (toggleInlineAtCaret(inlineTag)) {
            captureSelection();
            sync();
            return;
          }
        }
      }
      if (applyInlineWrap(inlineTag)) {
        captureSelection();
        sync();
        return;
      }
    }
    if (cmd === "insertUnorderedList" && applyList("ul")) {
      captureSelection();
      sync();
      return;
    }
    if (cmd === "insertOrderedList" && applyList("ol")) {
      captureSelection();
      sync();
      return;
    }
    if (cmd === "formatBlock") {
      // arg is e.g. "<blockquote>", "<pre>", or "<p>"
      const tag = (arg || "").replace(/[<>]/g, "").toLowerCase();
      if (tag && tag !== "p" && applyBlockWrap(tag)) {
        captureSelection();
        sync();
        return;
      }
      if (tag === "p") {
        // "p" = plain — unwrap any existing block wrapper.
        for (const t of ["blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"]) {
          if (applyBlockWrap(t)) {
            captureSelection();
            sync();
            return;
          }
        }
      }
    }
    // execCommand fallback for commands without a manual implementation.
    try {
      document.execCommand(cmd, false, arg ?? undefined);
    } catch {
      /* ignore */
    }
    captureSelection();
    sync();
  };

  // Hidden file picker → onPasteFile; files are never embedded inline, only added to the attachment grid.
  const pickFileAsAttachment = ()=> {
    const handler = onPasteFileRef.current;
    if (!handler) {
      toast("Attach files via the attachment area for this form.", "info");
      return;
    }
    const f = document.createElement("input");
    f.type = "file";
    f.accept = "image/*,application/pdf";
    f.style.display = "none";
    f.addEventListener("change", () => {
      const file = f.files?.[0];
      if (file) {
        if (isUnsafeExt(file.name) || isUnsafeMime(file.type)) {
          toast(`Blocked unsafe file: ${file.name}`, "error");
        } else {
          try {
            const result = handler(file);
            if (result instanceof Promise) {
              result.catch((err) => {
                toast(`Failed to attach: ${describeError(err)}`, "error");
              });
            }
          } catch (err) {
            toast(`Failed to attach: ${describeError(err)}`, "error");
          }
        }
      }
      f.remove();
    });
    document.body.appendChild(f);
    f.click();
  };

  const handleToolbarCmd = (btn) => {
    const cmd = btn.dataset.cmd;
    if (!cmd) return;
    const arg = btn.dataset.arg || null;
    if (cmd === "bh-image") {
      pickFileAsAttachment();
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

  // Reflect active formatting on toolbar buttons via imperative classList (buttons render once).
  const updateActiveStates = ()=> {
    const toolbar = toolbarRef.current;
    if (!toolbar) return;
    toolbar.querySelectorAll("button[data-cmd]").forEach((b) => {
      const c = b.dataset.cmd ?? "";
      let active = false;
      // For manual-wrapped inline styles, check the DOM ancestor; fall back
      // to queryCommandState (unreliable for the wrapped path but fine otherwise).
      const inlineTag = INLINE_TOGGLE[c];
      if (inlineTag !== undefined) {
        active = inAncestor([inlineTag]);
        if (!active) {
          try {
            active = !!document.queryCommandState(c);
          } catch {
            /* unsupported command */
          }
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

  // Pre/post snapshots so undo treats each toolbar action  step.
  const runToolbarCmd = (btn) => {
    flushSnapshot();
    snapshot(); // pre-state
    handleToolbarCmd(btn);
    snapshot(); // post-state
  };

  // preventDefault on mousedown keeps focus (and typing state) in the editor; capture selection first.
  const handleToolbarMouseDown = (e) => {
    const target = e.target instanceof Element ? e.target : null;
    const btn = target?.closest("button[data-cmd]");
    if (!btn || btn.disabled) return;
    captureSelection();
    e.preventDefault();
    runToolbarCmd(btn);
  };

  // Keyboard path (Enter/Space); e.detail === 0 marks keyboard clicks, mouse is handled by mousedown.
  const handleToolbarClick = (e) => {
    if (e.detail !== 0) {
      e.preventDefault();
      return;
    }
    const target = e.target instanceof Element ? e.target : null;
    const btn = target?.closest("button[data-cmd]");
    if (!btn || btn.disabled) return;
    e.preventDefault();
    runToolbarCmd(btn);
  };

  const handleInput = ()=> {
    sync();
    scheduleSnapshot();
    updateActiveStates();
  };

  // Mirror external textarea writes (e.g. Playwright fill) into the surface.
  const handleNativeInput = ()=> {
    const editor = editorRef.current;
    const ta = nativeRef.current;
    if (!editor || !ta) return;
    if (editor.innerHTML !== ta.value) {
      editor.innerHTML = ta.value;
      sync();
      scheduleSnapshot();
    }
  };

  const handleKeyUp = ()=> {
    captureSelection();
    updateActiveStates();
  };

  const handleMouseUp = ()=> {
    captureSelection();
    updateActiveStates();
  };

  const handleFocus = ()=> {
    captureSelection();
  };

  const handleKeyDown = (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && !e.altKey && e.key === "Enter") {
      onCtrlEnterRef.current?.(e);
      return;
    }
    if (!mod || e.altKey) return;
    const key = e.key.toLowerCase();
    // Snapshot history for undo/redo — the native stack misses toolbar formatting changes.
    if (key === "z" && !e.shiftKey) {
      e.preventDefault();
      undoEdit();
      updateActiveStates();
      return;
    }
    if (key === "z" && e.shiftKey) {
      e.preventDefault();
      redoEdit();
      updateActiveStates();
      return;
    }
    if (key === "y" && !e.shiftKey) {
      e.preventDefault();
      redoEdit();
      updateActiveStates();
      return;
    }
    if (e.shiftKey || !formatting) return;
    if (key === "b") {
      e.preventDefault();
      flushSnapshot();
      runCmd("bold");
      snapshot();
      updateActiveStates();
    } else if (key === "i") {
      e.preventDefault();
      flushSnapshot();
      runCmd("italic");
      snapshot();
      updateActiveStates();
    } else if (key === "u") {
      e.preventDefault();
      flushSnapshot();
      runCmd("underline");
      snapshot();
      updateActiveStates();
    }
  };

  // Route pasted/dropped files to onPasteFile; blocks unsafe/oversized, never embeds inline.
  const routeFiles = (files) => {
    const handler = onPasteFileRef.current;
    for (const f of files) {
      if (isUnsafeExt(f.name) || isUnsafeMime(f.type)) {
        toast(`"${f.name || f.type}" can't be attached — for safety, programs and scripts aren't allowed.`, "error");
        continue;
      }
      const tooBig = fileTooLargeMessage(f);
      if (tooBig) {
        toast(tooBig, "error");
        continue;
      }
      if (!handler) {
        toast(`There's nowhere to attach "${f.name || "this file"}" here.`, "info");
        continue;
      }
      try {
        const result = handler(f);
        if (result instanceof Promise) {
          result.catch((err) => {
            toast(`Couldn't attach "${f.name || "file"}": ${describeError(err)}`, "error");
          });
        }
      } catch (err) {
        toast(`Couldn't attach "${f.name || "file"}": ${describeError(err)}`, "error");
      }
    }
  };

  // Intercept file pastes; text/HTML paste falls through to the browser.
  const handlePaste = (e) => {
    const items = e.clipboardData ? Array.from(e.clipboardData.items) : [];
    const files = [];
    for (const it of items) {
      if (it.kind === "file") {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (!files.length) return; // no files — let the browser handle it
    e.preventDefault();
    routeFiles(files);
  };

  // Intercept file drags so the browser can't embed them inline or navigate away.
  const handleDragOver = (e) => {
    if (disabled) return;
    const types = e.dataTransfer ? Array.from(e.dataTransfer.types) : [];
    if (!types.includes("Files")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  };

  const handleDrop = (e) => {
    if (disabled) return;
    const files = e.dataTransfer ? Array.from(e.dataTransfer.files) : [];
    if (!files.length) return; // not a file drop — let the browser handle it
    e.preventDefault();
    routeFiles(files);
  };

  // Seed the surface and initial undo snapshot on mount.
  useLayoutEffect(() => {
    const editor = editorRef.current;
    const history = historyRef.current;
    if (editor) {
      editor.innerHTML = initialHtmlRef.current;
      resetHistory();
    }
    return () => {
      if (history.debounce !== null) {
        window.clearTimeout(history.debounce);
        history.debounce = null;
      }
      history.stack = [];
      history.idx = -1;
      history.restoring = false;
      savedRangeRef.current = null;
    };
  }, [resetHistory]);

  // Keep savedRangeRef current while the editor is focused.
  useEffect(() => {
    const editor = editorRef.current;
    const onSelectionChange = () => {
      if (document.activeElement === editor) captureSelection();
    };
    document.addEventListener("selectionchange", onSelectionChange);
    return () => document.removeEventListener("selectionchange", onSelectionChange);
  }, []);

  useImperativeHandle(ref, () => ({
    setHtml(html) {
      const editor = editorRef.current;
      if (!editor) return;
      editor.innerHTML = html || "";
      sync();
      resetHistory(); // wholesale replace — reset history so Ctrl+Z can't jump to old content

    },
    getHtml() {
      return computeHtml();
    },
    focus() {
      editorRef.current?.focus();
    },
  }));

  return (
    <div className="bh-rt-wrap">
      {/* Hidden native textarea (.bh-rt-native): clipped to 1×1px so automation can fill it; two-way synced with the surface. */}
      <textarea
        ref={nativeRef}
        className="bh-rt-native"
        name={name}
        id={textareaId}
        defaultValue={initialHtml}
        tabIndex={-1}
        disabled={disabled}
        onInput={handleNativeInput}
        aria-hidden="true"
      />
      <div
        ref={toolbarRef}
        className="bh-rt-toolbar"
        role="toolbar"
        aria-label="Formatting"
        hidden={!formatting}
        tabIndex={disabled ? -1 : 0}
        onKeyDown={(e) => {
          if (e.key !== "Enter" && e.key !== " ") return;
          const target = e.target instanceof Element ? e.target : null;
          if (target?.closest("button[data-cmd]")) return;
          e.preventDefault();
          toolbarRef.current?.querySelector("button[data-cmd]:not([disabled])")?.focus();
        }}
        onMouseDown={handleToolbarMouseDown}
        onClick={handleToolbarClick}
      >
        <button type="button" data-cmd="bold" title="Bold (Ctrl+B)" aria-label="Bold" disabled={disabled}>
          <b>B</b>
        </button>
        <button type="button" data-cmd="italic" title="Italic (Ctrl+I)" aria-label="Italic" disabled={disabled}>
          <i>I</i>
        </button>
        <button type="button" data-cmd="underline" title="Underline (Ctrl+U)" aria-label="Underline" disabled={disabled}>
          <u>U</u>
        </button>
        <button type="button" data-cmd="strikeThrough" title="Strikethrough" aria-label="Strikethrough" disabled={disabled}>
          <s>S</s>
        </button>
        <span className="bh-rt-divider" />
        <button type="button" data-cmd="insertUnorderedList" title="Bulleted list" aria-label="Bulleted list" disabled={disabled}>
          {"•"}
        </button>
        <button type="button" data-cmd="insertOrderedList" title="Numbered list" aria-label="Numbered list" disabled={disabled}>
          1.
        </button>
        <span className="bh-rt-divider" />
        <button type="button" data-cmd="formatBlock" data-arg="blockquote" title="Quote" aria-label="Quote" disabled={disabled}>
          {'"'}
        </button>
        <button type="button" data-cmd="formatBlock" data-arg="pre" title="Code block" aria-label="Code block" disabled={disabled}>
          {"</>"}
        </button>
        <span className="bh-rt-divider" />
        <button type="button" data-cmd="bh-image" title="Insert image" aria-label="Insert image" disabled={disabled}>
          {"🖼"}
        </button>
      </div>
      <div
        ref={editorRef}
        className="bh-rt-editor"
        role="textbox"
        contentEditable={!disabled}
        suppressContentEditableWarning
        tabIndex={disabled ? -1 : 0}
        aria-multiline="true"
        aria-label={ariaLabel || placeholder || "Rich text"}
        aria-disabled={disabled || undefined}
        data-placeholder={placeholder || undefined}
        onInput={handleInput}
        onKeyUp={handleKeyUp}
        onMouseUp={handleMouseUp}
        onFocus={handleFocus}
        onKeyDown={handleKeyDown}
        onPaste={handlePaste}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
      />
    </div>
  );
});

RichEditor.displayName = "RichEditor";

export default RichEditor;
