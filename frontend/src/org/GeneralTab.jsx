// Organization name and description (admins).
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";

export default function GeneralTab() {
  const { currentUser, refreshMe } = useApp();
  const [name, setName] = useState(currentUser.organization_name);
  const [description, setDescription] = useState("");
  const [loaded, setLoaded] = useState(false);

  // The description is not part of /auth/me, so fetch it once.
  useEffect(() => {
    api("/organization")
      .then((org) => {
        setName(org.name);
        setDescription(org.description || "");
        setLoaded(true);
      })
      .catch(toastError);
  }, []);

  async function save(e) {
    e.preventDefault();
    try {
      await withLoader(async () => {
        await api("/organization", { method: "PUT", json: { name: name.trim(), description } });
        await refreshMe();
      }, "Saving…");
      toast("Organization updated", "success");
    } catch (err) {
      toastError(err);
    }
  }

  return (
    <form className="ent-section" noValidate onSubmit={save} aria-busy={!loaded}>
      <h3>Organization</h3>
      <fieldset className="ent-fieldset" disabled={!loaded}>
      <label className="field">
        <span>
          Name <em>*</em>
        </span>
        <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} />
      </label>
      <label className="field">
        <span>Description</span>
        <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={3} maxLength={1000} />
      </label>
      <div className="ent-actions">
        <button type="submit" className="btn primary" disabled={name.trim().length < 2}>
          Save
        </button>
      </div>
      </fieldset>
    </form>
  );
}
