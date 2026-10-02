/** Board settings (Jira's board configuration): general, columns & statuses,
 * quick filters. */
import { useEffect, useMemo, useState } from "react";
import { useApp } from "../../state/AppContext";
import BhSelect from "../../components/BhSelect";
import ChipPicker from "../../components/ChipPicker";
import { confirmDialog } from "../../components/ConfirmHost";
import { toast, toastError } from "../../lib/toast";
import { ALL_ITEM_TYPES, itemTypeLabel } from "../../lib/itemTypes";
import { agileApi, notifyAgileChanged } from "./agileApi";
import { useLatestLoad } from "./useAgileProject";
import { SWIMLANE_MODES } from "./logic/board";
import { STATISTIC_LABELS } from "./logic/estimates";

const WEEKDAYS = [[1, "Mon"], [2, "Tue"], [3, "Wed"], [4, "Thu"], [5, "Fri"], [6, "Sat"], [7, "Sun"]];
const CATEGORIES = [
  { value: "todo", label: "To do" },
  { value: "in_progress", label: "In progress" },
  { value: "testing", label: "Testing" },
  { value: "done", label: "Done" },
];
const ENFORCEMENT = [
  { value: "off", label: "Show only" },
  { value: "warn", label: "Warn before exceeding" },
  { value: "block", label: "Block exceeding" },
];

/** Problems that would make the server refuse the column set, or leave issues stuck. */
function columnProblems(columns) {
  const problems = [];
  if (!columns.length) problems.push("A board needs at least one column.");
  const names = new Set();
  for (const col of columns) {
    const key = col.name.trim().toLowerCase();
    if (!key) problems.push("Every column needs a name.");
    else if (names.has(key)) problems.push(`Two columns are named "${col.name.trim()}".`);
    names.add(key);
    if (col.wip_limit != null && col.min_cards != null && col.min_cards > col.wip_limit) {
      problems.push(`"${col.name}": the minimum is above the maximum.`);
    }
  }
  const last = columns[columns.length - 1];
  if (last && last.category !== "done") problems.push("The right-most column must have the Done category: issues there count as finished.");
  if (last && !last.statuses.length) problems.push("The right-most column needs at least one status.");
  return problems;
}

function numberOrNull(value) {
  if (value === "" || value == null) return null;
  const n = Number.parseInt(value, 10);
  return Number.isFinite(n) && n > 0 ? n : null;
}

function GeneralSettings({ board, onSaved }) {
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  // Re-seed only when these settings change on the server, so a refresh from
  // another section's save (or any agile change) keeps unsaved edits here.
  const savedKey = JSON.stringify([board.id, board.name, board.estimation_mode, board.swimlane_mode,
    board.timezone, board.working_weekdays]);
  useEffect(() => {
    const [, name, estimationMode, swimlaneMode, timezone, weekdays] = JSON.parse(savedKey);
    setForm({
      name, estimation_mode: estimationMode, swimlane_mode: swimlaneMode,
      timezone, working_weekdays: weekdays || [1, 2, 3, 4, 5],
    });
  }, [savedKey]);
  if (!form) return null;
  const toggleDay = (day) => setForm((f) => ({
    ...f,
    working_weekdays: f.working_weekdays.includes(day)
      ? f.working_weekdays.filter((d) => d !== day) : [...f.working_weekdays, day].sort((a, b) => a - b),
  }));
  const save = async (e) => {
    e.preventDefault();
    if (!form.working_weekdays.length) {
      toast("Pick at least one working day", "error");
      return;
    }
    setBusy(true);
    try {
      await agileApi.updateBoard(board.id, { ...form, name: form.name.trim(), version: board.version });
      toast("Board settings saved", "success");
      onSaved();
      notifyAgileChanged();
    } catch (err) {
      toastError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="ag-settings-card" onSubmit={save} aria-labelledby="ag-general-title">
      <h3 id="ag-general-title">General</h3>
      <label className="field"><span>Board name</span>
        <input value={form.name} maxLength={120} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
      </label>
      <div className="field"><span>Estimation statistic</span>
        <BhSelect ariaLabel="Estimation statistic" value={form.estimation_mode}
          onChange={(v) => setForm({ ...form, estimation_mode: v })}
          options={Object.entries(STATISTIC_LABELS).map(([value, label]) => ({ value, label }))} />
        <small className="muted">Used by the backlog totals, the sprint dialogs and every report.</small>
      </div>
      <div className="field"><span>Default swimlanes</span>
        <BhSelect ariaLabel="Default swimlanes" value={form.swimlane_mode}
          onChange={(v) => setForm({ ...form, swimlane_mode: v })} options={SWIMLANE_MODES} />
      </div>
      <fieldset className="field ag-days">
        <legend>Working days <small className="muted">(the burndown guideline stays flat on the others)</small></legend>
        {WEEKDAYS.map(([day, label]) => (
          <label key={day} className="ag-check">
            <input type="checkbox" checked={form.working_weekdays.includes(day)} onChange={() => toggleDay(day)} /> {label}
          </label>
        ))}
      </fieldset>
      <label className="field"><span>Timezone (IANA name, e.g. Asia/Kolkata)</span>
        <input value={form.timezone} maxLength={64} onChange={(e) => setForm({ ...form, timezone: e.target.value })} />
        <small className="muted">Sprint dates and report days use this timezone.</small>
      </label>
      <div className="ag-dialog-actions">
        <button type="submit" className="btn primary" disabled={busy}>Save general settings</button>
      </div>
    </form>
  );
}

function ColumnsSettings({ board, onSaved }) {
  const { meta } = useApp();
  const [columns, setColumns] = useState([]);
  const [busy, setBusy] = useState(false);
  const allStatuses = meta.statuses || [];

  // Re-seed only when the columns change on the server (not when another
  // section saves), so unsaved column edits survive.
  const savedColumns = JSON.stringify((board.columns || []).map((c) => ({
    id: c.id, name: c.name, category: c.category, statuses: [...c.statuses],
    wip_limit: c.wip_limit, min_cards: c.min_cards, wip_enforcement: c.wip_enforcement || "off", color: c.color || "",
  })));
  useEffect(() => {
    setColumns(JSON.parse(savedColumns));
  }, [savedColumns]);

  const mapped = new Set(columns.flatMap((c) => c.statuses));
  const unmapped = allStatuses.filter((s) => !mapped.has(s));
  const problems = columnProblems(columns);
  const update = (index, patch) => setColumns((cols) => cols.map((c, n) => (n === index ? { ...c, ...patch } : c)));
  const moveStatus = (status, toIndex) => setColumns((cols) => cols.map((c, n) => ({
    ...c,
    statuses: n === toIndex
      ? [...c.statuses.filter((s) => s !== status), status]
      : c.statuses.filter((s) => s !== status),
  })));
  const unmap = (status) => setColumns((cols) => cols.map((c) => ({ ...c, statuses: c.statuses.filter((s) => s !== status) })));
  const shift = (index, delta) => setColumns((cols) => {
    const next = [...cols];
    const target = index + delta;
    if (target < 0 || target >= next.length) return cols;
    [next[index], next[target]] = [next[target], next[index]];
    return next;
  });
  const remove = async (index) => {
    const col = columns[index];
    const ok = await confirmDialog(
      `Remove the "${col.name}" column? Its statuses become unmapped until you place them in another column.`,
      { title: "Remove column", okLabel: "Remove", danger: true },
    );
    if (ok) setColumns((cols) => cols.filter((_, n) => n !== index));
  };
  const add = () => setColumns((cols) => {
    const at = Math.max(0, cols.length - 1);
    const next = [...cols];
    next.splice(at, 0, { id: null, name: "New column", category: "in_progress", statuses: [], wip_limit: null, min_cards: null, wip_enforcement: "off", color: "" });
    return next;
  });

  const save = async () => {
    if (problems.length) return;
    setBusy(true);
    try {
      await agileApi.saveColumns(board.id, {
        version: board.version,
        columns: columns.map((c) => ({
          id: c.id ?? undefined, name: c.name.trim(), category: c.category, statuses: c.statuses,
          wip_limit: c.wip_limit, min_cards: c.min_cards, wip_enforcement: c.wip_enforcement, color: c.color,
        })),
      });
      toast("Columns saved", "success");
      onSaved();
      notifyAgileChanged();
    } catch (err) {
      toastError(err);
    } finally {
      setBusy(false);
    }
  };

  const columnOptions = columns.map((c, n) => ({ value: String(n), label: c.name || `Column ${n + 1}` }));

  return (
    <section className="ag-settings-card" aria-labelledby="ag-columns-title">
      <h3 id="ag-columns-title">Columns and statuses</h3>
      <p className="muted small">
        Each status shows in one column. Issues whose status is in the right-most column are done:
        that decides sprint completion and every report. Min/max set the column constraints (WIP).
      </p>
      <div className="ag-col-editor">
        {columns.map((col, index) => (
          <div key={col.id ?? `new-${index}`} className="ag-col-edit" data-category={col.category}>
            <div className="ag-col-edit-head">
              <input aria-label={`Column ${index + 1} name`} value={col.name} maxLength={80}
                onChange={(e) => update(index, { name: e.target.value })} />
              <span className="ag-col-edit-moves">
                <button type="button" className="btn ghost btn-sm" aria-label={`Move ${col.name} left`}
                  disabled={index === 0} onClick={() => shift(index, -1)}>◀</button>
                <button type="button" className="btn ghost btn-sm" aria-label={`Move ${col.name} right`}
                  disabled={index === columns.length - 1} onClick={() => shift(index, 1)}>▶</button>
                <button type="button" className="btn ghost btn-sm" aria-label={`Remove ${col.name}`}
                  disabled={columns.length === 1} onClick={() => remove(index)}>✕</button>
              </span>
            </div>
            <div className="field"><span>Category</span>
              <BhSelect ariaLabel={`${col.name} category`} value={col.category} options={CATEGORIES}
                onChange={(v) => update(index, { category: v })} />
            </div>
            <div className="ag-form-row">
              <label className="field"><span>Min</span>
                <input type="number" min="1" value={col.min_cards ?? ""} aria-label={`${col.name} minimum`}
                  onChange={(e) => update(index, { min_cards: numberOrNull(e.target.value) })} />
              </label>
              <label className="field"><span>Max</span>
                <input type="number" min="1" value={col.wip_limit ?? ""} aria-label={`${col.name} maximum`}
                  onChange={(e) => update(index, { wip_limit: numberOrNull(e.target.value) })} />
              </label>
            </div>
            {col.wip_limit != null && (
              <div className="field"><span>When over the max</span>
                <BhSelect ariaLabel={`${col.name} limit enforcement`} value={col.wip_enforcement} options={ENFORCEMENT}
                  onChange={(v) => update(index, { wip_enforcement: v })} />
              </div>
            )}
            <ul className="ag-status-list" aria-label={`Statuses in ${col.name}`}>
              {col.statuses.map((status) => (
                <li key={status} className="ag-status-chip">
                  <span>{status}</span>
                  <BhSelect ariaLabel={`Move ${status} to column`} value={String(index)} options={columnOptions}
                    onChange={(v) => moveStatus(status, Number(v))} />
                  <button type="button" className="ag-link" aria-label={`Unmap ${status}`} onClick={() => unmap(status)}>✕</button>
                </li>
              ))}
              {!col.statuses.length && <li className="muted small">No statuses: cards cannot enter this column.</li>}
            </ul>
          </div>
        ))}
        <div className="ag-col-edit ag-col-unmapped">
          <div className="ag-col-edit-head"><strong>Unmapped statuses</strong></div>
          <p className="muted small">Issues with these statuses are not shown on the board.</p>
          <ul className="ag-status-list">
            {unmapped.map((status) => (
              <li key={status} className="ag-status-chip">
                <span>{status}</span>
                <BhSelect ariaLabel={`Add ${status} to column`} value="" options={[{ value: "", label: "Add to…" }, ...columnOptions]}
                  onChange={(v) => v !== "" && moveStatus(status, Number(v))} />
              </li>
            ))}
            {!unmapped.length && <li className="muted small">Every status is on the board.</li>}
          </ul>
        </div>
      </div>
      {problems.length > 0 && (
        <ul className="ag-error" role="alert">{problems.map((p) => <li key={p}>{p}</li>)}</ul>
      )}
      <div className="ag-dialog-actions">
        <button type="button" className="btn ghost" onClick={add}>+ Add column</button>
        <button type="button" className="btn primary" disabled={busy || problems.length > 0} onClick={save}>Save columns</button>
      </div>
    </section>
  );
}

const EMPTY_FILTER = { name: "", item_types: [], priorities: [], assignee_ids: [], epic_ids: [], label_ids: [], flagged: false, text: "" };

/** Human summary of a quick filter's criteria. */
function describeQuickFilter(spec, { users = [], epics = [], labels = [] } = {}) {
  const parts = [];
  const names = (ids, list, key = "name") => ids.map((id) => list.find((x) => x.id === id)?.[key] ?? `#${id}`).join(", ");
  if (spec.item_types?.length) parts.push(`type: ${spec.item_types.join(", ")}`);
  if (spec.priorities?.length) parts.push(`priority: ${spec.priorities.join(", ")}`);
  if (spec.assignee_ids?.length) parts.push(`assignee: ${names(spec.assignee_ids, users)}`);
  if (spec.epic_ids?.length) parts.push(`epic: ${names(spec.epic_ids, epics, "title")}`);
  if (spec.label_ids?.length) parts.push(`label: ${names(spec.label_ids, labels, "display_name")}`);
  if (spec.flagged) parts.push("flagged");
  if (spec.text) parts.push(`text: "${spec.text}"`);
  return parts.join(" · ") || "everything";
}

function QuickFilterSettings({ board }) {
  const { users, meta } = useApp();
  const list = useLatestLoad(() => agileApi.quickFilters(board.id), [board.id]);
  const extras = useLatestLoad(async () => {
    const [epics, labels] = await Promise.all([agileApi.epics(board.project_id), agileApi.labels(board.project_id)]);
    return { epics, labels };
  }, [board.project_id]);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const epics = extras.data?.epics || [];
  const labels = extras.data?.labels || [];
  const people = (users || []).filter((u) => u.is_active);
  const toggle = (key, value) => setDraft((d) => ({
    ...d, [key]: d[key].includes(value) ? d[key].filter((v) => v !== value) : [...d[key], value],
  }));
  const save = async (e) => {
    e.preventDefault();
    const { id, version, name, ...spec } = draft;
    setBusy(true);
    try {
      if (id) await agileApi.updateQuickFilter(id, { name: name.trim(), filter_json: spec, version });
      else await agileApi.createQuickFilter(board.id, { name: name.trim(), filter_json: spec, is_shared: true });
      toast("Quick filter saved", "success");
      setDraft(null);
      notifyAgileChanged();
    } catch (err) {
      toastError(err);
    } finally {
      setBusy(false);
    }
  };
  const remove = async (qf) => {
    const ok = await confirmDialog(`Delete the quick filter "${qf.name}"?`, { title: "Delete quick filter", okLabel: "Delete", danger: true });
    if (!ok) return;
    try {
      await agileApi.deleteQuickFilter(qf.id);
      notifyAgileChanged();
    } catch (err) {
      toastError(err);
    }
  };
  const lookups = { users: people, epics, labels };
  return (
    <section className="ag-settings-card" aria-labelledby="ag-qf-title">
      <h3 id="ag-qf-title">Quick filters</h3>
      <p className="muted small">Shown above the board next to “Only my issues” and “Recently updated”. Active filters combine.</p>
      <ul className="ag-qf-list">
        {(list.data || []).map((qf) => (
          <li key={qf.id}>
            <strong>{qf.name}</strong> <span className="muted small">{describeQuickFilter(qf.filter_json || {}, lookups)}</span>
            <span className="ag-spacer" />
            <button type="button" className="btn ghost btn-sm" onClick={() => setDraft({ ...EMPTY_FILTER, ...(qf.filter_json || {}), id: qf.id, version: qf.version, name: qf.name })}>Edit</button>
            <button type="button" className="btn ghost btn-sm" onClick={() => remove(qf)}>Delete</button>
          </li>
        ))}
        {!list.data?.length && <li className="muted small">No custom quick filters yet.</li>}
      </ul>
      {draft ? (
        <form className="ag-qf-form" onSubmit={save}>
          <label className="field"><span>Name <em>*</em></span>
            <input value={draft.name} maxLength={120} required onChange={(e) => setDraft({ ...draft, name: e.target.value })} autoFocus />
          </label>
          <div className="field"><span>Types</span>
            <ChipPicker items={ALL_ITEM_TYPES.map((t) => ({ id: t, label: itemTypeLabel(t) }))} selected={draft.item_types}
              onToggle={(v) => toggle("item_types", v)} />
          </div>
          <div className="field"><span>Priorities</span>
            <ChipPicker items={(meta.priorities || []).map((p) => ({ id: p, label: p }))} selected={draft.priorities}
              onToggle={(v) => toggle("priorities", v)} />
          </div>
          <div className="field"><span>Assignees</span>
            <ChipPicker items={people.map((u) => ({ id: u.id, label: u.name }))} selected={draft.assignee_ids}
              onToggle={(v) => toggle("assignee_ids", v)} />
          </div>
          {epics.length > 0 && (
            <div className="field"><span>Epics</span>
              <ChipPicker items={epics.map((e) => ({ id: e.id, label: e.title }))} selected={draft.epic_ids}
                onToggle={(v) => toggle("epic_ids", v)} />
            </div>
          )}
          {labels.length > 0 && (
            <div className="field"><span>Labels</span>
              <ChipPicker items={labels.map((l) => ({ id: l.id, label: l.display_name }))} selected={draft.label_ids}
                onToggle={(v) => toggle("label_ids", v)} />
            </div>
          )}
          <label className="ag-check"><input type="checkbox" checked={draft.flagged}
            onChange={(e) => setDraft({ ...draft, flagged: e.target.checked })} /> Flagged only</label>
          <label className="field"><span>Text in key or summary</span>
            <input value={draft.text} maxLength={120} onChange={(e) => setDraft({ ...draft, text: e.target.value })} />
          </label>
          <div className="ag-dialog-actions">
            <button type="button" className="btn ghost" onClick={() => setDraft(null)}>Cancel</button>
            <button type="submit" className="btn primary" disabled={busy || !draft.name.trim()}>Save quick filter</button>
          </div>
        </form>
      ) : (
        <button type="button" className="btn ghost btn-sm" onClick={() => setDraft({ ...EMPTY_FILTER })}>+ Add quick filter</button>
      )}
    </section>
  );
}

export default function BoardSettingsPanel({ board, onSaved }) {
  const current = useMemo(() => board, [board]);
  return (
    <div className="ag-settings">
      <GeneralSettings board={current} onSaved={onSaved} />
      <ColumnsSettings board={current} onSaved={onSaved} />
      <QuickFilterSettings board={current} />
    </div>
  );
}
