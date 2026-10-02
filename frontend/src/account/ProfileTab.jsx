// Display name, and the two-step verified email change.
import { useState } from "react";
import PasswordInput from "../components/PasswordInput";
import { api } from "../lib/api";
import { isValidEmail } from "../lib/constants";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";

export default function ProfileTab() {
  const { currentUser, refreshMe } = useApp();
  const [name, setName] = useState(currentUser.name);
  const [newEmail, setNewEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [awaitingCode, setAwaitingCode] = useState(false);

  async function saveName(e) {
    e.preventDefault();
    try {
      await withLoader(async () => {
        await api("/auth/profile", { method: "PUT", json: { name: name.trim() } });
        await refreshMe();
      }, "Saving…");
      toast("Profile updated", "success");
    } catch (err) {
      toastError(err);
    }
  }

  async function requestChange(e) {
    e.preventDefault();
    if (!isValidEmail(newEmail)) {
      toast("Enter a valid email address", "error");
      return;
    }
    try {
      await withLoader(
        () => api("/auth/email-change/request", {
          method: "POST",
          json: { new_email: newEmail.trim(), current_password: password },
        }),
        "Sending code…",
      );
      setPassword("");
      setAwaitingCode(true);
      toast(`A 6-digit code was sent to ${newEmail.trim()}`, "success");
    } catch (err) {
      toastError(err);
    }
  }

  async function confirmChange(e) {
    e.preventDefault();
    try {
      await withLoader(async () => {
        await api("/auth/email-change/confirm", { method: "POST", json: { code: code.trim() } });
        await refreshMe();
      }, "Confirming…");
      setAwaitingCode(false);
      setNewEmail("");
      setCode("");
      toast("Email address changed", "success");
    } catch (err) {
      toastError(err);
    }
  }

  return (
    <>
      <form className="ent-section" noValidate onSubmit={saveName}>
        <h3>Profile</h3>
        <label className="field">
          <span>
            Name <em>*</em>
          </span>
          <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={120}
            autoComplete="name" />
        </label>
        <p className="hint">
          {currentUser.organization_name} · {currentUser.role}
        </p>
        <div className="ent-actions">
          <button type="submit" className="btn primary" disabled={!name.trim() || name.trim() === currentUser.name}>
            Save
          </button>
        </div>
      </form>

      <div className="ent-section">
        <h3>Email address</h3>
        <p className="hint">
          Signed in as <strong>{currentUser.email}</strong>. Changing it needs your password and a code
          sent to the new address.
        </p>
        {!awaitingCode ? (
          <form noValidate onSubmit={requestChange}>
            <label className="field">
              <span>
                New email <em>*</em>
              </span>
              <input type="email" value={newEmail} onChange={(e) => setNewEmail(e.target.value)} required
                maxLength={254} autoComplete="email" />
            </label>
            <label className="field" htmlFor="profileEmailPassword">
              <span>
                Current password <em>*</em>
              </span>
              <PasswordInput id="profileEmailPassword" value={password}
                onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password" />
            </label>
            <div className="ent-actions">
              <button type="submit" className="btn primary" disabled={!newEmail || !password}>
                Send code
              </button>
            </div>
          </form>
        ) : (
          <form noValidate onSubmit={confirmChange}>
            <label className="field">
              <span>
                6-digit code sent to {newEmail} <em>*</em>
              </span>
              <input value={code} onChange={(e) => setCode(e.target.value)} required maxLength={6}
                inputMode="numeric" autoComplete="one-time-code" />
            </label>
            <div className="ent-actions">
              <button type="button" className="btn ghost" onClick={() => setAwaitingCode(false)}>
                Start over
              </button>
              <button type="submit" className="btn primary" disabled={code.trim().length !== 6}>
                Confirm
              </button>
            </div>
          </form>
        )}
      </div>
    </>
  );
}
