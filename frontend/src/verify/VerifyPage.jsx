// Email verification: opens from the mailed link, confirms it, and offers a fresh link when the
// old one is gone. (The animated multi-step sign-up wraps this later.)
import { useEffect, useState } from "react";
import AuthCard from "../components/AuthCard";
import { errorMessage, sendJson } from "../lib/publicApi";

export default function VerifyPage() {
  const [token] = useState(() => new URLSearchParams(location.search).get("token") || "");
  const sent = new URLSearchParams(location.search).get("sent") === "1";
  const [alert, setAlert] = useState(
    token
      ? { msg: "Confirming your email address…", kind: "success" }
      : sent
        ? { msg: "Check your inbox: we sent you a link to confirm your address.", kind: "success" }
        : null,
  );
  const [verified, setVerified] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) return;
    sendJson("/auth/verify-email", { json: { token } })
      .then((res) => {
        if (res.ok) {
          setVerified(true);
          setAlert({ msg: "Your email is confirmed. You can sign in now.", kind: "success" });
        } else {
          setAlert({ msg: errorMessage(res.data, "This link is not valid anymore"), kind: "error" });
        }
      })
      .catch(() => setAlert({ msg: "Network error. Try again", kind: "error" }));
  }, [token]);

  async function onResend(e) {
    e.preventDefault();
    const email = e.currentTarget.elements.namedItem("email").value.trim();
    setBusy(true);
    try {
      const res = await sendJson("/auth/resend-verification", { json: { email } });
      setAlert(
        res.ok
          ? { msg: "If that address is waiting for confirmation, a new link is on its way.", kind: "success" }
          : { msg: errorMessage(res.data, "Could not send a new link"), kind: "error" },
      );
    } catch {
      setAlert({ msg: "Network error. Try again", kind: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Confirm your email" alert={alert}>
      {verified ? (
        <div className="auth-links">
          <a href="/login.html">Sign in</a>
        </div>
      ) : (
        <form className="auth-form" noValidate onSubmit={onResend}>
          <label className="field">
            <span>Email</span>
            <input name="email" type="email" required maxLength={254} autoComplete="username" />
          </label>
          <button type="submit" className="btn primary auth-submit" disabled={busy}>
            {busy ? "Sending…" : "Send a new link"}
          </button>
        </form>
      )}
    </AuthCard>
  );
}
