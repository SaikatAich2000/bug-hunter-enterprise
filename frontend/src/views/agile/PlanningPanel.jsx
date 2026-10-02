import { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../../state/AppContext";
import { api } from "../../lib/api";
import { toast, toastError } from "../../lib/toast";
import BhSelect from "../../components/BhSelect";

const READINESS_MARK = { passed: "✓", warning: "⚠", failed: "✕" };

/** Symbol for a planning readiness check (passed / warning / failed). */
function readinessMark(item) {
  if (item.passed) return READINESS_MARK.passed;
  return READINESS_MARK[item.severity] || READINESS_MARK.failed;
}

export default function PlanningPanel({ projectId, canManage }) {
  const { users } = useApp();
  const [board, setBoard] = useState(null);
  const [sprints, setSprints] = useState([]);
  const [selectedSprintId, setSelectedSprintId] = useState(null);
  const [planning, setPlanning] = useState(null);
  const [loading, setLoading] = useState(false);
  // The capacity being edited, with the sprint it was loaded for: Save writes
  // to that sprint only, never to one picked while an answer was in flight.
  const [capacityDraft, setCapacityDraftState] = useState({ sprintId: null, entries: {} });
  const baseRequest = useRef(0);
  const planningRequest = useRef(0);

  const refreshBase = useCallback(async () => {
    if (!projectId) return;
    const request = ++baseRequest.current;
    setLoading(true);
    try {
      const s = await api(`/agile/projects/${projectId}/settings`);
      if (request !== baseRequest.current) return;
      if (s.agile_enabled && s.board_id) {
        const [b, sprintRows] = await Promise.all([
          api(`/agile/boards/${s.board_id}`),
          api(`/agile/sprints?board_id=${s.board_id}`),
        ]);
        if (request !== baseRequest.current) return;
        setBoard(b);
        const open = sprintRows.filter((sp) => sp.state === "future" || sp.state === "active");
        setSprints(open);
        // Keep the pick only if it belongs to this project's board.
        setSelectedSprintId((prev) => (open.some((sp) => sp.id === prev) ? prev : open[0]?.id ?? null));
      } else {
        setBoard(null);
        setSprints([]);
        setSelectedSprintId(null);
      }
    } catch (err) {
      if (request === baseRequest.current) toastError(err);
    } finally {
      if (request === baseRequest.current) setLoading(false);
    }
  }, [projectId]);
  useEffect(() => { void refreshBase(); }, [refreshBase]);

  const refreshPlanning = useCallback(async () => {
    const request = ++planningRequest.current;
    const sprintId = selectedSprintId;
    setPlanning(null);
    setCapacityDraftState({ sprintId: null, entries: {} });
    if (!sprintId) return;
    try {
      const p = await api(`/agile/sprints/${sprintId}/planning`);
      if (request !== planningRequest.current) return;
      setPlanning(p);
      const entries = {};
      p.capacity.forEach((c) => { entries[c.user_id] = { value: c.capacity_value, unit: c.capacity_unit, notes: c.notes }; });
      setCapacityDraftState({ sprintId, entries });
    } catch (err) {
      if (request === planningRequest.current) toastError(err);
    }
  }, [selectedSprintId]);
  useEffect(() => { void refreshPlanning(); }, [refreshPlanning]);

  const saveCapacity = async () => {
    const sprintId = capacityDraft.sprintId;
    if (!sprintId) return;
    const entries = Object.entries(capacityDraft.entries)
      .filter(([, v]) => v.value !== "" && v.value != null)
      .map(([userId, v]) => ({
        user_id: Number(userId),
        capacity_value: Number(v.value) || 0,
        capacity_unit: v.unit || (board?.estimation_mode === "time" ? "minutes" : "points"),
        notes: v.notes || "",
        days_off: [],
      }));
    try {
      await api(`/agile/sprints/${sprintId}/capacity`, { method: "PUT", json: { entries } });
      toast("Capacity saved", "success");
      await refreshPlanning();
    } catch (err) {
      toastError(err);
    }
  };

  const setDraft = (userId, field, value) => {
    setCapacityDraftState((prev) => ({
      ...prev, entries: { ...prev.entries, [userId]: { ...prev.entries[userId], [field]: value } },
    }));
  };

  if (loading) return <div className="sprints-loading">Loading…</div>;
  if (!board) return <div className="empty-state"><p>Scrum is not set up for this project yet.</p></div>;

  const unit = board.estimation_mode === "time" ? "minutes" : "points";

  return (
    <div className="planning-panel">
      <div className="board-toolbar">
        <label htmlFor="planning-sprint-select" className="muted small">Sprint:</label>
        <BhSelect
          id="planning-sprint-select"
          value={selectedSprintId ? String(selectedSprintId) : ""}
          onChange={(v) => setSelectedSprintId(v ? Number(v) : null)}
          options={sprints.length === 0
            ? [{ value: "", label: "No open sprints" }]
            : sprints.map((sp) => ({ value: String(sp.id), label: `${sp.name} (${sp.state})` }))}
        />
      </div>

      {!planning ? (
        <p className="muted small">Select a sprint to plan.</p>
      ) : (
        <>
          <div className="planning-summary">
            <div className="planning-stat"><strong>{planning.item_count}</strong><span className="muted small">Items</span></div>
            <div className="planning-stat"><strong>{planning.total_estimate ?? "—"}</strong><span className="muted small">Total {unit}</span></div>
            <div className="planning-stat"><strong>{planning.unestimated_count}</strong><span className="muted small">Unestimated</span></div>
            <div className={`planning-stat planning-ready${planning.ready_to_start ? " ok" : ""}`}>
              <strong>{planning.ready_to_start ? "Ready" : "Not ready"}</strong>
              <span className="muted small">to start</span>
            </div>
          </div>

          <div className="sprints-section">
            <h2>Readiness checks</h2>
            {planning.readiness.length === 0 ? (
              <p className="muted small">No checks configured.</p>
            ) : (
              <ul className="planning-readiness-list">
                {planning.readiness.map((r) => (
                  <li key={r.key} className={`planning-readiness-item ${r.passed ? "pass" : r.severity}`}>
                    <span>{readinessMark(r)}</span> {r.message}
                  </li>
                ))}
              </ul>
            )}
          </div>

          {board.estimation_mode === "item_count" ? (
            <div className="sprints-section">
              <h2>Capacity</h2>
              <p className="muted small">
                This board estimates by item count, so per-person capacity isn&apos;t tracked.
              </p>
            </div>
          ) : (
          <div className="sprints-section">
            <h2>Capacity</h2>
            <table className="sprints-backlog-table">
              <thead>
                <tr><th>Person</th><th>Capacity ({unit})</th><th>Notes</th></tr>
              </thead>
              <tbody>
                {users.filter((u) => u.is_active).map((u) => (
                  <tr key={u.id}>
                    <td>{u.name}</td>
                    <td>
                      <input
                        type="number"
                        min="0"
                        disabled={!canManage}
                        value={capacityDraft.entries[u.id]?.value ?? ""}
                        onChange={(e) => setDraft(u.id, "value", e.target.value)}
                        className="planning-capacity-input"
                        aria-label={`Capacity of ${u.name} (${unit})`}
                      />
                    </td>
                    <td>
                      <input
                        disabled={!canManage}
                        value={capacityDraft.entries[u.id]?.notes ?? ""}
                        onChange={(e) => setDraft(u.id, "notes", e.target.value)}
                        placeholder="Optional"
                        aria-label={`Notes for ${u.name}`}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {canManage && (
              <button className="btn primary" style={{ marginTop: 10 }} onClick={saveCapacity}
                disabled={capacityDraft.sprintId !== selectedSprintId}>Save capacity</button>
            )}
          </div>
          )}
        </>
      )}
    </div>
  );
}
