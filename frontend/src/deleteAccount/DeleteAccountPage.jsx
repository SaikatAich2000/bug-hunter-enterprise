// Delete my account from the web (also the deletion URL an app-store listing points to).
// Signs in first (with the 2FA code when needed), then deletes with the password.
import { useState } from "react";
import AuthCard from "../components/AuthCard";
import PasswordInput from "../components/PasswordInput";
import { errorMessage, sendJson } from "../lib/publicApi";

export default function DeleteAccountPage() {
  const [alert, setAlert] = useState(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(null); // pending 2FA token
  const [done, setDone] = useState(false);

  async function signIn(email, password, code) {
    if (pending) {
      return sendJson("/auth/login/totp", { json: { pending_token: pending, code } });
    }
    return sendJson("/auth/login", { json: { email, password } });
  }

  async function onSubmit(e) {
    e.preventDefault();
    setAlert(null);
    const f = e.currentTarget;
    const value = (name) => f.elements.namedItem(name)?.value ?? "";
    const email = value("email").trim();
    const password = value("password");
    if (!email || !password) {
      setAlert({ msg: "Email and password are both required.", kind: "error" });
      return;
    }
    setBusy(true);
    try {
      const signedIn = await signIn(email, password, value("code").trim());
      if (signedIn.ok && signedIn.data?.requires_totp) {
        setPending(signedIn.data.pending_token);
        setAlert({
          msg: "Enter the 6-digit code from your authenticator app (or a recovery code).",
          kind: "success",
        });
        return;
      }
      if (!signedIn.ok) {
        setAlert({ msg: errorMessage(signedIn.data, "Sign-in failed. Check your details."), kind: "error" });
        return;
      }
      const res = await sendJson("/auth/account", { method: "DELETE", json: { password } });
      if (res.ok) {
        setDone(true);
        return;
      }
      setAlert({ msg: errorMessage(res.data, `Delete failed (HTTP ${res.status}).`), kind: "error" });
    } catch {
      setAlert({ msg: "Network error. Try again", kind: "error" });
    } finally {
      setBusy(false);
    }
  }

  const submitLabel = pending ? "Continue" : "Permanently delete my account";

  if (done) {
    return (
      <AuthCard
        title="Account deleted"
        tagline="Your account and its sessions are gone."
        alert={{ msg: "Your account has been deleted. Thanks for using this service.", kind: "success" }}
      >
        <div className="auth-links">
          <a href="/login.html">Back to sign in</a>
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Delete your account" tagline="This is permanent. There is no undo." alert={alert}>
      <form id="deleteForm" className="auth-form" noValidate onSubmit={onSubmit}>
        <p className="auth-help">
          We sign you in, confirm your password and delete your account immediately. Items you
          reported stay so your team isn&apos;t disrupted; your name is removed from them.
        </p>
        <label className="field">
          <span>
            Email <em>*</em>
          </span>
          <input name="email" type="email" required maxLength={254} autoComplete="username" autoFocus
            readOnly={Boolean(pending)} />
        </label>
        <label className="field" htmlFor="deletePassword">
          <span>
            Password <em>*</em>
          </span>
          <PasswordInput id="deletePassword" name="password" required maxLength={200}
            autoComplete="current-password" />
        </label>
        {pending && (
          <label className="field">
            <span>
              2FA code <em>*</em>
            </span>
            <input name="code" type="text" required maxLength={20} autoComplete="one-time-code" />
          </label>
        )}
        <button type="submit" className="btn danger auth-submit" id="deleteSubmit" disabled={busy}>
          {busy ? "Working…" : submitLabel}
        </button>
        <div className="auth-links">
          <a href="/">Cancel and go back</a>
        </div>
      </form>
    </AuthCard>
  );
}
