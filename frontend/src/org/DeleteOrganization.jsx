// Danger zone of the General tab: delete the whole organization (admins). Two steps, like the server's:
// a dry run that lists what would go, then the password and the organization's exact name.
import { useEffect, useRef, useState } from "react";
import PasswordInput from "../components/PasswordInput";
import { api } from "../lib/api";
import { toastError } from "../lib/toast";

const LABELS = [
  ["users", "People"],
  ["projects", "Projects"],
  ["items", "Work items"],
  ["comments", "Comments"],
  ["attachments", "Attachments"],
  ["webhooks", "Webhooks"],
  ["invitations", "Invitations"],
  ["saved_views", "Saved views"],
  ["audit_entries", "Audit trail entries"],
];

export default function DeleteOrganization({ orgName }) {
  const [summary, setSummary] = useState(null);
  const [password, setPassword] = useState("");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const headingRef = useRef(null);

  useEffect(() => {
    if (summary) headingRef.current?.focus({ preventScroll: true });
  }, [summary]);

  async function preview() {
    setBusy(true);
    try {
      setSummary(await api("/organization/deletion-summary"));
    } catch (err) {
      toastError(err);
    } finally {
      setBusy(false);
    }
  }

  async function remove(e) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/organization/delete", { method: "POST", json: { password, confirm_name: typed } });
      location.replace("/login.html");
    } catch (err) {
      toastError(err);
      setBusy(false);
    }
  }

  function cancel() {
    setSummary(null);
    setPassword("");
    setTyped("");
  }

  return (
    <section className="ent-section ent-danger" id="orgDanger" aria-labelledby="orgDangerTitle">
      <h3 id="orgDangerTitle">Delete organization</h3>
      <p className="hint">
        Permanently removes {orgName} and everything in it. There is no undo. First see exactly what would be deleted.
      </p>
      {!summary ? (
        <div className="ent-actions">
          <button type="button" className="btn" disabled={busy} onClick={() => void preview()}>
            Show what will be deleted
          </button>
        </div>
      ) : (
        <form noValidate onSubmit={remove}>
          <h4 tabIndex={-1} ref={headingRef} className="ent-danger-heading">This will permanently delete</h4>
          <dl className="ent-counts">
            {LABELS.map(([key, label]) => (
              <div key={key}>
                <dt>{label}</dt>
                <dd>{summary.will_delete?.[key] ?? 0}</dd>
              </div>
            ))}
          </dl>
          <label className="field" htmlFor="orgDeletePassword">
            <span>Your password <em>*</em></span>
            <PasswordInput id="orgDeletePassword" value={password} onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password" />
          </label>
          <label className="field" htmlFor="orgDeleteName">
            <span>Type the organization name <strong>{summary.organization}</strong> to confirm <em>*</em></span>
            <input id="orgDeleteName" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
          </label>
          <div className="ent-actions">
            <button type="button" className="btn ghost" disabled={busy} onClick={cancel}>Cancel</button>
            <button type="submit" className="btn danger" disabled={busy || !password || typed !== summary.organization}>
              Delete organization forever
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
