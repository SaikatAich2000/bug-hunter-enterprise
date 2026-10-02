/**
 * User create/edit modal (user == null → create). Password required on create,
 * optional on edit (blank keeps the existing hash).
 */
import { useEffect, useRef, useState } from "react";
import ChipPicker from "../components/ChipPicker";
import { confirmDialog } from "../components/ConfirmHost";
import Modal from "../components/Modal";
import PasswordInput from "../components/PasswordInput";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";
import {
  PASSWORD_HINT,
  PASSWORD_MIN_LENGTH,
  isValidEmail,
  validatePassword,
} from "../lib/constants";
// Key names avoid "password" to reduce password-manager false-positives; createHint mirrors server policy.
const HINTS = {
  editPlaceholder: "Leave blank to keep current",
  editHint: "Leave blank to keep current",
  createPlaceholder: `Min ${PASSWORD_MIN_LENGTH} characters`,
  createHint: PASSWORD_HINT,
};

export default function UserModal() {
  const { userModal, closeUserModal, loadUsers, refreshAll, canManage, isAdmin, projects } = useApp();
  const { open, user } = userModal;
  const isEdit = user != null;

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("user");
  const [isActive, setIsActive] = useState(true);
  const [password, setPassword] = useState("");
  // Projects this user can access (empty = no access for non-admins).
  const [projectIds, setProjectIds] = useState([]);
  const nameRef = useRef(null);

  // Reset form state on every open and focus the first field.
  useEffect(() => {
    if (!open) return;
    setName(user ? user.name : "");
    setEmail(user ? user.email : "");
    setRole(user ? user.role || "user" : "user");
    setIsActive(user ? user.is_active : true);
    setPassword("");
    setProjectIds(user?.project_ids ?? []);
    const t = setTimeout(() => nameRef.current?.focus(), 50);
    return () => clearTimeout(t);
  }, [open, user]);

  async function resetTwoFactor() {
    const ok = await confirmDialog(
      `Turn off two-factor sign-in for ${user.name}? They will sign in with their password alone until they enrol again.`,
      { title: "Turn off 2FA", okLabel: "Turn off", danger: true },
    );
    if (!ok) return;
    try {
      await withLoader(async () => {
        await api(`/users/${user.id}/reset-2fa`, { method: "POST" });
        await loadUsers();
      }, "Turning off 2FA…");
      toast("Two-factor sign-in turned off", "success");
      closeUserModal();
    } catch (err) {
      toastError(err);
    }
  }

  function toggleProject(pid) {
    setProjectIds((cur) =>
      cur.includes(pid) ? cur.filter((x) => x !== pid) : [...cur, pid],
    );
  }

  // Guard matches backend's require_manager_or_admin; keeps the form from rendering for non-managers.
  if (!open || !canManage) return null;

  async function onSubmit(e) {
    e.preventDefault();
    const id = user?.id;
    const trimmedEmail = email.trim();
    if (!isValidEmail(trimmedEmail)) {
      toast("Enter a valid email address", "error");
      return;
    }
    const payload = {
      name: name.trim(),
      email: trimmedEmail,
      role,
      is_active: isActive,
      // Always sent; on edit, replaces memberships with the current selection.
      project_ids: projectIds,
    };
    // On edit, omitting password keeps the existing hash.
    if (password) {
      const pwErr = validatePassword(password);
      if (pwErr) {
        toast(pwErr, "error");
        return;
      }
      payload.password = password;
    } else if (!id) {
      toast("Password is required for new users", "error");
      return;
    }

    try {
      await withLoader(
        async () => {
          if (id) {
            await api(`/users/${id}`, { method: "PUT", json: payload });
          } else {
            await api("/users", { method: "POST", json: payload });
          }
          closeUserModal();
          await loadUsers();
          await refreshAll();
        },
        id ? "Saving user…" : "Creating user…",
      );
      toast(id ? "User updated" : "User created", "success");
    } catch (err) {
      toastError(err);
    }
  }

  return (
    <Modal
      id="modalUser"
      open={open}
      title={
        <span id="modalUserTitle">{user ? `Edit ${user.name}` : "New User"}</span>
      }
      onClose={closeUserModal}
    >
      <form id="formUser" className="modal-body" noValidate onSubmit={onSubmit}>
        <input type="hidden" name="id" value={user ? user.id : ""} readOnly />
        <label className="field">
          <span>
            Name <em>*</em>
          </span>
          <input
            name="name"
            required
            minLength={2}
            maxLength={120}
            ref={nameRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="field">
          <span>
            Email <em>*</em>
          </span>
          <input
            name="email"
            type="email"
            required
            maxLength={254}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="field">
          <span>
            Role <em>*</em>
          </span>
          <select
            name="role"
            value={role}
            onChange={(e) => setRole(e.target.value)}
          >
            <option value="user">User — only edit own bugs</option>
            <option value="manager">Manager — edit any bug, manage projects</option>
            {/* Managers cannot grant admin; only admins can. */}
            {(isAdmin || role === "admin") && (
              <option value="admin">Admin — full access including users</option>
            )}
          </select>
        </label>
        <label className="field" id="userPasswordField">
          <span>
            Password{" "}
            <em className={isEdit ? "js-required hidden" : "js-required"}>*</em>
          </span>
          <PasswordInput
            name="password"
            required={!isEdit}
            minLength={8}
            maxLength={200}
            placeholder={isEdit ? HINTS.editPlaceholder : HINTS.createPlaceholder}
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <small className="hint" id="userPasswordHint">
            {isEdit ? HINTS.editHint : HINTS.createHint}
          </small>
        </label>
        <label className="field check-row">
          <input
            type="checkbox"
            name="is_active"
            checked={isActive}
            onChange={(e) => setIsActive(e.target.checked)}
          />
          <span>Active (can log in)</span>
        </label>
        {isAdmin && isEdit && user.totp_enabled && (
          <div className="field" id="userTwoFactorField">
            <span>Two-factor sign-in</span>
            <button type="button" className="btn" id="userResetTwoFactor" onClick={() => void resetTwoFactor()}>
              Turn off their 2FA
            </button>
            <small className="hint">
              For someone who lost their authenticator and recovery codes. They then sign in with
              their password and can enrol again.
            </small>
          </div>
        )}
        <div className="field" id="userProjectsField">
          <span>Projects</span>
          <ChipPicker
            id="userProjects"
            items={projects.map((p) => ({ id: p.id, label: p.name }))}
            selected={projectIds}
            onToggle={toggleProject}
          />
          <small className="hint">
            Managers and users only see the bugs, requirements, tasks and events
            in the projects tagged here. Leave empty for no access. Admins always
            see everything, regardless of tags.
          </small>
        </div>
        <div className="modal-foot">
          <button
            type="button"
            className="btn ghost"
            data-close-modal
            onClick={closeUserModal}
          >
            Cancel
          </button>
          <button type="submit" className="btn primary">
            Save
          </button>
        </div>
      </form>
    </Modal>
  );
}
