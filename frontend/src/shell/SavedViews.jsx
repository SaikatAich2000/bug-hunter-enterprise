// Saved filter views: apply one, save the current filters as a view, delete your own.
// Admins and managers can share a view with the whole organization.
import { useCallback, useEffect, useState } from "react";
import { confirmDialog } from "../components/ConfirmHost";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";

export default function SavedViews() {
  const { filters, setFilters, canManage } = useApp();
  const [views, setViews] = useState([]);
  const [selected, setSelected] = useState("");
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState("");
  const [shared, setShared] = useState(false);

  const load = useCallback(async () => {
    try {
      setViews(await api("/saved-views"));
    } catch (err) {
      toastError(err);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const current = views.find((v) => String(v.id) === selected) ?? null;

  function apply(id) {
    setSelected(id);
    const view = views.find((v) => String(v.id) === id);
    if (view) setFilters((prev) => ({ ...prev, ...view.filters }));
  }

  async function save(e) {
    e.preventDefault();
    try {
      const made = await withLoader(
        () => api("/saved-views", { method: "POST", json: { name: name.trim(), filters, shared_with_org: shared } }),
        "Saving view…",
      );
      await load();
      setSelected(String(made.id));
      setSaving(false);
      setName("");
      setShared(false);
      toast("View saved", "success");
    } catch (err) {
      toastError(err);
    }
  }

  async function remove() {
    const ok = await confirmDialog(`Delete the view "${current.name}"?`, {
      title: "Delete view",
      okLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await withLoader(() => api(`/saved-views/${current.id}`, { method: "DELETE" }), "Deleting…");
      setSelected("");
      await load();
    } catch (err) {
      toastError(err);
    }
  }

  return (
    <div className="saved-views" id="savedViews">
      <select aria-label="Saved views" value={selected} onChange={(e) => apply(e.target.value)}>
        <option value="">Saved views…</option>
        {views.map((v) => (
          <option key={v.id} value={v.id}>
            {v.shared_with_org ? "🌐 " : ""}
            {v.name}
          </option>
        ))}
      </select>
      {current?.is_mine && (
        <button type="button" className="btn ghost" onClick={() => void remove()}>
          Delete view
        </button>
      )}
      {!saving ? (
        <button type="button" className="btn ghost" onClick={() => setSaving(true)}>
          Save view
        </button>
      ) : (
        <form className="saved-views-form" noValidate onSubmit={save}>
          <input aria-label="View name" placeholder="View name" value={name} maxLength={80} autoFocus
            onChange={(e) => setName(e.target.value)} />
          {canManage && (
            <label className="ent-check">
              <input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} />
              <span>Share with everyone</span>
            </label>
          )}
          <button type="submit" className="btn primary" disabled={!name.trim()}>
            Save
          </button>
          <button type="button" className="btn ghost" onClick={() => setSaving(false)}>
            Cancel
          </button>
        </form>
      )}
    </div>
  );
}
