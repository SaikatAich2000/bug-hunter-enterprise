// The project's custom fields on the work item form. The parent keeps `handleRef`, a ref this
// component fills with the current fields and values, and saves them with saveCustomValues()
// after the item itself is saved.
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import BhDateInput from "./BhDateInput";

/** Names of required fields that have no value yet. */
export function missingRequiredFields(handle) {
  if (!handle) return [];
  return handle.fields
    .filter((f) => f.is_required && !String(handle.values[f.id] ?? "").trim())
    .map((f) => f.name);
}

/** Store the form's values on item `bugId` (a no-op when the project has no fields, or on an
 *  existing item whose values were not touched). */
export async function saveCustomValues(bugId, handle) {
  if (!handle || handle.fields.length === 0 || !bugId) return;
  if (!handle.dirty && handle.existing) return;
  await api(`/bugs/${bugId}/custom-values`, {
    method: "PUT",
    json: handle.fields.map((f) => ({ field_id: f.id, value: String(handle.values[f.id] ?? "") })),
  });
}

export default function BugCustomFields({ projectId, bugId, readOnly, handleRef }) {
  const [fields, setFields] = useState([]);
  const [values, setValues] = useState({});
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setFields([]);
    setValues({});
    setDirty(false);
    if (!projectId) return undefined;
    const loadValues = bugId ? api(`/bugs/${bugId}/custom-values`) : Promise.resolve([]);
    Promise.all([api(`/projects/${projectId}/custom-fields`), loadValues])
      .then(([defs, stored]) => {
        if (cancelled) return;
        setFields(defs);
        setValues(Object.fromEntries(stored.map((v) => [v.field_id, v.value])));
      })
      .catch(() => {
        /* the item form still works without its custom fields */
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, bugId]);

  // Publish the state for the parent's save handler (read in event handlers, never in render).
  useEffect(() => {
    handleRef.current = { fields, values, dirty, existing: Boolean(bugId) };
  }, [handleRef, fields, values, dirty, bugId]);

  if (fields.length === 0) return null;

  function set(id, value) {
    setValues((prev) => ({ ...prev, [id]: value }));
    setDirty(true);
  }

  return (
    <fieldset className="field ent-custom-fields" id="bugCustomFields" disabled={readOnly}>
      <legend>Custom fields</legend>
      {fields.map((f) => {
        const id = `customField-${f.id}`;
        const label = (
          <span>
            {f.name} {f.is_required && <em>*</em>}
          </span>
        );
        if (f.field_type === "select") {
          return (
            <label key={f.id} className="field" htmlFor={id}>
              {label}
              <select id={id} value={values[f.id] ?? ""} onChange={(e) => set(f.id, e.target.value)}>
                <option value="">—</option>
                {f.options.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            </label>
          );
        }
        if (f.field_type === "date") {
          return (
            <div key={f.id} className="field">
              {label}
              <BhDateInput name={id} ariaLabel={f.name} value={values[f.id] ?? ""} onChange={(v) => set(f.id, v)} />
            </div>
          );
        }
        return (
          <label key={f.id} className="field" htmlFor={id}>
            {label}
            <input id={id} type={f.field_type === "number" ? "number" : "text"} step="any" maxLength={2000}
              value={values[f.id] ?? ""} onChange={(e) => set(f.id, e.target.value)} />
          </label>
        );
      })}
    </fieldset>
  );
}
