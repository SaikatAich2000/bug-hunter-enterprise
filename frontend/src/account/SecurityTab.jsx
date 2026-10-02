// Password and two-factor authentication (authenticator app + one-time recovery codes).
import { useCallback, useEffect, useState } from "react";
import PasswordInput from "../components/PasswordInput";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { QrCode } from "../lib/qr";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";

function RecoveryCodes({ codes, onDone }) {
  return (
    <div className="ent-recovery" role="alert">
      <p>
        <strong>Save these recovery codes now.</strong> Each works once if you lose your authenticator
        app. They are not shown again.
      </p>
      <ul className="ent-codes">
        {codes.map((c) => (
          <li key={c}>
            <code>{c}</code>
          </li>
        ))}
      </ul>
      <div className="ent-actions">
        <button type="button" className="btn ghost" onClick={() => navigator.clipboard?.writeText(codes.join("\n"))}>
          Copy
        </button>
        <button type="button" className="btn primary" onClick={onDone}>
          I saved them
        </button>
      </div>
    </div>
  );
}

export default function SecurityTab() {
  const { setAccountOpen, setChangePasswordOpen, refreshMe } = useApp();
  const [status, setStatus] = useState(null);
  const [enrolment, setEnrolment] = useState(null); // { secret, otpauth_uri }
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [recovery, setRecovery] = useState(null);

  const load = useCallback(async () => {
    try {
      setStatus(await api("/auth/2fa/status"));
    } catch (err) {
      toastError(err);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  async function begin() {
    try {
      setEnrolment(await withLoader(() => api("/auth/2fa/begin", { method: "POST" }), "Preparing…"));
    } catch (err) {
      toastError(err);
    }
  }

  async function confirm(e) {
    e.preventDefault();
    try {
      const res = await withLoader(
        () => api("/auth/2fa/confirm", { method: "POST", json: { code: code.trim() } }),
        "Verifying…",
      );
      setRecovery(res.recovery_codes);
      setEnrolment(null);
      setCode("");
      await Promise.all([load(), refreshMe()]);
      toast("Two-factor authentication is on", "success");
    } catch (err) {
      toastError(err);
    }
  }

  async function disable(e) {
    e.preventDefault();
    try {
      await withLoader(() => api("/auth/2fa/disable", { method: "POST", json: { password } }), "Turning off…");
      setPassword("");
      await Promise.all([load(), refreshMe()]);
      toast("Two-factor authentication is off", "success");
    } catch (err) {
      toastError(err);
    }
  }

  async function regenerate() {
    try {
      const res = await withLoader(
        () => api("/auth/2fa/recovery-codes/regenerate", { method: "POST", json: { password } }),
        "Generating…",
      );
      setPassword("");
      setRecovery(res.recovery_codes);
      await load();
    } catch (err) {
      toastError(err);
    }
  }

  return (
    <>
      <div className="ent-section">
        <h3>Password</h3>
        <p className="hint">Changing it signs out every other device.</p>
        <div className="ent-actions">
          <button
            type="button"
            className="btn"
            onClick={() => {
              setAccountOpen(false);
              setChangePasswordOpen(true);
            }}
          >
            Change password…
          </button>
        </div>
      </div>

      <div className="ent-section">
        <h3>Two-factor authentication</h3>
        {status === null && <p className="hint">Loading…</p>}
        {status && !status.available && (
          <p className="hint">Two-factor authentication is turned off on this server.</p>
        )}

        {recovery && <RecoveryCodes codes={recovery} onDone={() => setRecovery(null)} />}

        {status?.available && !status.enabled && !enrolment && (
          <>
            <p className="hint">
              Require a code from an authenticator app (Google Authenticator, Authy, 1Password, …)
              in addition to your password.
            </p>
            <div className="ent-actions">
              <button type="button" className="btn primary" onClick={() => void begin()}>
                Enable 2FA
              </button>
            </div>
          </>
        )}

        {enrolment && (
          <form noValidate onSubmit={confirm}>
            <p className="hint">
              Scan this code with your authenticator app, then enter the 6-digit code it shows.
            </p>
            <QrCode text={enrolment.otpauth_uri} label="Authenticator setup QR code" />
            <p className="hint">
              Cannot scan? Enter this key by hand: <code>{enrolment.secret}</code>
            </p>
            <label className="field">
              <span>
                Code <em>*</em>
              </span>
              <input value={code} onChange={(e) => setCode(e.target.value)} required maxLength={10}
                inputMode="numeric" autoComplete="one-time-code" />
            </label>
            <div className="ent-actions">
              <button type="button" className="btn ghost" onClick={() => setEnrolment(null)}>
                Cancel
              </button>
              <button type="submit" className="btn primary" disabled={code.trim().length < 6}>
                Turn on
              </button>
            </div>
          </form>
        )}

        {status?.enabled && (
          <form noValidate onSubmit={disable}>
            <p className="hint">
              Two-factor authentication is <strong>on</strong>. {status.unused_recovery_codes} recovery code(s) left.
            </p>
            <label className="field" htmlFor="securityPassword">
              <span>Password (needed to change this)</span>
              <PasswordInput id="securityPassword" value={password}
                onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            </label>
            <div className="ent-actions">
              <button type="button" className="btn" disabled={!password} onClick={() => void regenerate()}>
                New recovery codes
              </button>
              <button type="submit" className="btn danger" disabled={!password}>
                Turn off 2FA
              </button>
            </div>
          </form>
        )}
      </div>
    </>
  );
}
