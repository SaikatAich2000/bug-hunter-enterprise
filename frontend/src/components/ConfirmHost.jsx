/** Promise-based confirm dialog; App renders <ConfirmHost/> once, confirmDialog() awaits a boolean. */
import { useEffect, useState } from "react";

let push = null;

export function confirmDialog(message, opts = {}) {
  const full = {
    title: opts.title ?? "Confirm",
    okLabel: opts.okLabel ?? "Delete",
    danger: opts.danger ?? true,
  };
  return new Promise((resolve) => {
    if (!push) {
      resolve(false);
      return;
    }
    push({ message, opts: full, resolve });
  });
}

/** Same host, plus a single text input; resolves the trimmed string, or null on cancel. */
export function promptDialog(message, opts = {}) {
  const full = {
    title: opts.title ?? "Enter a value",
    okLabel: opts.okLabel ?? "OK",
    danger: opts.danger ?? false,
    input: true,
    placeholder: opts.placeholder ?? "",
    defaultValue: opts.defaultValue ?? "",
  };
  return new Promise((resolve) => {
    if (!push) {
      resolve(null);
      return;
    }
    push({ message, opts: full, resolve, isPrompt: true });
  });
}

export default function ConfirmHost() {
  const [current, setCurrent] = useState(null);
  const [inputValue, setInputValue] = useState("");

  useEffect(() => {
    push = (p) => {
      setCurrent((prev) => {
        // second confirm cancels the first
        prev?.resolve(prev?.isPrompt ? null : false);
        return p;
      });
      setInputValue(p.opts.defaultValue ?? "");
    };
    return () => {
      push = null;
    };
  }, []);

  const settle = (value) => {
    if (current?.isPrompt) {
      current?.resolve(value ? inputValue.trim() : null);
    } else {
      current?.resolve(value);
    }
    setCurrent(null);
  };

  // capture-phase Escape beats the global modal-Escape handler
  useEffect(() => {
    if (!current) return;
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        settle(false);
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current]);

  return (
    <div className="modal" id="modalConfirm" hidden={!current}>
      <div className="modal-card sm confirm-card">
        <div className="modal-head">
          <h2 id="confirmTitle">{current?.opts.title ?? "Confirm"}</h2>
          <button
            type="button"
            className="icon-btn modal-close"
            id="confirmClose"
            aria-label="Close"
            onClick={() => settle(false)}
          >
            ✕
          </button>
        </div>
        <div className="modal-body confirm-body">
          <p id="confirmMessage">{current?.message ?? ""}</p>
          {current?.opts.input && (
            <input
              type="text"
              className="confirm-input"
              autoFocus
              value={inputValue}
              placeholder={current?.opts.placeholder}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") settle(true);
              }}
            />
          )}
        </div>
        <div className="modal-foot confirm-foot">
          <button type="button" className="btn ghost" id="confirmCancel" onClick={() => settle(false)}>
            Cancel
          </button>
          <button
            type="button"
            className={`btn ${current?.opts.danger ? "danger" : "primary"}`}
            id="confirmOk"
            onClick={() => settle(true)}
          >
            {current?.opts.okLabel ?? "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}
