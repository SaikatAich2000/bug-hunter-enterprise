/**
 * Modal for changing the current user's password; validates match + policy, POSTs to /api/auth/change-password.
 */
import { useEffect, useRef, useState } from "react";
import Modal from "../components/Modal";
import PasswordInput from "../components/PasswordInput";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";
import { PASSWORD_HINT, validatePassword } from "../lib/constants";

export default function ChangePasswordModal() {
  const { changePasswordOpen, setChangePasswordOpen } = useApp();

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const currentRef = useRef(null);

  const close = () => setChangePasswordOpen(false);

  // Reset the form on open, then focus the current-password input.
  useEffect(() => {
    if (!changePasswordOpen) return;
    setCurrent("");
    setNext("");
    setConfirm("");
    const t = setTimeout(() => currentRef.current?.focus(), 50);
    return () => clearTimeout(t);
  }, [changePasswordOpen]);

  async function onSubmit(e) {
    e.preventDefault();
    if (next !== confirm) {
      toast("New passwords don't match", "error");
      return;
    }
    const pwErr = validatePassword(next);
    if (pwErr) {
      toast(pwErr, "error");
      return;
    }
    try {
      await withLoader(async () => {
        await api("/auth/change-password", {
          method: "POST",
          json: { current_password: current, new_password: next },
        });
        // Clear credentials from state so they aren't retained if reopened.
        setCurrent("");
        setNext("");
        setConfirm("");
        close();
      }, "Updating password…");
      toast("Password updated", "success");
    } catch (err) {
      toastError(err);
    }
  }

  return (
    <Modal
      id="modalChangePassword"
      open={changePasswordOpen}
      title="Change password"
      onClose={close}
    >
      <form
        id="formChangePassword"
        className="modal-body"
        noValidate
        onSubmit={onSubmit}
      >
        <label className="field" htmlFor="cpCurrentPassword">
          <span>
            Current password <em>*</em>
          </span>
          <PasswordInput id="cpCurrentPassword"
            name="current_password"
            required
            autoComplete="current-password"
            ref={currentRef}
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </label>
        <label className="field">
          <span>
            New password <em>*</em>
          </span>
          <PasswordInput
            name="new_password"
            required
            minLength={8}
            maxLength={200}
            autoComplete="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
          <small className="hint">{PASSWORD_HINT}</small>
        </label>
        <label className="field" htmlFor="cpConfirmPassword">
          <span>
            Confirm new password <em>*</em>
          </span>
          <PasswordInput id="cpConfirmPassword"
            name="confirm_password"
            required
            minLength={8}
            maxLength={200}
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </label>
        <div className="modal-foot">
          <button
            type="button"
            className="btn ghost"
            data-close-modal
            onClick={close}
          >
            Cancel
          </button>
          <button type="submit" className="btn primary">
            Update password
          </button>
        </div>
      </form>
    </Modal>
  );
}
