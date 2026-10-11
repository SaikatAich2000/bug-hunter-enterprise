// Create an organization in three short steps (organization, you, password); the signed-up person becomes
// its first admin. When the server wants the address confirmed, a fourth "check your inbox" step takes over
// instead of a redirect, with a resend that waits a minute between links.
import { useEffect, useRef, useState } from "react";
import AuthCard from "../components/AuthCard";
import PasswordInput from "../components/PasswordInput";
import ResendVerification from "../components/ResendVerification";
import { PASSWORD_HINT, isValidEmail, validatePassword } from "../lib/constants";
import { errorMessage, sendJson } from "../lib/publicApi";

const STEPS = ["Organization", "You", "Password", "Confirm"];

export default function SignupPage() {
  const [step, setStep] = useState(0);
  const [dir, setDir] = useState("fwd");
  const [alert, setAlert] = useState(null);
  const [busy, setBusy] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [form, setForm] = useState({ organization_name: "", name: "", email: "", password: "", confirm_password: "" });
  const headingRef = useRef(null);
  const mounted = useRef(false);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

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

  // A new step replaces the old one: focus its heading so screen readers announce it and Tab starts there.
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    headingRef.current?.focus({ preventScroll: true });
  }, [step]);

  const go = (to) => {
    setAlert(null);
    setDir(to > step ? "fwd" : "back");
    setStep(to);
  };
  const fail = (msg) => setAlert({ msg, kind: "error" });

  function next(e) {
    e.preventDefault();
    if (step === 0) {
      if (form.organization_name.trim().length < 2) return fail("Give your organization a name of at least 2 characters");
      return go(1);
    }
    if (form.name.trim().length < 2) return fail("Enter your name");
    if (!isValidEmail(form.email.trim())) return fail("Enter a valid work email address");
    return go(2);
  }

  async function onSubmit(e) {
    e.preventDefault();
    setAlert(null);
    if (form.password !== form.confirm_password) return fail("Passwords don't match");
    const weak = validatePassword(form.password);
    if (weak) return fail(weak);
    setBusy(true);
    try {
      const res = await sendJson("/auth/signup", {
        json: {
          organization_name: form.organization_name.trim(),
          name: form.name.trim(),
          email: form.email.trim(),
          password: form.password,
        },
      });
      if (res.status === 202) {
        // Verification required: no session yet, the link was mailed.
        setForm((f) => ({ ...f, password: "", confirm_password: "" }));
        go(3);
      } else if (res.ok) {
        location.replace("/");
      } else {
        fail(errorMessage(res.data, "Could not create the organization"));
      }
    } catch {
      fail("Network error. Try again");
    } finally {
      setBusy(false);
    }
    return undefined;
  }

  const heading = (text) => (
    <h2 className="auth-step-title" tabIndex={-1} ref={headingRef}>{text}</h2>
  );

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
      <div hidden={!enabled} id="signupSteps">
        <ol className="auth-pips" aria-label="Sign-up progress">
          {STEPS.map((label, i) => (
            <li key={label} className={`auth-pip${i === step ? " current" : ""}${i < step ? " past" : ""}`}
              aria-current={i === step ? "step" : undefined} title={label}>
              <span aria-hidden="true">{i < step ? "✓" : i + 1}</span>
              <span className="sr-only">{`Step ${i + 1}: ${label}${i < step ? " (done)" : ""}`}</span>
            </li>
          ))}
        </ol>

        <div className="auth-step" key={step} data-dir={dir}>
          {step === 0 && (
            <form id="signupForm" className="auth-form" noValidate onSubmit={next}>
              {heading("Name your organization")}
              <label className="field">
                <span>Organization name <em>*</em></span>
                <input name="organization_name" type="text" required maxLength={120} autoComplete="organization"
                  placeholder="Acme, Inc." autoFocus value={form.organization_name} onChange={set("organization_name")} />
              </label>
              <button type="submit" className="btn primary auth-submit">Continue</button>
            </form>
          )}

          {step === 1 && (
            <form id="signupForm" className="auth-form" noValidate onSubmit={next}>
              {heading("Tell us about you")}
              <label className="field">
                <span>Your name <em>*</em></span>
                <input name="name" type="text" required maxLength={120} autoComplete="name" value={form.name}
                  onChange={set("name")} />
              </label>
              <label className="field">
                <span>Work email <em>*</em></span>
                <input name="email" type="email" required maxLength={254} autoComplete="username" value={form.email}
                  onChange={set("email")} />
              </label>
              <div className="auth-nav">
                <button type="button" className="btn" onClick={() => go(0)}>Back</button>
                <button type="submit" className="btn primary">Continue</button>
              </div>
            </form>
          )}

          {step === 2 && (
            <form id="signupForm" className="auth-form" noValidate onSubmit={onSubmit}>
              {heading("Choose a password")}
              <label className="field">
                <span>Password <em>*</em></span>
                <PasswordInput name="password" required minLength={8} maxLength={200} autoComplete="new-password"
                  value={form.password} onChange={set("password")} />
                <small className="hint">{PASSWORD_HINT}</small>
              </label>
              <label className="field" htmlFor="signupConfirmPassword">
                <span>Confirm password <em>*</em></span>
                <PasswordInput id="signupConfirmPassword" name="confirm_password" required minLength={8}
                  maxLength={200} autoComplete="new-password" value={form.confirm_password}
                  onChange={set("confirm_password")} />
              </label>
              <div className="auth-nav">
                <button type="button" className="btn" disabled={busy} onClick={() => go(1)}>Back</button>
                <button type="submit" className="btn primary" id="signupSubmit" disabled={busy}>
                  {busy ? "Creating..." : "Create organization"}
                </button>
              </div>
            </form>
          )}

          {step === 3 && (
            <div id="signupSent">
              <div className="auth-status wait" aria-hidden="true">{"✉"}</div>
              {heading("Check your inbox")}
              <p className="auth-help">
                We sent a confirmation link to <strong>{form.email.trim()}</strong>. Open it to finish creating{" "}
                {form.organization_name.trim()}. The link works once and expires.
              </p>
              <ResendVerification email={form.email.trim()} onAlert={setAlert} />
            </div>
          )}
        </div>
      </div>
      <div className="auth-links">
        <a href="/login.html">Already have an account? Sign in</a>
      </div>
    </AuthCard>
  );
}
