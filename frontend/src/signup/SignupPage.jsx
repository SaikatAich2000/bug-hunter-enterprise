// Create an organization: the signed-up person becomes its first admin and is signed in.
import { useEffect, useState } from "react";
import AuthCard from "../components/AuthCard";
import PasswordInput from "../components/PasswordInput";
import { PASSWORD_HINT, validatePassword } from "../lib/constants";
import { errorMessage, sendJson } from "../lib/publicApi";

export default function SignupPage() {
  const [alert, setAlert] = useState(null);
  const [busy, setBusy] = useState(false);
  const [enabled, setEnabled] = useState(true);

  // The server says whether public sign-up is on; hide the form when it is not.
  useEffect(() => {
    fetch("/api/meta")
      .then((r) => (r.ok ? r.json() : null))
      .then((meta) => {
        if (meta?.signup_enabled === false) setEnabled(false);
      })
      .catch(() => {
        /* the server decides on submit */
      });
  }, []);

  async function onSubmit(e) {
    e.preventDefault();
    setAlert(null);
    const f = e.currentTarget;
    const value = (name) => f.elements.namedItem(name).value;
    const password = value("password");
    if (password !== value("confirm_password")) {
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
      const res = await sendJson("/auth/signup", {
        json: {
          organization_name: value("organization_name").trim(),
          name: value("name").trim(),
          email: value("email").trim(),
          password,
        },
      });
      if (res.ok) {
        location.replace("/");
        return;
      }
      setAlert({ msg: errorMessage(res.data, "Could not create the organization"), kind: "error" });
    } catch {
      setAlert({ msg: "Network error. Try again", kind: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard
      title="Create your organization"
      tagline="You become its first admin and can invite your team"
      alert={alert}
    >
      {!enabled && (
        <p className="auth-help">
          Creating organizations is turned off on this server. Ask your administrator for an
          invitation.
        </p>
      )}
      <form id="signupForm" className="auth-form" noValidate hidden={!enabled} onSubmit={onSubmit}>
        <label className="field">
          <span>
            Organization name <em>*</em>
          </span>
          <input name="organization_name" type="text" required maxLength={120}
            autoComplete="organization" placeholder="Acme, Inc." autoFocus />
        </label>
        <label className="field">
          <span>
            Your name <em>*</em>
          </span>
          <input name="name" type="text" required maxLength={120} autoComplete="name" />
        </label>
        <label className="field">
          <span>
            Work email <em>*</em>
          </span>
          <input name="email" type="email" required maxLength={254} autoComplete="username" />
        </label>
        <label className="field">
          <span>
            Password <em>*</em>
          </span>
          <PasswordInput name="password" required minLength={8} maxLength={200}
            autoComplete="new-password" />
          <small className="hint">{PASSWORD_HINT}</small>
        </label>
        <label className="field" htmlFor="signupConfirmPassword">
          <span>
            Confirm password <em>*</em>
          </span>
          <PasswordInput id="signupConfirmPassword" name="confirm_password" required minLength={8}
            maxLength={200} autoComplete="new-password" />
        </label>
        <button type="submit" className="btn primary auth-submit" id="signupSubmit" disabled={busy}>
          {busy ? "Creating…" : "Create organization"}
        </button>
      </form>
      <div className="auth-links">
        <a href="/login.html">Already have an account? Sign in</a>
      </div>
    </AuthCard>
  );
}
