/** Epics: the project's hierarchy, Epic → Story/Task/Bug/Requirement → Sub-task,
 * with each Epic's progress. */
import { useEffect, useMemo, useState } from "react";
import { useApp } from "../../state/AppContext";
import Modal from "../../components/Modal";
import BhDateInput from "../../components/BhDateInput";
import { confirmDialog } from "../../components/ConfirmHost";
import { api } from "../../lib/api";
import { toast, toastError } from "../../lib/toast";
import { agileApi, notifyAgileChanged } from "./agileApi";
import { useLatestLoad } from "./useAgileProject";
import ActionMenu from "./ActionMenu";
import {
  EstimateBadge, IssueKey, StatusLozenge, SubtaskProgress, TypeIcon,
} from "./IssueBits";
import { AssigneeStack, epicColor } from "./agileShared";
import { formatNumber, statisticUnit } from "./logic/estimates";

const EPIC_COLORS = ["#7e57c2", "#2e7d32", "#1565c0", "#ad1457", "#ef6c00", "#00838f", "#6d4c41", "#5c6bc0"];

/** Done / in progress / to do bar from an epic's progress counts. */
function progressParts(progress) {
  const total = progress?.child_count || 0;
  if (!total) return { done: 0, inProgress: 0, todo: 0, total: 0 };
  const done = progress.completed_child_count || 0;
  const inProgress = progress.in_progress_child_count || 0;
  return { done, inProgress, todo: Math.max(0, total - done - inProgress), total };
}

function ProgressBar({ progress }) {
  const parts = progressParts(progress);
  if (!parts.total) return <span className="muted small">No issues yet</span>;
  const pct = (n) => `${(n / parts.total) * 100}%`;
  return (
    <span className="ag-progress" role="img"
      aria-label={`${parts.done} done, ${parts.inProgress} in progress, ${parts.todo} to do`}
      title={`${parts.done} done · ${parts.inProgress} in progress · ${parts.todo} to do`}>
      <span className="ag-progress-done" style={{ width: pct(parts.done) }} />
      <span className="ag-progress-doing" style={{ width: pct(parts.inProgress) }} />
    </span>
  );
}

function EpicDialog({ epic, onClose }) {
  const [title, setTitle] = useState("");
  const [color, setColor] = useState("");
  const [start, setStart] = useState("");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!epic) return;
    setTitle(epic.title);
    setColor(epic.color || "");
    setStart(epic.start_date || "");
    setTarget(epic.target_date || "");
  }, [epic]);
  if (!epic) return null;
  const error = start && target && target < start ? "The target date cannot be before the start date." : null;
  const submit = async (e) => {
    e.preventDefault();
    if (error) return;
    setBusy(true);
    try {
      await agileApi.updateEpic(epic.id, {
        title: title.trim(), color, start_date: start || null, target_date: target || null, version: epic.version,
      });
      toast("Epic updated", "success");
      notifyAgileChanged();
      onClose();
    } catch (err) {
      toastError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal id="epicDialog" open={Boolean(epic)} title={`Epic details: ${epic.display_id}`} onClose={onClose}>
      <form className="ag-dialog" onSubmit={submit}>
        <label className="field">
          <span>Epic name <em>*</em></span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={120} required data-autofocus />
        </label>
        <fieldset className="field ag-colors">
          <legend>Colour</legend>
          {EPIC_COLORS.map((c) => (
            <button key={c} type="button" className={`ag-swatch${color === c ? " active" : ""}`}
              style={{ background: c }} aria-label={`Colour ${c}`} aria-pressed={color === c} onClick={() => setColor(c)} />
          ))}
        </fieldset>
        <div className="ag-form-row">
          <div className="field"><span>Start date</span><BhDateInput ariaLabel="Start date" value={start} onChange={setStart} /></div>
          <div className="field"><span>Target date</span><BhDateInput ariaLabel="Target date" value={target} onChange={setTarget} /></div>
        </div>
        {error && <p className="ag-error" role="alert">{error}</p>}
        <div className="ag-dialog-actions">
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn primary" disabled={busy || !!error || !title.trim()}>Save</button>
        </div>
      </form>
    </Modal>
  );
}

export default function HierarchyPanel({ projectId, canManage, board }) {
  const { openBugDetail, openBugForm } = useApp();
  const mode = board.estimation_mode;
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [showDone, setShowDone] = useState(true);
  const [open, setOpen] = useState({});
  const [editing, setEditing] = useState(null);

  useEffect(() => {
    const t = setTimeout(() => setSearch(query.trim()), 300);
    return () => clearTimeout(t);
  }, [query]);

  const tree = useLatestLoad(() => {
    const params = new URLSearchParams({ include_done: String(showDone) });
    if (search) params.set("q", search);
    return agileApi.projectHierarchy(projectId, `?${params}`);
  }, [projectId, search, showDone]);

  const data = tree.data;
  const epicsById = useMemo(() => new Map((data?.epics || []).map((e) => [e.id, e])), [data]);
  const sprintsById = useMemo(() => new Map((data?.sprints || []).map((s) => [s.id, s])), [data]);
  const issuesByEpic = useMemo(() => {
    const map = new Map();
    for (const issue of data?.issues || []) {
      const key = issue.epic_id ?? "none";
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(issue);
    }
    return map;
  }, [data]);
  // An archived Epic stays out of the way unless issues are still linked to
  // it: those must remain reachable, under their Epic marked "Archived".
  const epics = useMemo(
    () => (data?.epics || []).filter((e) => !e.archived || (issuesByEpic.get(e.id) || []).length > 0),
    [data, issuesByEpic],
  );
  const subtasksByParent = useMemo(() => {
    const map = new Map();
    for (const sub of data?.subtasks || []) {
      if (!map.has(sub.parent_id)) map.set(sub.parent_id, []);
      map.get(sub.parent_id).push(sub);
    }
    return map;
  }, [data]);

  const relink = async (issue, epicId) => {
    try {
      await agileApi.hierarchy(issue.id, { epic_id: epicId, version: issue.version });
      toast(epicId ? `${issue.display_id} moved to ${epicsById.get(epicId)?.title}` : `${issue.display_id} removed from its epic`, "success");
      notifyAgileChanged();
    } catch (err) {
      toastError(err);
    }
  };

  const archive = async (epic) => {
    const ok = await confirmDialog(
      `Archive ${epic.title}? It is hidden from planning; its issues keep their link and history.`,
      { title: "Archive epic", okLabel: "Archive", danger: true },
    );
    if (!ok) return;
    try {
      await api(`/agile/epics/${epic.id}/archive`, { method: "POST" });
      toast("Epic archived", "success");
      notifyAgileChanged();
    } catch (err) {
      toastError(err);
    }
  };

  const toggle = (key) => setOpen((o) => ({ ...o, [key]: !o[key] }));

  const renderIssue = (issue) => {
    const subs = subtasksByParent.get(issue.id) || [];
    const key = `i${issue.id}`;
    const sprint = issue.sprint_id ? sprintsById.get(issue.sprint_id) : null;
    const moveItems = issue.can_edit ? [
      { separator: true },
      // An archived Epic takes no new issues.
      ...epics.filter((e) => e.id !== issue.epic_id && !e.archived)
        .map((e) => ({ label: `Move to epic: ${e.title}`, onSelect: () => relink(issue, e.id) })),
      issue.epic_id != null && { label: "Remove from epic", onSelect: () => relink(issue, null) },
    ] : [];
    return (
      <li key={issue.id} className="ag-tree-node">
        <div className="ag-tree-row">
          {subs.length ? (
            <button type="button" className="ag-collapse" aria-expanded={Boolean(open[key])}
              aria-label={`${open[key] ? "Hide" : "Show"} sub-tasks of ${issue.display_id}`} onClick={() => toggle(key)}>
              {open[key] ? "▾" : "▸"}
            </button>
          ) : <span className="ag-collapse-spacer" />}
          <TypeIcon type={issue.item_type} />
          <IssueKey issue={issue} onOpen={openBugDetail} />
          <button type="button" className="ag-row-title" onClick={() => openBugDetail(issue.id)}>{issue.title}</button>
          <SubtaskProgress issue={issue} />
          {sprint && <span className={`ag-sprint-tag ag-state-${sprint.state}`} title={`In ${sprint.name}`}>{sprint.name}</span>}
          <StatusLozenge status={issue.status} category={issue.status_category} />
          <AssigneeStack assignees={issue.assignees} />
          <EstimateBadge issue={issue} mode={mode} />
          <ActionMenu label={`Actions for ${issue.display_id}`} items={[
            { label: "Open issue", onSelect: () => openBugDetail(issue.id) },
            { label: "Create sub-task", onSelect: () => openBugForm({ defaultProjectId: projectId, defaultType: "Sub-task", defaultParentId: issue.id }) },
            ...moveItems,
          ]} />
        </div>
        {open[key] && subs.length > 0 && (
          <ul className="ag-tree-children">
            {subs.map((sub) => (
              <li key={sub.id} className="ag-tree-node">
                <div className="ag-tree-row">
                  <span className="ag-collapse-spacer" />
                  <TypeIcon type={sub.item_type} />
                  <IssueKey issue={sub} onOpen={openBugDetail} />
                  <button type="button" className="ag-row-title" onClick={() => openBugDetail(sub.id)}>{sub.title}</button>
                  <StatusLozenge status={sub.status} category={sub.status_category} />
                  <AssigneeStack assignees={sub.assignees} />
                </div>
              </li>
            ))}
          </ul>
        )}
      </li>
    );
  };

  if (!data) return tree.loading ? <div className="sprints-loading">Loading…</div> : null;
  const unit = statisticUnit(mode);
  const loose = issuesByEpic.get("none") || [];

  return (
    <div className="ag-hierarchy">
      <div className="ag-filterbar" role="search">
        <input className="ag-search" type="search" placeholder="Search issues by key or summary"
          aria-label="Search issues" value={query} onChange={(e) => setQuery(e.target.value)} />
        <label className="ag-check">
          <input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> Show done issues
        </label>
        <span className="ag-spacer" />
        <span className="muted small">Epic → Story / Task / Bug / Requirement → Sub-task</span>
        <button type="button" className="btn primary btn-sm"
          onClick={() => openBugForm({ defaultProjectId: projectId, defaultType: "Epic" })}>+ Create epic</button>
      </div>

      {epics.length === 0 && !search && (
        <p className="muted ag-note">No epics yet. Epics group related stories, tasks, bugs and requirements across sprints.</p>
      )}

      <ul className="ag-tree">
        {epics.map((epic) => {
          const key = `e${epic.id}`;
          const children = issuesByEpic.get(epic.id) || [];
          const expanded = open[key] ?? false;
          return (
            <li key={epic.id} className="ag-epic-node" style={{ "--epic-color": epicColor(epic.id, epicsById) }}>
              <div className="ag-epic-row">
                <button type="button" className="ag-collapse" aria-expanded={expanded}
                  aria-label={`${expanded ? "Collapse" : "Expand"} ${epic.title}`} onClick={() => toggle(key)}>
                  {expanded ? "▾" : "▸"}
                </button>
                <TypeIcon type="Epic" />
                <IssueKey issue={epic} onOpen={openBugDetail} />
                <button type="button" className="ag-row-title ag-epic-title" onClick={() => toggle(key)}>{epic.title}</button>
                <StatusLozenge status={epic.status} category={epic.status_category} />
                {epic.archived && <span className="ag-lozenge ag-cat-todo" title="Archived epic">Archived</span>}
                <ProgressBar progress={epic.progress} />
                <span className="muted small">
                  {epic.progress.completed_child_count}/{epic.progress.child_count} issues
                  {mode !== "item_count" && ` · ${formatNumber(epic.progress.completed_estimate)}/${formatNumber(epic.progress.total_estimate)} ${unit}`}
                </span>
                {(epic.start_date || epic.target_date) && (
                  <span className="muted small">{epic.start_date || "?"} → {epic.target_date || "?"}</span>
                )}
                <ActionMenu label={`Actions for ${epic.display_id}`} items={[
                  { label: "Open epic", onSelect: () => openBugDetail(epic.id) },
                  !epic.archived && { label: "Create issue in epic", onSelect: () => openBugForm({ defaultProjectId: projectId, defaultEpicId: epic.id }) },
                  canManage && { label: "Edit details (colour, dates)", onSelect: () => setEditing(epic) },
                  canManage && !epic.archived && { separator: true },
                  canManage && !epic.archived && { label: "Archive epic", danger: true, onSelect: () => archive(epic) },
                ]} />
              </div>
              {expanded && (
                <ul className="ag-tree-children">
                  {children.length ? children.map(renderIssue) : <li className="muted small ag-tree-empty">No issues in this epic.</li>}
                </ul>
              )}
            </li>
          );
        })}
        <li className="ag-epic-node ag-epic-none">
          <div className="ag-epic-row">
            <button type="button" className="ag-collapse" aria-expanded={Boolean(open.none)}
              aria-label={`${open.none ? "Collapse" : "Expand"} issues without epic`} onClick={() => toggle("none")}>
              {open.none ? "▾" : "▸"}
            </button>
            <span className="ag-row-title ag-epic-title">Issues without epic</span>
            <span className="muted small">{data.unparented_total} issue{data.unparented_total === 1 ? "" : "s"}</span>
          </div>
          {open.none && (
            <ul className="ag-tree-children">
              {loose.map(renderIssue)}
              {data.unparented_total > loose.length && (
                <li className="muted small ag-tree-empty">
                  Showing {loose.length} of {data.unparented_total}. Search to find the others.
                </li>
              )}
              {!loose.length && <li className="muted small ag-tree-empty">None.</li>}
            </ul>
          )}
        </li>
      </ul>
      <EpicDialog epic={editing} onClose={() => setEditing(null)} />
    </div>
  );
}
