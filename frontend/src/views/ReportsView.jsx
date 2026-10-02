/**
 * Pick a report type, set filters, Run, then Download XLSX (same backend engine).
 * Catalog loads once on mount; XLSX export reuses the last successful run's filters, not current UI state.
 */
import { useEffect, useRef, useState } from "react";
import { api, apiBlob } from "../lib/api";
import { hideLoader, showLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";
import BhDateInput from "../components/BhDateInput";
import BhSelect from "../components/BhSelect";
const REPORTS_DEFAULT_PRESETS = {
  last_7_days: 7,
  last_30_days: 30,
};

/** Local calendar date as "YYYY-MM-DD" (avoids toISOString()'s west-of-UTC evening shift). */
function isoDay(d) {
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

/** Only "right" is a meaningful value; everything else falls back to left. */
function colAlign(col) {
  return col.align === "right" ? "right" : "left";
}

/** Cell value  text: Yes/No for booleans, JSON for objects, truncated at 200 chars. */
/** Stable React key for a report result row, derived from the projected cells. */
function reportRowKey(columns, row) {
  return columns.map((col) => String(row?.[col.key] ?? "")).join("|");
}

function reportCellText(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  let text;
  if (typeof value === "object") {
    try {
      text = JSON.stringify(value);
    } catch {
      text = "";
    }
  } else {
    text = String(value);
  }
  if (text.length > 200) return text.slice(0, 197) + "…";
  return text;
}

/** Plain-object summary values (e.g. by_type: {"Bug": 252}) render  lists, not raw JSON. */
function isBreakdown(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Toggle a value in an array. */
function toggleVal(list, v) {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

/** Keys mirror the chip `data-name` attributes. */

const EMPTY_CHIPS = {
  item_type: [],
  status: [],
  priority: [],
  environment: [],
  project_id: [],
  assignee_id: [],
  reporter_id: [],
};

/** Matches the FilterIn schema in app/routes/reports.py. */

function ChipGroup({
  id,
  name,
  scroll,
  items,
  checked,
  onToggle,
}) {
  return (
    <div className={scroll ? "reports-chips reports-chips-scroll" : "reports-chips"} id={id}>
      {items.map((it) => (
        <label className="reports-chip" key={String(it.value)}>
          <input
            type="checkbox"
            value={String(it.value)}
            data-name={name}
            checked={checked.includes(it.value)}
            onChange={() => onToggle(it.value)}
          />
          {/* Visible only when checked — controlled by styles.css. */}
          <span className="reports-chip-indicator" aria-hidden="true">✓</span>
          <span className="reports-chip-label">{it.text}</span>
        </label>
      ))}
    </div>
  );
}

export default function ReportsView() {
  const { projects, users, setView, setSprintsDeepLinkTab } = useApp();

  // `lastRun` holds the key + filters from the most recent successful run (download disabled until set).
  const [catalog, setCatalog] = useState(null);
  const [reportKey, setReportKey] = useState("");
  const [result, setResult] = useState(null);
  const [lastRun, setLastRun] = useState(null);
  const runningRef = useRef(false);

  // Filter state.
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [chips, setChips] = useState(EMPTY_CHIPS);
  const [textSearch, setTextSearch] = useState("");
  const [includeNotABug, setIncludeNotABug] = useState(false);
  const [runLabel, setRunLabel] = useState("");

  // Apply the default date window for the selected report type.
  const applyTypeDefaults = (meta) => {
    const win = meta?.default_window || "all_time";
    const days = REPORTS_DEFAULT_PRESETS[win];
    if (days !== undefined) {
      const today = new Date();
      const from = new Date(today);
      from.setDate(from.getDate() - days);
      setDateFrom(isoDay(from));
      setDateTo(isoDay(today));
    } else {
      // all_time: clear both bounds
      setDateFrom("");
      setDateTo("");
    }
  };

  // Load catalog once; initRef guards strict-mode double-invocation.
  const initRef = useRef(false);
  useEffect(() => {
    if (initRef.current) return;
    initRef.current = true;
    (async () => {
      try {
        const cat = await api("/reports/types");
        setCatalog(cat);
        const first = cat.types[0];
        setReportKey(first ? first.key : "");
        applyTypeDefaults(first);
      } catch (err) {
        toastError(err);
      }
    })();
  }, []);

  const selectedMeta = catalog?.types.find((t) => t.key === reportKey);
  const vocab = catalog?.vocab;

  // Project/assignee/reporter IDs are validated against the live lists so
  // stale chips from deleted entities don't reach the API.
  const buildFilters = ()=> ({
    date_from: dateFrom || null,
    date_to: dateTo || null,
    item_types: chips.item_type,
    statuses: chips.status,
    priorities: chips.priority,
    environments: chips.environment,
    project_ids: chips.project_id.filter((id) => projects.some((p) => p.id === id)),
    assignee_ids: chips.assignee_id.filter((id) => users.some((u) => u.id === id)),
    reporter_ids: chips.reporter_id.filter((id) => users.some((u) => u.id === id)),
    include_not_a_bug: includeNotABug,
    text_search: textSearch.trim() || null,
    label: runLabel.trim() || null,
  });

  // Reset all filter inputs to per-type defaults.
  const resetFilters = () => {
    setChips(EMPTY_CHIPS);
    setTextSearch("");
    setRunLabel("");
    setIncludeNotABug(false);
    applyTypeDefaults(selectedMeta);
  };

  // Quick-fill date range from today back N days.
  const setDatePreset = (days) => {
    const today = new Date();
    const from = new Date(today);
    from.setDate(from.getDate() - days);
    setDateFrom(isoDay(from));
    setDateTo(isoDay(today));
  };

  // Submit the current filters and store the result + filter snapshot.
  const runReportNow = async () => {
    if (runningRef.current) return;
    if (!reportKey) return;
    const filters = buildFilters();
    runningRef.current = true;
    showLoader("Running report…");
    try {
      const res = await api("/reports/run", {
        method: "POST",
        json: { report_key: reportKey, filters },
      });
      setResult(res);
      setLastRun({ reportKey, filters });
    } catch (err) {
      toastError(err);
    } finally {
      runningRef.current = false;
      hideLoader();
    }
  };

  // Export the last run  using a programmatic anchor click.
  const downloadReportXlsx = async () => {
    if (!lastRun) {
      toast("Run a report first, then download", "info");
      return;
    }
    showLoader("Building spreadsheet…");
    try {
      const { blob, filename } = await apiBlob("/reports/export.xlsx", {
        method: "POST",
        json: { report_key: lastRun.reportKey, filters: lastRun.filters },
      });
      const fname = filename || "bug-hunter-report.xlsx";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fname;
      document.body.append(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      toastError(err);
    } finally {
      hideLoader();
    }
  };

  // Meta line shown beneath the result title.
  const metaBits = lastRun && result
    ? [
        `${result.total} row${result.total === 1 ? "" : "s"}`,
        lastRun.filters.date_from ? `from ${lastRun.filters.date_from}` : null,
        lastRun.filters.date_to ? `to ${lastRun.filters.date_to}` : null,
      ].filter(Boolean)
    : [];

  const summaryEntries = result ? Object.entries(result.summary ?? {}) : [];
  const showTruncated = !!result && result.rows.length > 0 && result.truncated;

  return (
    <section className="view" id="viewReports">
      <div className="reports-sprint-banner">
        <span>Looking for Sprint burndown, scope changes, or Daily Sprint Reports?</span>
        <button
          type="button"
          className="btn ghost btn-sm"
          onClick={() => { setSprintsDeepLinkTab("reports"); setView("sprints"); }}
        >
          View Sprint Reports →
        </button>
      </div>
      <div className="reports-shell">
        <aside className="reports-side" aria-label="Report configuration">
          <div className="reports-side-scroll">
            <div className="reports-side-section">
              <label className="field" htmlFor="reportTypeSelect">
                <span>Report type</span>
                <BhSelect
                  id="reportTypeSelect"
                  ariaLabel="Report type"
                  value={reportKey}
                  onChange={(v) => {
                    setReportKey(v);
                    // Reset date range to the new type's default window.
                    applyTypeDefaults(catalog?.types.find((t) => t.key === v));
                  }}
                  options={(catalog?.types ?? []).map((t) => ({
                    value: t.key,
                    label: `${t.icon || "📈"}  ${t.label}`,
                  }))}
                />
              </label>
              <p className="reports-help" id="reportTypeHelp">{selectedMeta?.description || ""}</p>
            </div>
            <div className="reports-side-section">
              <h3>Filters</h3>
              <div className="reports-side-row">
                <label className="field" htmlFor="reportDateFrom">
                  <span>Date from</span>
                  <BhDateInput id="reportDateFrom" value={dateFrom} onChange={setDateFrom} />
                </label>
                <label className="field" htmlFor="reportDateTo">
                  <span>Date to</span>
                  <BhDateInput id="reportDateTo" value={dateTo} onChange={setDateTo} />
                </label>
              </div>
              <div className="reports-side-row reports-side-row-presets">
                <button
                  className="btn ghost btn-sm"
                  id="reportPresetThisWeekBtn"
                  type="button"
                  onClick={() => setDatePreset(7)}
                >
                  Last 7 days
                </button>
                <button
                  className="btn ghost btn-sm"
                  id="reportPresetThisMonthBtn"
                  type="button"
                  onClick={() => setDatePreset(30)}
                >
                  Last 30 days
                </button>
              </div>
              <fieldset className="field">
                <legend>Item types</legend>
                <ChipGroup
                  id="reportItemTypes"
                  name="item_type"
                  items={(vocab?.item_types ?? []).map((v) => ({ value: v , text: String(v) }))}
                  checked={chips.item_type}
                  onToggle={(v) => setChips((c) => ({ ...c, item_type: toggleVal(c.item_type, v) }))}
                />
              </fieldset>
              <fieldset className="field">
                <legend>Statuses</legend>
                <ChipGroup
                  id="reportStatuses"
                  name="status"
                  items={(vocab?.statuses ?? []).map((v) => ({ value: v, text: v }))}
                  checked={chips.status}
                  onToggle={(v) => setChips((c) => ({ ...c, status: toggleVal(c.status, v) }))}
                />
              </fieldset>
              <fieldset className="field">
                <legend>Priorities</legend>
                <ChipGroup
                  id="reportPriorities"
                  name="priority"
                  items={(vocab?.priorities ?? []).map((v) => ({ value: v, text: v }))}
                  checked={chips.priority}
                  onToggle={(v) => setChips((c) => ({ ...c, priority: toggleVal(c.priority, v) }))}
                />
              </fieldset>
              <fieldset className="field">
                <legend>Environments</legend>
                <ChipGroup
                  id="reportEnvironments"
                  name="environment"
                  items={(vocab?.environments ?? []).map((v) => ({ value: v, text: v }))}
                  checked={chips.environment}
                  onToggle={(v) => setChips((c) => ({ ...c, environment: toggleVal(c.environment, v) }))}
                />
              </fieldset>
              <fieldset className="field">
                <legend>Projects</legend>
                <ChipGroup
                  id="reportProjects"
                  name="project_id"
                  scroll
                  items={projects.map((p) => ({ value: p.id, text: p.name }))}
                  checked={chips.project_id}
                  onToggle={(v) => setChips((c) => ({ ...c, project_id: toggleVal(c.project_id, v) }))}
                />
              </fieldset>
              <fieldset className="field">
                <legend>Assignees</legend>
                <ChipGroup
                  id="reportAssignees"
                  name="assignee_id"
                  scroll
                  items={users.map((u) => ({ value: u.id, text: u.name }))}
                  checked={chips.assignee_id}
                  onToggle={(v) => setChips((c) => ({ ...c, assignee_id: toggleVal(c.assignee_id, v) }))}
                />
              </fieldset>
              <fieldset className="field">
                <legend>Reporters</legend>
                <ChipGroup
                  id="reportReporters"
                  name="reporter_id"
                  scroll
                  items={users.map((u) => ({ value: u.id, text: u.name }))}
                  checked={chips.reporter_id}
                  onToggle={(v) => setChips((c) => ({ ...c, reporter_id: toggleVal(c.reporter_id, v) }))}
                />
              </fieldset>
              <label className="field">
                <span>Title / description contains</span>
                <input
                  id="reportTextSearch"
                  type="search"
                  maxLength={400}
                  placeholder="Free-text match…"
                  autoComplete="off"
                  value={textSearch}
                  onChange={(e) => setTextSearch(e.target.value)}
                />
              </label>
              <label className="field check-row">
                <input
                  type="checkbox"
                  id="reportIncludeNotABug"
                  checked={includeNotABug}
                  onChange={(e) => setIncludeNotABug(e.target.checked)}
                />
                <span>Include “Not a Bug” items</span>
              </label>
              <label className="field">
                <span>Run label (optional)</span>
                <input
                  id="reportRunLabel"
                  type="text"
                  maxLength={120}
                  placeholder="e.g. Q2 review pull"
                  autoComplete="off"
                  value={runLabel}
                  onChange={(e) => setRunLabel(e.target.value)}
                />
              </label>
            </div>
          </div>
          <div className="reports-side-actions">
            <button className="btn primary" id="reportRunBtn" type="button" onClick={() => { void runReportNow(); }}>
              Run report
            </button>
            <button className="btn ghost" id="reportClearBtn" type="button" onClick={resetFilters}>
              Reset filters
            </button>
          </div>
        </aside>
        <section className="reports-main" aria-label="Report results">
          <div className="reports-result-head">
            <div>
              <h3 id="reportResultTitle">{result ? result.report_label : "Choose a report to begin"}</h3>
              <p className="muted small" id="reportResultMeta">{metaBits.join(" · ")}</p>
            </div>
            <button
              className="btn primary"
              id="reportDownloadBtn"
              type="button"
              disabled={!lastRun}
              onClick={() => { void downloadReportXlsx(); }}
            >
              Download XLSX
            </button>
          </div>
          {/* Summary cards — hidden until a run completes */}
          <div className="reports-summary" id="reportSummary" hidden={!summaryEntries.length}>
            {summaryEntries.map(([k, v]) => {
              const breakdown = isBreakdown(v) ? Object.entries(v) : null;
              return (
                <div className="reports-summary-card" key={k}>
                  <div className="reports-summary-label">{k.replaceAll("_", " ")}</div>
                  {breakdown?.length === 0 ? (
                    <div className="reports-summary-value">—</div>
                  ) : breakdown ? (
                    <ul className="reports-summary-breakdown">
                      {breakdown.map(([sk, sv]) => (
                        <li key={sk}>
                          <span className="reports-summary-bd-key">{sk}</span>
                          <span className="reports-summary-bd-val">{reportCellText(sv)}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <div className="reports-summary-value">{reportCellText(v)}</div>
                  )}
                </div>
              );
            })}
          </div>
          {/* Result table */}
          <div className="reports-table-scroll">
            <table className="bug-table reports-table" id="reportTable" aria-label="Report results">
              <thead id="reportTableHead">
                {result ? (
                  <tr>
                    {result.columns.map((col) => (
                      <th key={col.key} scope="col" style={{ textAlign: colAlign(col) }}>
                        {col.label}
                      </th>
                    ))}
                  </tr>
                ) : (
                  <tr><th scope="col">Run a report to populate this table</th></tr>
                )}
              </thead>
              <tbody id="reportTableBody">
                {(result?.rows ?? []).map((row) => (
                  <tr key={reportRowKey(result.columns, row)}>
                    {result.columns.map((col) => (
                      <td key={col.key} style={{ textAlign: colAlign(col) }}>
                        {reportCellText(row[col.key])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="reports-empty" id="reportEmpty" hidden={!result || result.rows.length > 0}>
            <p>No rows match your filters. Try widening the date range or removing a filter</p>
          </div>
          <div className="reports-truncated" id="reportTruncated" hidden={!showTruncated}>
            <p>
              Showing the first{" "}
              <span id="reportTruncatedCount">
                {result ? String(result.truncated_cap || result.rows.length) : "1000"}
              </span>{" "}
              rows. Download the XLSX for the full set
            </p>
          </div>
        </section>
      </div>
    </section>
  );
}
