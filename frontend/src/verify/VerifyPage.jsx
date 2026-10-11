// Email verification: opens from the mailed link and shows one of four states: waiting for the link
// (no token), confirming, confirmed, or a link that no longer works (with a way to get a fresh one).
import { useEffect, useRef, useState } from "react";
import AuthCard from "../components/AuthCard";
import ResendVerification from "../components/ResendVerification";
import { errorMessage, sendJson } from "../lib/publicApi";

function StatusIcon({ kind }) {
  return (
    <div className={`auth-status ${kind}`} aria-hidden="true">
      {kind === "ok" ? "✓" : kind === "bad" ? "!" : "…"}
    </div>
  );
}

export default function VerifyPage() {
  const [token] = useState(() => new URLSearchParams(location.search).get("token") || "");
  // "waiting" (no token: check your inbox) | "checking" | "verified" | "failed"
  const [state, setState] = useState(token ? "checking" : "waiting");
  const [reason, setReason] = useState("");
  const [alert, setAlert] = useState(null);
  const headingRef = useRef(null);

  useEffect(() => {
    if (!token) return undefined;
    let cancelled = false;
    sendJson("/auth/verify-email", { json: { token } })
      .then((res) => {
        if (cancelled) return;
        if (res.ok) setState("verified");
        else {
          setReason(errorMessage(res.data, "This link is not valid anymore."));
          setState("failed");
        }
      })
      .catch(() => {
        if (cancelled) return;
        setReason("Network error. Check your connection and reload this page.");
        setState("failed");
      });
    return () => { cancelled = true; };
  }, [token]);

  // Each outcome replaces the last: move focus to its heading so it is announced and keyboard users start there.
  useEffect(() => {
    if (state !== "checking") headingRef.current?.focus({ preventScroll: true });
  }, [state]);

  return (
    <AuthCard title="Confirm your email" alert={alert}>
      <div className="auth-step" data-dir="fwd" key={state} aria-busy={state === "checking"}>
        {state === "checking" && (
          <>
            <StatusIcon kind="wait" />
            <h2 className="auth-step-title" role="status">Confirming your email address...</h2>
          </>
        )}
        {state === "verified" && (
          <>
            <StatusIcon kind="ok" />
            <h2 className="auth-step-title" tabIndex={-1} ref={headingRef}>Your email is confirmed</h2>
            <p className="auth-help">You can sign in now.</p>
            <a className="btn primary auth-submit auth-cta" href="/login.html" id="verifySignIn">Sign in</a>
          </>
        )}
        {state === "failed" && (
          <>
            <StatusIcon kind="bad" />
            <h2 className="auth-step-title" tabIndex={-1} ref={headingRef}>This link did not work</h2>
            <p className="auth-help" id="verifyReason">{reason} Links work once and expire, so ask for a new one.</p>
            <ResendVerification onAlert={setAlert} />
          </>
        )}
        {state === "waiting" && (
          <>
            <StatusIcon kind="wait" />
            <h2 className="auth-step-title" tabIndex={-1} ref={headingRef}>Check your inbox</h2>
            <p className="auth-help">
              We emailed you a link to confirm your address. Open it on this device to finish. If it has not
              arrived, check your spam folder or ask for a new one.
            </p>
            <ResendVerification onAlert={setAlert} />
          </>
        )}
      </div>
      <div className="auth-links">
        <a href="/login.html">Back to sign in</a>
      </div>
    </AuthCard>
  );
}
