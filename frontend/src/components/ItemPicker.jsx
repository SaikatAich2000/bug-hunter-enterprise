// Searchable multi-select combobox for linking work items (controlled).
// excludeIds strips the current + already-linked items so self-links/duplicates are impossible.
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { api } from "../lib/api";
import { debounce } from "../lib/format";
import { itemTypeEmoji } from "../modals/bug/helpers";

const PAGE = 20;

const TYPE_TABS = [
  { key: "all", label: "All" },
  { key: "Bug", label: "Bugs" },
  { key: "Requirement", label: "Requirements" },
  { key: "Task", label: "Tasks" },
];

export default function ItemPicker({
  selected,
  onChange,
  excludeIds,
  placeholder = "Search items by title or #id…",
  disabled,
  fixedType = null,
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState(fixedType || "all");
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);

  const wrapRef = useRef(null);
  const inputRef = useRef(null);
  const listboxId = useId();

  const excludeRef = useRef(excludeIds);
  // Synced after commit (never during render) so in-flight searches read the latest list.
  useEffect(() => {
    excludeRef.current = excludeIds;
  }, [excludeIds]);
  const selectedIds = new Set(selected.map((s) => s.id));

  // Sequence counter drops stale responses.
  const seqRef = useRef(0);

  const runSearch = useCallback(async (q, type) => {
    const seq = ++seqRef.current;
    setLoading(true);
    try {
      const params = new URLSearchParams({ page_size: String(PAGE) });
      if (q.trim()) params.set("q", q.trim());
      if (type && type !== "all") params.set("item_type", type);
      const res = await api(`/bugs?${params}`);
      if (seq !== seqRef.current) return;
      const exclude = new Set(excludeRef.current);
      setResults(
        res.items
          .filter((b) => !exclude.has(b.id))
          .map((b) => ({ id: b.id, title: b.title, item_type: b.item_type, status: b.status })),
      );
      setActiveIdx(0);
    } catch {
      if (seq === seqRef.current) setResults([]);
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, []);

  // Read typeFilter at call time, not at debounce-schedule time, so a trailing
  // call can't populate the list with results from the previously selected tab.
  const typeFilterRef = useRef(typeFilter);
  useEffect(() => {
    typeFilterRef.current = typeFilter;
  }, [typeFilter]);
  // The debounced entry point is built in an effect (never during render) and kept
  // in a ref so the input handler always calls the single live instance.
  const debouncedSearchRef = useRef(null);
  useEffect(() => {
    const debounced = debounce((q) => runSearch(q, typeFilterRef.current), 200);
    debouncedSearchRef.current = debounced;
    // Cancel any pending debounced call on unmount to avoid stale state updates.
    return () => debounced.cancel();
  }, [runSearch]);

  // On open: focus the input and fetch the initial result page.
  useEffect(() => {
    if (!open) return;
    inputRef.current?.focus();
    void runSearch(query, typeFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Tab switch while open re-queries immediately, bypassing the debounce.
  useEffect(() => {
    if (open) void runSearch(query, typeFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typeFilter]);

  // Outside-click closes.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e) => {
      const t = e.target instanceof Node ? e.target : null;
      if (t && wrapRef.current?.contains(t)) return;
      setOpen(false);
    };
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  // Toggle membership — the dropdown stays open so several items can be picked.
  const toggle = (item) => {
    if (selectedIds.has(item.id)) {
      onChange(selected.filter((s) => s.id !== item.id));
    } else {
      onChange([...selected, item]);
    }
  };

  const onKeyDown = (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const item = results[activeIdx];
      if (item) toggle(item);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
    }
  };

  const renderResults = () => {
    if (loading && results.length === 0) {
      return <div className="item-picker-empty muted">Searching…</div>;
    }
    if (results.length === 0) {
      return <div className="item-picker-empty muted">No matching items</div>;
    }
    return results.map((it, i) => {
      const checked = selectedIds.has(it.id);
      return (
        <button
          type="button"
          key={it.id}
          role="option"
          aria-selected={checked}
          className={`item-picker-row${i === activeIdx ? " is-active" : ""}${checked ? " is-checked" : ""}`}
          onMouseEnter={() => setActiveIdx(i)}
          onClick={() => toggle(it)}
        >
          <span className="item-picker-check" aria-hidden="true">{checked ? "✓" : ""}</span>
          <span className="inline-type" data-type={it.item_type}>
            {itemTypeEmoji(it.item_type)}
          </span>
          <span className="item-picker-row-id">#{it.id}</span>
          <span className="item-picker-row-title" title={it.title}>
            {it.title}
          </span>
          <span className="badge" data-status={it.status}>
            {it.status}
          </span>
        </button>
      );
    });
  };

  return (
    <div className="item-picker" ref={wrapRef}>
      {selected.length > 0 && (
        <div className="item-picker-chips">
          {selected.map((it) => (
            <span key={it.id} className="item-picker-chip" title={it.title}>
              <span className="inline-type" data-type={it.item_type}>
                {itemTypeEmoji(it.item_type)}
              </span>
              <span className="item-picker-chip-id">#{it.id}</span>
              <span className="item-picker-chip-title">{it.title}</span>
              <button
                type="button"
                className="item-picker-clear"
                aria-label={`Remove #${it.id}`}
                disabled={disabled}
                onClick={() => onChange(selected.filter((s) => s.id !== it.id))}
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}

      <button
        type="button"
        className={`bh-sel-btn item-picker-trigger${disabled ? " is-disabled" : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        disabled={disabled}
        onClick={() => !disabled && setOpen((o) => !o)}
      >
        <span className="bh-sel-label bh-sel-placeholder">
          {selected.length ? `${selected.length} selected — add more…` : placeholder}
        </span>
        <span className="bh-sel-caret" aria-hidden="true">▾</span>
      </button>

      {open && (
        <div className="item-picker-pop">
          {!fixedType && <div className="item-picker-tabs" role="tablist">
            {TYPE_TABS.map((t) => (
              <button
                type="button"
                key={t.key}
                role="tab"
                aria-selected={typeFilter === t.key}
                className={`item-picker-tab${typeFilter === t.key ? " is-active" : ""}`}
                onClick={() => setTypeFilter(t.key)}
              >
                {t.label}
              </button>
            ))}
          </div>}
          <input
            ref={inputRef}
            type="search"
            className="item-picker-search"
            placeholder={placeholder}
            autoComplete="off"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              debouncedSearchRef.current?.(e.target.value);
            }}
            onKeyDown={onKeyDown}
          />
          <div className="item-picker-list" role="listbox" aria-multiselectable="true" id={listboxId}>
            {renderResults()}
          </div>
        </div>
      )}
    </div>
  );
}
