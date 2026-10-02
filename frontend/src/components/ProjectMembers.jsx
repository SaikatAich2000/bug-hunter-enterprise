// Members of one project and their project role (lead / member). Admins and the project's
// leads can add, change and remove people; everyone else only sees the list.
import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";
import { confirmDialog } from "./ConfirmHost";

export default function ProjectMembers({ projectId, editable }) {
  const { users, loadProjects } = useApp();
  const [members, setMembers] = useState(null);
  const [newUserId, setNewUserId] = useState("");
  const [newRole, setNewRole] = useState("member");

  const load = useCallback(async () => {
    try {
      setMembers(await api(`/projects/${projectId}/members`));
    } catch (err) {
      toastError(err);
    }
  }, [projectId]);
  useEffect(() => {
    void load();
  }, [load]);

  const memberIds = new Set((members ?? []).map((m) => m.user_id));
  const candidates = users.filter((u) => u.is_active && !memberIds.has(u.id));

  async function run(label, fn) {
    try {
      await withLoader(async () => {
        await fn();
        await Promise.all([load(), loadProjects()]);
      }, label);
    } catch (err) {
      toastError(err);
    }
  }

  async function add(e) {
    e.preventDefault();
    if (!newUserId) return;
    await run("Adding…", () =>
      api(`/projects/${projectId}/members`, { method: "POST", json: { user_id: Number(newUserId), role: newRole } }),
    );
    setNewUserId("");
    setNewRole("member");
  }

  async function remove(m) {
    const ok = await confirmDialog(`Remove ${m.user_name} from this project?`, {
      title: "Remove member",
      okLabel: "Remove",
      danger: true,
    });
    if (!ok) return;
    await run("Removing…", () => api(`/projects/${projectId}/members/${m.user_id}`, { method: "DELETE" }));
    toast("Member removed", "success");
  }

  return (
    <div className="ent-section" id="projectMembers">
      <h3>Members</h3>
      <p className="hint">
        Leads manage the project&apos;s members and custom fields. Admins see every project without being
        listed.
      </p>
      {members === null && <p className="hint">Loading…</p>}
      {members?.length === 0 && <p className="hint">Nobody has been added yet.</p>}
      {members?.length > 0 && (
        <ul className="ent-member-list">
          {members.map((m) => (
            <li key={m.user_id} className="ent-member">
              <span className="ent-member-name">
                {m.user_name} <span className="hint">{m.user_email}</span>
              </span>
              {editable ? (
                <select
                  aria-label={`Project role of ${m.user_name}`}
                  value={m.project_role}
                  onChange={(e) =>
                    void run("Saving…", () =>
                      api(`/projects/${projectId}/members/${m.user_id}`, { method: "PUT", json: { role: e.target.value } }),
                    )
                  }
                >
                  <option value="member">Member</option>
                  <option value="lead">Lead</option>
                </select>
              ) : (
                <span className="ent-status">{m.project_role}</span>
              )}
              {editable && (
                <button type="button" className="btn ghost" onClick={() => void remove(m)}>
                  Remove
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {editable && candidates.length > 0 && (
        <form className="ent-inline-form" noValidate onSubmit={add}>
          <select aria-label="Person to add" value={newUserId} onChange={(e) => setNewUserId(e.target.value)}>
            <option value="">Add a person…</option>
            {candidates.map((u) => (
              <option key={u.id} value={u.id}>
                {u.name} ({u.email})
              </option>
            ))}
          </select>
          <select aria-label="Role of the new member" value={newRole} onChange={(e) => setNewRole(e.target.value)}>
            <option value="member">Member</option>
            <option value="lead">Lead</option>
          </select>
          <button type="submit" className="btn primary" disabled={!newUserId}>
            Add
          </button>
        </form>
      )}
    </div>
  );
}
