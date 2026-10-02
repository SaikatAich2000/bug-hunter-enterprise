// Accept an emailed invitation: preview the organization, then choose a name and password.
import { useEffect, useState } from "react";
import AuthCard from "../components/AuthCard";
import PasswordInput from "../components/PasswordInput";
import { PASSWORD_HINT, validatePassword } from "../lib/constants";
import { errorMessage, sendJson } from "../lib/publicApi";

const ROLE_LABEL = { admin: "an admin", manager: "a manager", user: "a member" };

export default function AcceptInvitePage() {
  const [token] = useState(() => new URLSearchParams(location.search).get("token") || "");
  const [preview, setPreview] = useState(null);
  const [alert, setAlert] = useState(
    token ? null : { msg: "This invitation link is missing its token.", kind: "error" },
  );
  const [loading, setLoading] = useState(Boolean(token));
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) return;
    sendJson(`/invitations/preview/${encodeURIComponent(token)}`, { method: "GET" })
      .then((res) => {
        if (res.ok) setPreview(res.data);
        else setAlert({ msg: errorMessage(res.data, "This invitation is not valid."), kind: "error" });
      })
      .catch(() => setAlert({ msg: "Network error. Try again", kind: "error" }))
      .finally(() => setLoading(false));
  }, [token]);

  async function onSubmit(e) {
    e.preventDefault();
    setAlert(null);
    const f = e.currentTarget;
    const password = f.elements.namedItem("password").value;
    if (password !== f.elements.namedItem("confirm_password").value) {
      setAlert({ msg: "Passwords don't match", kind: "error" });
      return;
    }
    const weak = validatePassword(password);
    if (weak) {
      setAlert({ msg: weak, kind: "error" });
      return;
    }
    setBusy(true);
    try {
      const res = await sendJson("/invitations/accept", {
        json: { token, name: f.elements.namedItem("name").value.trim(), password },
      });
      if (res.ok) {
        location.replace("/");
        return;
      }
      setAlert({ msg: errorMessage(res.data, "Could not accept the invitation"), kind: "error" });
    } catch {
      setAlert({ msg: "Network error. Try again", kind: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Accept invitation" alert={alert}>
      {loading && <p className="auth-help">Checking your invitation…</p>}
      {preview && (
        <form id="acceptForm" className="auth-form" noValidate onSubmit={onSubmit}>
          <p className="auth-help">
            You&apos;ve been invited to join <strong>{preview.organization_name}</strong>
            {preview.invited_by_name ? <> by {preview.invited_by_name}</> : null} as{" "}
            <strong>{ROLE_LABEL[preview.role] || preview.role}</strong>. You will sign in as{" "}
            <strong>{preview.email}</strong>.
          </p>
          <label className="field">
            <span>
              Your name <em>*</em>
            </span>
            <input name="name" type="text" required maxLength={120} autoComplete="name" autoFocus />
          </label>
          <label className="field">
            <span>
              Choose a password <em>*</em>
            </span>
            <PasswordInput name="password" required minLength={8} maxLength={200}
              autoComplete="new-password" />
            <small className="hint">{PASSWORD_HINT}</small>
          </label>
          <label className="field" htmlFor="acceptConfirmPassword">
            <span>
              Confirm password <em>*</em>
            </span>
            <PasswordInput id="acceptConfirmPassword" name="confirm_password" required minLength={8}
              maxLength={200} autoComplete="new-password" />
          </label>
          <button type="submit" className="btn primary auth-submit" id="acceptSubmit" disabled={busy}>
            {busy ? "Joining…" : "Join"}
          </button>
        </form>
      )}
      <div className="auth-links">
        <a href="/login.html">Go to sign in</a>
      </div>
    </AuthCard>
  );
}
