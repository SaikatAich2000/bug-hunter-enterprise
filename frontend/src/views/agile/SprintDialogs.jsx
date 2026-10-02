/** Jira's sprint dialogs: create/edit, start and complete. */
import { useEffect, useMemo, useState } from "react";
import Modal from "../../components/Modal";
import BhSelect from "../../components/BhSelect";
import BhDateInput from "../../components/BhDateInput";
import { toast, toastError } from "../../lib/toast";
import { agileApi, notifyAgileChanged } from "./agileApi";
import { localIsoDate, localToday } from "./agileShared";
import { categoryTotals, formatNumber, statisticUnit } from "./logic/estimates";

const DURATIONS = [
  { value: "1", label: "1 week" },
  { value: "2", label: "2 weeks" },
  { value: "3", label: "3 weeks" },
  { value: "4", label: "4 weeks" },
  { value: "custom", label: "Custom" },
];

/** Inclusive end date ``weeks`` weeks after ``start`` (a 2-week sprint from Mon ends Sun+7). */
function endForDuration(start, weeks) {
  if (!start || weeks === "custom") return "";
  const d = new Date(`${start}T00:00:00`);
  if (Number.isNaN(d.getTime())) return "";
  d.setDate(d.getDate() + Number(weeks) * 7 - 1);
  return localIsoDate(d);
}

function durationOf(start, end) {
  if (!start || !end) return "2";
  for (const weeks of ["1", "2", "3", "4"]) {
    if (endForDuration(start, weeks) === end) return weeks;
  }
  return "custom";
}

function DateFields({ start, end, duration, onChange }) {
  return (
    <>
      <div className="field">
        <span id="sprintDurationLabel">Duration</span>
        <BhSelect
          ariaLabel="Duration"
          value={duration}
          options={DURATIONS}
          onChange={(v) => onChange({ start, duration: v, end: v === "custom" ? end : endForDuration(start, v) })}
        />
      </div>
      <div className="ag-form-row">
        <div className="field">
          <span>Start date</span>
          <BhDateInput
            ariaLabel="Start date"
            value={start}
            onChange={(v) => onChange({ start: v, duration, end: duration === "custom" ? end : endForDuration(v, duration) })}
          />
        </div>
        <div className="field">
          <span>End date</span>
          <BhDateInput
            ariaLabel="End date"
            value={end}
            onChange={(v) => onChange({ start, duration: durationOf(start, v), end: v })}
          />
        </div>
      </div>
    </>
  );
}

function validDates(start, end) {
  if (start && end && end < start) return "The end date cannot be before the start date.";
  return null;
}

export function SprintFormDialog({ open, boardId, sprint, defaultName, onClose }) {
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [dates, setDates] = useState({ start: "", end: "", duration: "2" });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setName(sprint?.name ?? defaultName ?? "");
    setGoal(sprint?.goal ?? "");
    const start = sprint?.start_date ?? "";
    const end = sprint?.end_date ?? "";
    setDates({ start, end, duration: start && end ? durationOf(start, end) : "custom" });
  }, [open, sprint, defaultName]);

  // A future sprint may have no dates yet (as in Jira); a running one keeps them.
  const error = sprint?.state === "active" && !(dates.start && dates.end)
    ? "An active sprint needs a start and an end date."
    : validDates(dates.start, dates.end);
  const submit = async (e) => {
    e.preventDefault();
    if (error || !name.trim()) return;
    setBusy(true);
    try {
      const body = { name: name.trim(), goal: goal.trim(), start_date: dates.start || null, end_date: dates.end || null };
      if (sprint) await agileApi.updateSprint(sprint.id, { ...body, version: sprint.version });
      else await agileApi.createSprint(boardId, body);
      toast(sprint ? "Sprint updated" : "Sprint created", "success");
      notifyAgileChanged();
      onClose(true);
    } catch (err) {
      toastError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal id="sprintFormDialog" open={open} title={sprint ? `Edit sprint: ${sprint.name}` : "Create sprint"}
      onClose={() => onClose(false)}>
      <form className="ag-dialog" onSubmit={submit}>
        <label className="field">
          <span>Sprint name <em>*</em></span>
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} required data-autofocus />
        </label>
        <DateFields {...dates} onChange={setDates} />
        <label className="field">
          <span>Sprint goal</span>
          <textarea rows={3} value={goal} onChange={(e) => setGoal(e.target.value)} maxLength={2000} />
        </label>
        {error && <p className="ag-error" role="alert">{error}</p>}
        <div className="ag-dialog-actions">
          <button type="button" className="btn ghost" onClick={() => onClose(false)}>Cancel</button>
          <button type="submit" className="btn primary" disabled={busy || !!error || !name.trim()}>
            {sprint ? "Update" : "Create"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function StartSprintDialog({ open, sprint, issues, mode, onClose }) {
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [dates, setDates] = useState({ start: "", end: "", duration: "2" });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open || !sprint) return;
    setName(sprint.name);
    setGoal(sprint.goal || "");
    const start = sprint.start_date || localToday();
    const end = sprint.end_date || endForDuration(start, "2");
    setDates({ start, end, duration: durationOf(start, end) });
  }, [open, sprint]);

  const totals = useMemo(() => categoryTotals(issues || [], mode), [issues, mode]);
  const error = validDates(dates.start, dates.end) || (!dates.start || !dates.end ? "Pick the sprint's dates." : null);

  const submit = async (e) => {
    e.preventDefault();
    if (error) return;
    setBusy(true);
    try {
      await agileApi.startSprint(sprint.id, {
        version: sprint.version, name: name.trim() || sprint.name, goal: goal.trim(),
        start_date: dates.start, end_date: dates.end,
      });
      toast(`${name.trim() || sprint.name} started`, "success");
      notifyAgileChanged();
      onClose(true);
    } catch (err) {
      toastError(err);
    } finally {
      setBusy(false);
    }
  };

  if (!sprint) return null;
  return (
    <Modal id="startSprintDialog" open={open} title="Start sprint" onClose={() => onClose(false)}>
      <form className="ag-dialog" onSubmit={submit}>
        <p className="muted">
          <strong>{totals.count}</strong> issue{totals.count === 1 ? "" : "s"} will be included in this sprint
          {mode !== "item_count" && <> ({formatNumber(totals.total)} {statisticUnit(mode)})</>}.
          {totals.unestimated > 0 && mode !== "item_count" && (
            <> <span className="ag-warn">{totals.unestimated} not estimated.</span></>
          )}
        </p>
        <label className="field">
          <span>Sprint name <em>*</em></span>
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} required data-autofocus />
        </label>
        <DateFields {...dates} onChange={setDates} />
        <label className="field">
          <span>Sprint goal</span>
          <textarea rows={3} value={goal} onChange={(e) => setGoal(e.target.value)} maxLength={2000} />
        </label>
        {error && <p className="ag-error" role="alert">{error}</p>}
        <div className="ag-dialog-actions">
          <button type="button" className="btn ghost" onClick={() => onClose(false)}>Cancel</button>
          <button type="submit" className="btn primary" disabled={busy || !!error}>Start</button>
        </div>
      </form>
    </Modal>
  );
}

/** Jira's completion rule, as far as the client can see it: the issue and all
 * its Sub-tasks are done. The server applies the exact rule. */
function isCompleteIssue(issue) {
  return issue.status_category === "done" && (issue.subtasks_done ?? 0) >= (issue.subtask_count ?? 0);
}

export function CompleteSprintDialog({ open, sprint, issues, futureSprints, onClose }) {
  const [destination, setDestination] = useState("backlog");
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setDestination("backlog");
      setNewName("");
    }
  }, [open]);

  if (!sprint) return null;
  const all = issues || [];
  const done = all.filter(isCompleteIssue);
  const openIssues = all.filter((i) => !isCompleteIssue(i));
  const options = [
    { value: "backlog", label: "Backlog" },
    ...(futureSprints || []).map((s) => ({ value: `sprint:${s.id}`, label: s.name })),
    { value: "new", label: "New sprint" },
  ];

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const body = { version: sprint.version, closing_note: "Sprint completed" };
      if (destination.startsWith("sprint:")) {
        body.default_destination = "sprint";
        body.default_target_sprint_id = Number(destination.slice(7));
      } else if (destination === "new") {
        body.default_destination = "new_sprint";
        if (newName.trim()) body.new_sprint_name = newName.trim();
      } else {
        body.default_destination = "backlog";
      }
      await agileApi.completeSprint(sprint.id, body);
      toast(`${sprint.name} completed`, "success");
      notifyAgileChanged();
      onClose(true);
    } catch (err) {
      toastError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal id="completeSprintDialog" open={open} title={`Complete ${sprint.name}`}
      onClose={() => onClose(false)}>
      <form className="ag-dialog" onSubmit={submit}>
        <p>This sprint contains:</p>
        <ul className="ag-complete-summary">
          <li><strong>{done.length}</strong> completed issue{done.length === 1 ? "" : "s"}</li>
          <li><strong>{openIssues.length}</strong> open issue{openIssues.length === 1 ? "" : "s"}</li>
        </ul>
        <p className="muted small">
          Completed issues stay in this sprint. An issue counts as completed only when it and all its
          sub-tasks are in the board&apos;s last column.
        </p>
        {openIssues.length > 0 && (
          <>
            <div className="field">
              <span>Move open issues to</span>
              <BhSelect ariaLabel="Move open issues to" value={destination} onChange={setDestination} options={options} />
            </div>
            {destination === "new" && (
              <label className="field">
                <span>New sprint name (optional)</span>
                <input value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={120}
                  placeholder="Next default sprint name" />
              </label>
            )}
          </>
        )}
        <div className="ag-dialog-actions">
          <button type="button" className="btn ghost" onClick={() => onClose(false)}>Cancel</button>
          <button type="submit" className="btn primary" disabled={busy}>Complete sprint</button>
        </div>
      </form>
    </Modal>
  );
}
