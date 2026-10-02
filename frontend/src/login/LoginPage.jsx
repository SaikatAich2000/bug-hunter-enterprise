// Login page. Test contract: name="email"/"password" + submit button are Playwright selectors.
// Strict CSP (no inline scripts/styles); __APP_VERSION__ is substituted server-side — keep verbatim.
import { useEffect, useRef, useState } from "react";
import PasswordInput from "../components/PasswordInput";
import { getAppName, getStaticAssetUrl } from "../lib/branding";
import { loginTarget } from "../lib/safeRedirect.js";

// detail can be a plain string or a Pydantic validation-error array.
function detailMessage(data, fallback) {
  if (typeof data === "object" && data !== null && "detail" in data) {
    const detail = data.detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      return detail.map((e) => e.msg).join(", ");
    }
  }
  return fallback;
}

export default function LoginPage() {
  const appName = getAppName();
  const [view, setView] = useState("login");
  const [pendingToken, setPendingToken] = useState("");
  const [signupEnabled, setSignupEnabled] = useState(false);
  const [totpBusy, setTotpBusy] = useState(false);
  const [loginAlert, setLoginAlert] = useState(null);
  const [forgotAlert, setForgotAlert] = useState(null);
  const [loginBusy, setLoginBusy] = useState(false);
  const [forgotBusy, setForgotBusy] = useState(false);
  const loginEmailRef = useRef(null);
  const forgotEmailRef = useRef(null);
  const totpCodeRef = useRef(null);
  const mountedRef = useRef(false);

  // "Create an organization" is offered only when the server allows public sign-up.
  useEffect(() => {
    fetch("/api/meta")
      .then((r) => (r.ok ? r.json() : null))
      .then((meta) => setSignupEnabled(Boolean(meta?.signup_enabled)))
      .catch(() => {
        /* no link without an answer */
      });
  }, []);

  // Re-focus the email field on view switch; initial focus is handled by autoFocus.
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true;
      return;
    }
    const target = { forgot: forgotEmailRef, totp: totpCodeRef }[view] || loginEmailRef;
    target.current?.focus();
  }, [view]);

  function hideAlerts() {
    setLoginAlert(null);
    setForgotAlert(null);
  }

  function onShowForgot(e) {
    e.preventDefault();
    hideAlerts();
    setView("forgot");
  }

  function onBackToLogin(e) {
    e.preventDefault();
    hideAlerts();
    setView("login");
  }

  function onToggleTheme() {
    const cur = document.documentElement.dataset.theme;
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("theme", next);
  }

  async function onLoginSubmit(e) {
    e.preventDefault();
    hideAlerts();
    const f = e.currentTarget;
    const email = f.elements.namedItem("email").value.trim();
    const password = f.elements.namedItem("password").value;
    setLoginBusy(true);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        let msg = "Sign in failed";
        try {
          msg = detailMessage(await res.json(), msg);
        } catch {
          /* non-JSON error body — keep the fallback message */
        }
        setLoginAlert({ msg, kind: "error" });
        return;
      }
      let data = null;
      try {
        data = await res.json();
      } catch {
        /* an empty body means a plain sign-in */
      }
      if (data?.requires_totp) {
        setPendingToken(data.pending_token);
        setView("totp");
        return;
      }
      // Only a same-origin path is accepted (prevents open redirect).
      location.replace(loginTarget(location.search, location.hash, location.origin));
    } catch {
      setLoginAlert({ msg: "Network error. Try again", kind: "error" });
    } finally {
      setLoginBusy(false);
    }
  }

  async function onTotpSubmit(e) {
    e.preventDefault();
    hideAlerts();
    const code = e.currentTarget.elements.namedItem("code").value.trim();
    setTotpBusy(true);
    try {
      const res = await fetch("/api/auth/login/totp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ pending_token: pendingToken, code }),
      });
      if (!res.ok) {
        let msg = "Verification failed";
        try {
          msg = detailMessage(await res.json(), msg);
        } catch {
          /* non-JSON error body — keep the fallback message */
        }
        setLoginAlert({ msg, kind: "error" });
        return;
      }
      location.replace(loginTarget(location.search, location.hash, location.origin));
    } catch {
      setLoginAlert({ msg: "Network error. Try again", kind: "error" });
    } finally {
      setTotpBusy(false);
    }
  }

  async function onForgotSubmit(e) {
    e.preventDefault();
    hideAlerts();
    const f = e.currentTarget;
    const email = f.elements.namedItem("email").value.trim();
    setForgotBusy(true);
    try {
      const res = await fetch("/api/auth/forgot-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      if (res.status === 204) {
        setForgotAlert({
          msg: "Reset link sent. Check your inbox — the link expires in 30 minutes",
          kind: "success",
        });
      } else {
        let msg = "Request failed";
        try {
          msg = detailMessage(await res.json(), msg);
        } catch {
          /* non-JSON error body — keep the fallback message */
        }
        setForgotAlert({ msg, kind: "error" });
      }
    } catch {
      setForgotAlert({ msg: "Network error. Try again", kind: "error" });
    } finally {
      setForgotBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="auth-logo">
            <img src={getStaticAssetUrl("icon.png")} alt={appName} />
          </div>
          <h1>{appName}</h1>
          <p className="auth-tagline">Sign in to continue</p>
        </div>

        {/* Login form */}
        <form
          id="loginForm"
          className="auth-form"
          noValidate
          hidden={view !== "login"}
          onSubmit={onLoginSubmit}
        >
          <div
            id="loginAlert"
            className={loginAlert ? `auth-alert ${loginAlert.kind}` : "auth-alert"}
            hidden={loginAlert === null || view !== "login"}
          >
            {loginAlert?.msg}
          </div>
          <label className="field">
            <span>
              Email <em>*</em>
            </span>
            <input
              ref={loginEmailRef}
              name="email"
              type="email"
              required
              autoComplete="username"
              autoFocus
            />
          </label>
          <label className="field" htmlFor="passwordInput">
            <span>
              Password <em>*</em>
            </span>
            <PasswordInput
              id="passwordInput"
              name="password"
              required
              autoComplete="current-password"
            />
          </label>
          <button
            type="submit"
            className="btn primary auth-submit"
            id="loginSubmit"
            disabled={loginBusy}
          >
            {loginBusy ? "Signing in…" : "Sign in"}
          </button>
          <div className="auth-links">
            <button
              type="button"
              className="link-btn"
              id="showForgot"
              onClick={onShowForgot}
            >
              Forgot your password?
            </button>
            <button
              type="button"
              className="link-btn"
              id="loginThemeBtn"
              onClick={onToggleTheme}
            >
              <svg
                className="theme-icon"
                viewBox="0 0 16 16"
                xmlns="http://www.w3.org/2000/svg"
                aria-hidden="true"
              >
                <path d="M 8 2 A 6 6 0 0 0 8 14 Z" fill="#0b1020" />
                <path d="M 8 2 A 6 6 0 0 1 8 14 Z" fill="#ffffff" />
                <circle
                  cx="8"
                  cy="8"
                  r="6"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1"
                  strokeOpacity="0.55"
                />
                <line
                  x1="8"
                  y1="2"
                  x2="8"
                  y2="14"
                  stroke="currentColor"
                  strokeWidth="0.6"
                  strokeOpacity="0.4"
                />
              </svg>{" "}
              Theme
            </button>
          </div>
          {signupEnabled && (
            <div className="auth-links">
              <a href="/signup.html" id="showSignup">
                New here? Create an organization
              </a>
            </div>
          )}
        </form>

        {/* Second factor (shown after a correct password when 2FA is on) */}
        <form
          id="totpForm"
          className="auth-form"
          noValidate
          hidden={view !== "totp"}
          onSubmit={onTotpSubmit}
        >
          <div
            className={loginAlert ? `auth-alert ${loginAlert.kind}` : "auth-alert"}
            hidden={loginAlert === null || view !== "totp"}
          >
            {loginAlert?.msg}
          </div>
          <p className="auth-help">
            Enter the 6-digit code from your authenticator app, or one of your recovery codes
          </p>
          <label className="field">
            <span>
              Code <em>*</em>
            </span>
            <input
              ref={totpCodeRef}
              name="code"
              type="text"
              required
              maxLength={20}
              inputMode="text"
              autoComplete="one-time-code"
            />
          </label>
          <button type="submit" className="btn primary auth-submit" disabled={totpBusy}>
            {totpBusy ? "Verifying…" : "Verify"}
          </button>
          <div className="auth-links">
            <button type="button" className="link-btn" onClick={onBackToLogin}>
              ← Back to sign in
            </button>
          </div>
        </form>

        {/* Forgot password form (toggled) */}
        <form
          id="forgotForm"
          className="auth-form"
          noValidate
          hidden={view !== "forgot"}
          onSubmit={onForgotSubmit}
        >
          <div
            id="forgotAlert"
            className={forgotAlert ? `auth-alert ${forgotAlert.kind}` : "auth-alert"}
            hidden={forgotAlert === null}
          >
            {forgotAlert?.msg}
          </div>
          <p className="auth-help">
            Enter the email tied to your {appName} account and we&apos;ll send a reset link
          </p>
          <label className="field">
            <span>
              Email <em>*</em>
            </span>
            <input
              ref={forgotEmailRef}
              name="email"
              type="email"
              required
              autoComplete="username"
            />
          </label>
          <button type="submit" className="btn primary auth-submit" disabled={forgotBusy}>
            {forgotBusy ? "Sending…" : "Send reset link"}
          </button>
          <div className="auth-links">
            <button type="button" className="link-btn" id="backToLogin" onClick={onBackToLogin}>
              ← Back to sign in
            </button>
          </div>
        </form>
      </div>
      {/* Version string lives in login.html — bundle would emit __APP_VERSION__ verbatim. */}
    </main>
  );
}
