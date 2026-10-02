// Invite people by email and manage pending invitations (admins and managers).
import { useCallback, useEffect, useState } from "react";
import { confirmDialog } from "../components/ConfirmHost";
import { api } from "../lib/api";
import { isValidEmail } from "../lib/constants";
import { formatDate } from "../lib/format";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";

function statusOf(inv) {
  if (inv.accepted_at) return "accepted";
  if (inv.revoked_at) return "revoked";
  if (new Date(inv.expires_at) < new Date()) return "expired";
  return "pending";
}

export default function InvitationsTab() {
  const { isAdmin, projects } = useApp();
  const [items, setItems] = useState(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("user");
  const [projectIds, setProjectIds] = useState([]);
  const [asLead, setAsLead] = useState(false);

  // An invitation grants access to its projects, so offer only those the sender manages.
  const attachable = projects.filter((p) => isAdmin || p.can_manage);

  const load = useCallback(async () => {
    try {
      setItems(await api("/invitations"));
    } catch (err) {
      toastError(err);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  function toggleProject(id) {
    setProjectIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  async function send(e) {
    e.preventDefault();
    if (!isValidEmail(email)) {
      toast("Enter a valid email address", "error");
      return;
    }
    try {
      await withLoader(async () => {
        await api("/invitations", {
          method: "POST",
          json: { email: email.trim(), role, project_ids: projectIds, as_lead: asLead && projectIds.length > 0 },
        });
        await load();
      }, "Sending invitation…");
      toast(`Invitation sent to ${email.trim()}`, "success");
      setEmail("");
      setProjectIds([]);
      setAsLead(false);
    } catch (err) {
      toastError(err);
    }
  }

  async function revoke(inv) {
    const ok = await confirmDialog(`Revoke the invitation for ${inv.email}? The link stops working.`, {
      title: "Revoke invitation",
      okLabel: "Revoke",
      danger: true,
    });
    if (!ok) return;
    try {
      await withLoader(async () => {
        await api(`/invitations/${inv.id}`, { method: "DELETE" });
        await load();
      }, "Revoking…");
      toast("Invitation revoked", "success");
    } catch (err) {
      toastError(err);
    }
  }

  return (
    <>
      <form className="ent-section" noValidate onSubmit={send}>
        <h3>Invite someone</h3>
        <label className="field">
          <span>
            Email <em>*</em>
          </span>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required maxLength={254}
            autoComplete="off" />
        </label>
        <label className="field">
          <span>Role</span>
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="user">User</option>
            <option value="manager">Manager</option>
            {isAdmin && <option value="admin">Admin</option>}
          </select>
        </label>
        {attachable.length > 0 && (
          <fieldset className="field">
            <legend>Projects to join</legend>
            <div className="ent-check-grid">
              {attachable.map((p) => (
                <label key={p.id} className="ent-check">
                  <input type="checkbox" checked={projectIds.includes(p.id)} onChange={() => toggleProject(p.id)} />
                  <span>{p.name}</span>
                </label>
              ))}
            </div>
            <label className="ent-check">
              <input type="checkbox" checked={asLead} disabled={projectIds.length === 0}
                onChange={(e) => setAsLead(e.target.checked)} />
              <span>Join as project lead</span>
            </label>
          </fieldset>
        )}
        <div className="ent-actions">
          <button type="submit" className="btn primary" disabled={!email}>
            Send invitation
          </button>
        </div>
      </form>

      <div className="ent-section">
        <h3>Invitations</h3>
        {items === null && <p className="hint">Loading…</p>}
        {items?.length === 0 && <p className="hint">No invitations yet.</p>}
        {items?.length > 0 && (
          <div className="ent-table-wrap">
            <table className="ent-table">
              <thead>
                <tr>
                  <th scope="col">Email</th>
                  <th scope="col">Role</th>
                  <th scope="col">Invited by</th>
                  <th scope="col">Expires</th>
                  <th scope="col">Status</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((inv) => {
                  const status = statusOf(inv);
                  return (
                    <tr key={inv.id}>
                      <td>{inv.email}</td>
                      <td>{inv.role}</td>
                      <td>{inv.invited_by_name || "—"}</td>
                      <td>{formatDate(inv.expires_at)}</td>
                      <td>
                        <span className={`ent-status ent-status-${status}`}>{status}</span>
                      </td>
                      <td>
                        {status === "pending" && (
                          <button type="button" className="btn ghost" onClick={() => void revoke(inv)}>
                            Revoke
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
