// Outbound webhooks (admins): create, pause, test, rotate the signing secret, delete.
import { useCallback, useEffect, useState } from "react";
import { confirmDialog } from "../components/ConfirmHost";
import { api } from "../lib/api";
import { formatDate } from "../lib/format";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";

const EVENT_HELP =
  "Comma-separated: * for everything, or names such as bug.created, bug.*, comment.added";

function SecretNotice({ hook, onDone }) {
  return (
    <div className="ent-recovery" role="alert">
      <p>
        <strong>Signing secret for {hook.name}.</strong> Copy it now; it is not shown again. Verify each
        delivery with the <code>X-BugHunter-Signature</code> header (HMAC-SHA256 of the body).
      </p>
      <code className="ent-secret">{hook.secret}</code>
      <div className="ent-actions">
        <button type="button" className="btn ghost" onClick={() => navigator.clipboard?.writeText(hook.secret)}>
          Copy
        </button>
        <button type="button" className="btn primary" onClick={onDone}>
          I saved it
        </button>
      </div>
    </div>
  );
}

export default function WebhooksTab() {
  const [hooks, setHooks] = useState(null);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState("*");
  const [secretOf, setSecretOf] = useState(null);

  const load = useCallback(async () => {
    try {
      setHooks(await api("/webhooks"));
    } catch (err) {
      toastError(err);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  async function create(e) {
    e.preventDefault();
    try {
      const made = await withLoader(async () => {
        const res = await api("/webhooks", { method: "POST", json: { name: name.trim(), url: url.trim(), events } });
        await load();
        return res;
      }, "Creating…");
      setSecretOf(made);
      setName("");
      setUrl("");
      setEvents("*");
    } catch (err) {
      toastError(err);
    }
  }

  async function run(label, fn, okMessage) {
    try {
      await withLoader(async () => {
        await fn();
        await load();
      }, label);
      if (okMessage) toast(okMessage, "success");
    } catch (err) {
      toastError(err);
    }
  }

  async function rotate(hook) {
    const ok = await confirmDialog(`Rotate the signing secret of ${hook.name}? Listeners using the old one will fail verification.`, {
      title: "Rotate secret",
      okLabel: "Rotate",
      danger: true,
    });
    if (!ok) return;
    try {
      setSecretOf(await withLoader(() => api(`/webhooks/${hook.id}/rotate-secret`, { method: "POST" }), "Rotating…"));
    } catch (err) {
      toastError(err);
    }
  }

  async function remove(hook) {
    const ok = await confirmDialog(`Delete the webhook ${hook.name}?`, {
      title: "Delete webhook",
      okLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    await run("Deleting…", () => api(`/webhooks/${hook.id}`, { method: "DELETE" }), "Webhook deleted");
  }

  return (
    <>
      {secretOf && <SecretNotice hook={secretOf} onDone={() => setSecretOf(null)} />}

      <form className="ent-section" noValidate onSubmit={create}>
        <h3>Add a webhook</h3>
        <label className="field">
          <span>
            Name <em>*</em>
          </span>
          <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} />
        </label>
        <label className="field">
          <span>
            URL <em>*</em>
          </span>
          <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} required
            placeholder="https://example.com/hooks/bug-hunter" />
        </label>
        <label className="field">
          <span>Events</span>
          <input value={events} onChange={(e) => setEvents(e.target.value)} maxLength={500} />
        </label>
        <p className="hint">{EVENT_HELP}. Private and local addresses are refused.</p>
        <div className="ent-actions">
          <button type="submit" className="btn primary" disabled={!name.trim() || !url.trim()}>
            Create webhook
          </button>
        </div>
      </form>

      <div className="ent-section">
        <h3>Webhooks</h3>
        {hooks === null && <p className="hint">Loading…</p>}
        {hooks?.length === 0 && <p className="hint">No webhooks yet.</p>}
        {hooks?.map((h) => (
          <div key={h.id} className="ent-hook">
            <div className="ent-hook-main">
              <strong>{h.name}</strong>
              <span className={`ent-status ent-status-${h.is_active ? "pending" : "revoked"}`}>
                {h.is_active ? "active" : "paused"}
              </span>
              <div className="hint">
                {h.url} · {h.events}
              </div>
              <div className="hint">
                {h.last_delivered_at
                  ? `Last delivery ${formatDate(h.last_delivered_at)} · ${h.last_status_code ?? "no response"}`
                  : "Never delivered"}
                {h.last_error ? ` · ${h.last_error}` : ""}
                {h.consecutive_failures > 0 ? ` · ${h.consecutive_failures} failure(s) in a row` : ""}
              </div>
            </div>
            <div className="ent-hook-actions">
              <button type="button" className="btn ghost"
                onClick={() => void run("Sending…", () => api(`/webhooks/${h.id}/test`, { method: "POST" }), "Test ping queued")}>
                Test
              </button>
              <button type="button" className="btn ghost"
                onClick={() => void run("Saving…", () => api(`/webhooks/${h.id}`, { method: "PUT", json: { is_active: !h.is_active } }))}>
                {h.is_active ? "Pause" : "Resume"}
              </button>
              <button type="button" className="btn ghost" onClick={() => void rotate(h)}>
                Rotate secret
              </button>
              <button type="button" className="btn danger" onClick={() => void remove(h)}>
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
