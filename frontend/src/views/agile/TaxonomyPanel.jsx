import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";
import { toast, toastError } from "../../lib/toast";
import { confirmDialog } from "../../components/ConfirmHost";

const SUB_TABS = [
  { key: "releases", label: "Releases" },
  { key: "components", label: "Components" },
  { key: "labels", label: "Labels" },
];

function CreateRow({ fields, onCreate, busy }) {
  const [values, setValues] = useState({});
  const set = (k, v) => setValues((prev) => ({ ...prev, [k]: v }));
  return (
    <div className="sprints-create">
      {fields.map((f) => (
        <input
          key={f.key}
          placeholder={f.placeholder}
          value={values[f.key] || ""}
          onChange={(e) => set(f.key, e.target.value)}
          style={f.grow ? { flex: 1 } : undefined}
        />
      ))}
      <button
        className="btn primary"
        disabled={busy}
        onClick={() => onCreate(values, () => setValues({}))}
      >
        Create
      </button>
    </div>
  );
}

function ReleasesTab({ projectId, canManage }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await api(`/agile/releases?project_id=${projectId}`));
    } catch (err) {
      toastError(err);
    } finally {
      setLoading(false);
    }
  }, [projectId]);
  useEffect(() => { void refresh(); }, [refresh]);

  const create = async (values, clear) => {
    if (!values.name?.trim()) return toast("Release name required", "error");
    try {
      await api("/agile/releases", { method: "POST", json: { project_id: projectId, name: values.name.trim() } });
      clear();
      toast("Release created", "success");
      await refresh();
    } catch (err) {
      toastError(err);
    }
  };

  const release = async (row) => {
    const ok = await confirmDialog(`Release "${row.name}"? This marks it released.`, { title: "Release Version", okLabel: "Release", danger: false });
    if (!ok) return;
    try {
      await api(`/agile/releases/${row.id}/release`, { method: "POST", json: { version: row.version } });
      toast("Version released", "success");
      await refresh();
    } catch (err) {
      toastError(err);
    }
  };

  const archive = async (row) => {
    const ok = await confirmDialog(`Archive release "${row.name}"?`, { title: "Archive Release", okLabel: "Archive", danger: true });
    if (!ok) return;
    try {
      await api(`/agile/releases/${row.id}/archive`, { method: "POST" });
      toast("Release archived", "success");
      await refresh();
    } catch (err) {
      toastError(err);
    }
  };

  if (loading) return <div className="sprints-loading">Loading…</div>;
  return (
    <div>
      {canManage && <CreateRow fields={[{ key: "name", placeholder: "e.g. v2.1", grow: true }]} onCreate={create} />}
      {rows.length === 0 ? <p className="muted small">No releases yet.</p> : (
        <table className="sprints-backlog-table">
          <thead><tr><th>Name</th><th>Status</th><th>Release date</th><th style={{ textAlign: "right" }}>Actions</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.name}</td>
                <td><span className="badge" data-status={r.status}>{r.status}</span></td>
                <td>{r.release_date || "—"}</td>
                <td style={{ textAlign: "right" }}>
                  {canManage && r.status === "unreleased" && (
                    <span className="table-actions">
                      <button className="btn btn-sm" onClick={() => release(r)}>Release</button>
                      <button className="btn danger btn-sm" onClick={() => archive(r)}>Archive</button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ComponentsTab({ projectId, canManage }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await api(`/agile/components?project_id=${projectId}`));
    } catch (err) {
      toastError(err);
    } finally {
      setLoading(false);
    }
  }, [projectId]);
  useEffect(() => { void refresh(); }, [refresh]);

  const create = async (values, clear) => {
    if (!values.name?.trim()) return toast("Component name required", "error");
    try {
      await api("/agile/components", { method: "POST", json: { project_id: projectId, name: values.name.trim() } });
      clear();
      toast("Component created", "success");
      await refresh();
    } catch (err) {
      toastError(err);
    }
  };

  const archive = async (row) => {
    const ok = await confirmDialog(`Archive component "${row.name}"?`, { title: "Archive Component", okLabel: "Archive", danger: true });
    if (!ok) return;
    try {
      await api(`/agile/components/${row.id}/archive`, { method: "POST" });
      toast("Component archived", "success");
      await refresh();
    } catch (err) {
      toastError(err);
    }
  };

  if (loading) return <div className="sprints-loading">Loading…</div>;
  return (
    <div>
      {canManage && <CreateRow fields={[{ key: "name", placeholder: "e.g. Backend", grow: true }]} onCreate={create} />}
      {rows.length === 0 ? <p className="muted small">No components yet.</p> : (
        <table className="sprints-backlog-table">
          <thead><tr><th>Name</th><th>Assignee policy</th><th style={{ textAlign: "right" }}>Actions</th></tr></thead>
          <tbody>
            {rows.filter((r) => !r.archived).map((r) => (
              <tr key={r.id}>
                <td>{r.name}</td>
                <td>{r.default_assignee_policy}</td>
                <td style={{ textAlign: "right" }}>
                  {canManage && <button className="btn danger btn-sm" onClick={() => archive(r)}>Archive</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function LabelsTab({ projectId, canManage }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await api(`/agile/labels?project_id=${projectId}`));
    } catch (err) {
      toastError(err);
    } finally {
      setLoading(false);
    }
  }, [projectId]);
  useEffect(() => { void refresh(); }, [refresh]);

  const create = async (values, clear) => {
    if (!values.display_name?.trim()) return toast("Label name required", "error");
    try {
      await api("/agile/labels", { method: "POST", json: { project_id: projectId, display_name: values.display_name.trim() } });
      clear();
      toast("Label created", "success");
      await refresh();
    } catch (err) {
      toastError(err);
    }
  };

  const remove = async (row) => {
    const ok = await confirmDialog(`Delete label "${row.display_name}"? Used on ${row.usage_count} item(s).`, {
      title: "Delete Label", okLabel: "Delete", danger: true,
    });
    if (!ok) return;
    try {
      await api(`/agile/labels/${row.id}`, { method: "DELETE" });
      toast("Label deleted", "success");
      await refresh();
    } catch (err) {
      toastError(err);
    }
  };

  if (loading) return <div className="sprints-loading">Loading…</div>;
  return (
    <div>
      {canManage && <CreateRow fields={[{ key: "display_name", placeholder: "e.g. security", grow: true }]} onCreate={create} />}
      {rows.length === 0 ? <p className="muted small">No labels yet.</p> : (
        <div className="epic-filter-row">
          {rows.map((r) => (
            <span key={r.id} className="epic-chip" style={{ "--epic-color": r.color || "#607d8b" }}>
              <span className="epic-chip-dot" /> {r.display_name} ({r.usage_count})
              {canManage && (
                <button className="btn ghost btn-sm" title="Delete" onClick={() => remove(r)} style={{ marginLeft: 6 }}>✕</button>
              )}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export default function TaxonomyPanel({ projectId, canManage }) {
  const [subTab, setSubTab] = useState("releases");
  // null while loading; the taxonomy routes 404 while Agile is off, so show
  // the same empty state as the other Sprint tabs instead of error toasts.
  const [agileEnabled, setAgileEnabled] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setAgileEnabled(null);
    if (!projectId) return undefined;
    api(`/agile/projects/${projectId}/settings`)
      .then((s) => { if (!cancelled) setAgileEnabled(Boolean(s.agile_enabled)); })
      .catch((err) => { if (!cancelled) { setAgileEnabled(false); toastError(err); } });
    return () => { cancelled = true; };
  }, [projectId]);

  if (agileEnabled === null) return <div className="sprints-loading">Loading…</div>;
  if (!agileEnabled) {
    return (
      <div className="empty-state">
        <p>Scrum is not set up for this project yet.</p>
      </div>
    );
  }
  return (
    <div className="taxonomy-panel">
      <div className="agile-tabs taxonomy-subtabs" role="tablist" aria-label="Taxonomy views">
        {SUB_TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={subTab === t.key}
            className={`agile-tab${subTab === t.key ? " active" : ""}`}
            onClick={() => setSubTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      {subTab === "releases" && <ReleasesTab projectId={projectId} canManage={canManage} />}
      {subTab === "components" && <ComponentsTab projectId={projectId} canManage={canManage} />}
      {subTab === "labels" && <LabelsTab projectId={projectId} canManage={canManage} />}
    </div>
  );
}
