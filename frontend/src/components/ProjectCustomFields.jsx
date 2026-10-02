// Custom fields of one project (text, number, date, single choice) that items of the
// project can fill in. Admins and the project's leads manage them.
import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { confirmDialog } from "./ConfirmHost";

const TYPES = [
  { value: "text", label: "Text" },
  { value: "number", label: "Number" },
  { value: "date", label: "Date" },
  { value: "select", label: "Choice" },
];

export default function ProjectCustomFields({ projectId, editable }) {
  const [fields, setFields] = useState(null);
  const [name, setName] = useState("");
  const [type, setType] = useState("text");
  const [options, setOptions] = useState("");
  const [required, setRequired] = useState(false);

  const load = useCallback(async () => {
    try {
      setFields(await api(`/projects/${projectId}/custom-fields`));
    } catch (err) {
      toastError(err);
    }
  }, [projectId]);
  useEffect(() => {
    void load();
  }, [load]);

  async function add(e) {
    e.preventDefault();
    try {
      await withLoader(async () => {
        await api(`/projects/${projectId}/custom-fields`, {
          method: "POST",
          json: {
            name: name.trim(),
            field_type: type,
            options: type === "select" ? options.split(",").map((o) => o.trim()).filter(Boolean) : [],
            is_required: required,
            position: fields?.length ?? 0,
          },
        });
        await load();
      }, "Adding field…");
      setName("");
      setOptions("");
      setRequired(false);
      setType("text");
    } catch (err) {
      toastError(err);
    }
  }

  async function remove(f) {
    const ok = await confirmDialog(`Delete the field "${f.name}"? Its values on every item are removed.`, {
      title: "Delete field",
      okLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await withLoader(async () => {
        await api(`/projects/${projectId}/custom-fields/${f.id}`, { method: "DELETE" });
        await load();
      }, "Deleting…");
      toast("Field deleted", "success");
    } catch (err) {
      toastError(err);
    }
  }

  return (
    <div className="ent-section" id="projectCustomFields">
      <h3>Custom fields</h3>
      {fields === null && <p className="hint">Loading…</p>}
      {fields?.length === 0 && <p className="hint">No custom fields yet.</p>}
      {fields?.length > 0 && (
        <ul className="ent-member-list">
          {fields.map((f) => (
            <li key={f.id} className="ent-member">
              <span className="ent-member-name">
                {f.name}{" "}
                <span className="hint">
                  {TYPES.find((t) => t.value === f.field_type)?.label ?? f.field_type}
                  {f.options.length > 0 ? `: ${f.options.join(", ")}` : ""}
                  {f.is_required ? " · required" : ""}
                </span>
              </span>
              {editable && (
                <button type="button" className="btn ghost" onClick={() => void remove(f)}>
                  Delete
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {editable && (
        <form className="ent-inline-form" noValidate onSubmit={add}>
          <input aria-label="Field name" placeholder="Field name" value={name}
            onChange={(e) => setName(e.target.value)} maxLength={80} />
          <select aria-label="Field type" value={type} onChange={(e) => setType(e.target.value)}>
            {TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
          {type === "select" && (
            <input aria-label="Choices, comma-separated" placeholder="Choices, comma-separated" value={options}
              onChange={(e) => setOptions(e.target.value)} />
          )}
          <label className="ent-check">
            <input type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} />
            <span>Required</span>
          </label>
          <button type="submit" className="btn primary"
            disabled={!name.trim() || (type === "select" && !options.trim())}>
            Add field
          </button>
        </form>
      )}
    </div>
  );
}
