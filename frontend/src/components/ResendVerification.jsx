// "Send a new link" for an unconfirmed email address, with a cooldown (the server allows one a minute).
// With `email` it is a single button; without it, a field asks which address to confirm.
import { useState } from "react";
import { useCooldown } from "../lib/useCooldown";
import { errorMessage, sendJson } from "../lib/publicApi";

export const RESEND_COOLDOWN_S = 60;

export default function ResendVerification({ email = "", onAlert }) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [left, start] = useCooldown(RESEND_COOLDOWN_S);
  const address = (email || typed).trim();

  async function send(e) {
    e.preventDefault();
    if (!address || busy || left > 0) return;
    setBusy(true);
    try {
      const res = await sendJson("/auth/resend-verification", { json: { email: address } });
      if (res.ok) {
        start();
        onAlert?.({ msg: "If that address is waiting for confirmation, a new link is on its way.", kind: "success" });
      } else {
        onAlert?.({ msg: errorMessage(res.data, "Could not send a new link"), kind: "error" });
      }
    } catch {
      onAlert?.({ msg: "Network error. Try again", kind: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="auth-form" noValidate onSubmit={send} id="resendForm">
      {!email && (
        <label className="field">
          <span>Email</span>
          <input name="email" type="email" required maxLength={254} autoComplete="username" value={typed}
            onChange={(e) => setTyped(e.target.value)} />
        </label>
      )}
      <button type="submit" className="btn auth-submit" disabled={busy || left > 0 || !address}>
        {busy ? "Sending..." : left > 0 ? `Send a new link (${left}s)` : "Send a new link"}
      </button>
      <p className="auth-help" role="status" aria-live="polite">
        {left > 0 ? `You can ask for another link in ${left} seconds.` : ""}
      </p>
    </form>
  );
}
