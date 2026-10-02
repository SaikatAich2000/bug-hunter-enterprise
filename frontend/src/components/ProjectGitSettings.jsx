import { useCallback, useEffect, useRef, useState } from "react";
import BhSelect from "./BhSelect";
import { api } from "../lib/api";
import { toast, toastError } from "../lib/toast";
import { confirmDialog } from "./ConfirmHost";

const AUTH_NOT_CONFIGURED = "GitHub authentication is not configured on the server.";

function checkedAt(value) {
  return value ? new Date(value).toLocaleString() : "Never";
}

function statusText(config) {
  if (!config.credentials_configured) return AUTH_NOT_CONFIGURED;
  if (!config.enabled) return "Git integration is disabled for this project.";
  if (!config.branch_creation_enabled) return "Branch creation is disabled by the server.";
  return "GitHub authentication is configured on the server.";
}

function RepositoryCard({ repository }) {
  return (
    <div className="project-git-repository">
      <div className="project-git-repository-head">
        <div>
          <strong>{repository.full_name || repository.name}</strong>
          <div className="muted small">
            {repository.owner ? `${repository.owner}/${repository.name}` : repository.name}
            {repository.default_branch ? ` · default: ${repository.default_branch}` : ""}
          </div>
        </div>
        {repository.url && (
          <a href={repository.url} target="_blank" rel="noopener noreferrer">Open GitHub</a>
        )}
      </div>
    </div>
  );
}

export default function ProjectGitSettings({ projectId }) {
  const [config, setConfig] = useState(null);
  const [available, setAvailable] = useState([]);
  const [availableLoaded, setAvailableLoaded] = useState(false);
  const [availableBusy, setAvailableBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [configDraft, setConfigDraft] = useState(null);
  // Write-only PAT input. It lives in component state only for as long as the
  // input itself, is cleared after every successful submit, and is never put in
  // localStorage/sessionStorage or in any state derived from a response.
  const [credentialDraft, setCredentialDraft] = useState("");
  // Result of the most recent Test Connection. Held separately from `config` so a
  // draft test never mutates (or appears to mutate) the saved configuration.
  const [connectionResult, setConnectionResult] = useState(null);
  // Test Connection owns its own busy flag, so it never nests inside the page's
  // shared `busy` ownership or blocks/cancels a save that is already running.
  const [testing, setTesting] = useState(false);
  const loadGeneration = useRef(0);

  const load = useCallback(async () => {
    if (!projectId) return;
    const generation = ++loadGeneration.current;
    setLoading(true);
    try {
      const nextConfig = await api(`/git/projects/${projectId}/config`);
      if (generation !== loadGeneration.current) return;
      setConfig(nextConfig);
      setConfigDraft({
        enabled: nextConfig.enabled,
        organization: nextConfig.organization,
        base_url: nextConfig.base_url,
        default_base_branch: nextConfig.default_base_branch,
      });
    } catch (error) {
      if (generation !== loadGeneration.current) return;
      toastError(error);
    } finally {
      if (generation === loadGeneration.current) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    loadGeneration.current += 1;
    setConfig(null);
    setConfigDraft(null);
    setAvailable([]);
    setAvailableLoaded(false);
    setAvailableBusy(false);
    // A token typed against a previous project must never carry over.
    setCredentialDraft("");
    setConnectionResult(null);
    setTesting(false);
    void load();
  }, [load]);

  // The exact body both Save and Test Connection send: the visible draft plus an
  // optional newly typed PAT. A blank PAT field is omitted entirely, which is
  // what tells the backend to keep the stored project credential (and then to
  // fall back to the deployment token). The token is never read back from state
  // or from a response — it only ever travels outward, once.
  function configPayload(extra = {}) {
    return {
      ...configDraft,
      version: config?.configured ? config.version : null,
      ...(credentialDraft.trim() ? { credential: credentialDraft.trim() } : {}),
      ...extra,
    };
  }

  function applySavedConfig(updated) {
    setConfig(updated);
    setConfigDraft({
      enabled: updated.enabled,
      organization: updated.organization,
      base_url: updated.base_url,
      default_base_branch: updated.default_base_branch,
    });
    // Clear the token input the moment the request succeeds: the secret exists
    // only for as long as the user is typing it, and the masked
    // "Credential configured" state is all that is shown afterwards.
    setCredentialDraft("");
  }

  async function saveConfig() {
    // Duplicate-submission guard: one in-flight save at a time.
    if (!config || !configDraft || busy || testing) return;
    setBusy(true);
    try {
      const updated = await api(`/git/projects/${projectId}/config`, {
        method: "PUT",
        json: configPayload(),
      });
      applySavedConfig(updated);
      setConnectionResult(null);
      toast("Git integration settings saved", "success");
      return updated;
    } catch (error) {
      toastError(error);
      if (error?.status === 409) await load();
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function clearCredential() {
    if (!config || !configDraft || busy || testing) return;
    const confirmed = await confirmDialog(
      "Remove the stored credential for this project? The deployment-wide token, if one is configured, will be used instead.",
      { title: "Clear credential", okLabel: "Clear credential", danger: true },
    );
    if (!confirmed) return;
    setBusy(true);
    try {
      const updated = await api(`/git/projects/${projectId}/config`, {
        method: "PUT",
        json: configPayload({ credential: null, clear_credential: true }),
      });
      applySavedConfig(updated);
      setConnectionResult(null);
      toast("Git credential cleared", "success");
    } catch (error) {
      toastError(error);
      if (error?.status === 409) await load();
    } finally {
      setBusy(false);
    }
  }

  async function testConnection() {
    if (!configDraft || testing || busy) return;
    setTesting(true);
    try {
      // Test EXACTLY what is on screen. Nothing is saved first, no saved value is
      // substituted for a draft value, and the draft is not refreshed afterwards:
      // the backend answers a draft test without persisting it, and only the
      // connection result below changes.
      const result = await api(`/git/projects/${projectId}/config/test`, {
        method: "POST",
        json: configPayload(),
      });
      setConnectionResult(result);
      setCredentialDraft("");
      toast(result.message, result.ok ? "success" : "error");
    } catch (error) {
      toastError(error);
    } finally {
      setTesting(false);
    }
  }

  async function loadAvailable() {
    if (availableBusy) return;
    setAvailableBusy(true);
    try {
      const rows = await api(`/git/projects/${projectId}/available-repositories`);
      setAvailable(rows);
      setAvailableLoaded(true);
      if (!rows.length) toast("No GitHub repositories are available", "info");
    } catch (error) {
      setAvailableLoaded(false);
      toastError(error);
    } finally {
      setAvailableBusy(false);
    }
  }

  if (loading && !config) return <section className="project-git-settings"><p className="muted">Loading Git integration…</p></section>;
  if (!config || !configDraft) return <section className="project-git-settings"><p className="muted">Git integration settings could not be loaded.</p></section>;

  // A draft test is worth allowing whenever a credential could exist: either one
  // is already stored, or the operator has just typed a replacement.
  const canTest = Boolean(config.credentials_configured) || credentialDraft.trim().length > 0;

  return (
    <section className="project-git-settings" aria-labelledby={`project-git-title-${projectId}`}>
      <h3 id={`project-git-title-${projectId}`}>Git Integration</h3>
      <p className={`project-git-auth ${config.credentials_configured ? "is-ok" : "is-error"}`}>{statusText(config)}</p>
      <div className="project-git-grid">
        <label className="field" htmlFor={`project-git-enabled-${projectId}`}>
          <span>Integration</span>
          <BhSelect
            id={`project-git-enabled-${projectId}`}
            ariaLabel="Git integration enabled"
            value={configDraft.enabled ? "enabled" : "disabled"}
            disabled={busy || testing}
            onChange={(value) => setConfigDraft((current) => ({ ...current, enabled: value === "enabled" }))}
            options={[{ value: "disabled", label: "Disabled" }, { value: "enabled", label: "Enabled" }]}
          />
        </label>
        <label className="field" htmlFor={`project-git-org-${projectId}`}>
          <span>Organization</span>
          <input id={`project-git-org-${projectId}`} value={configDraft.organization} maxLength={120} disabled={busy || testing} onChange={(event) => setConfigDraft((current) => ({ ...current, organization: event.target.value }))} />
        </label>
        <label className="field" htmlFor={`project-git-url-${projectId}`}>
          <span>API / base URL</span>
          <input id={`project-git-url-${projectId}`} value={configDraft.base_url} maxLength={500} disabled={busy || testing} onChange={(event) => setConfigDraft((current) => ({ ...current, base_url: event.target.value }))} />
        </label>
        <label className="field" htmlFor={`project-git-base-${projectId}`}>
          <span>Default base branch</span>
          <input id={`project-git-base-${projectId}`} value={configDraft.default_base_branch} maxLength={120} disabled={busy || testing} onChange={(event) => setConfigDraft((current) => ({ ...current, default_base_branch: event.target.value }))} />
        </label>
        <label className="field" htmlFor={`project-git-credential-${projectId}`}>
          <span>Access token</span>
          <input
            id={`project-git-credential-${projectId}`}
            type="password"
            value={credentialDraft}
            autoComplete="new-password"
            spellCheck={false}
            maxLength={512}
            disabled={busy || testing}
            placeholder={config.credentials_configured ? "Leave blank to keep the stored credential" : "Paste a personal access token"}
            onChange={(event) => setCredentialDraft(event.target.value)}
          />
          <span className="muted small">
            {config.credentials_configured && !credentialDraft
              ? "Credential configured — type a token here to replace it."
              : "Leave blank to keep the stored credential. The token is never displayed, returned or logged."}
          </span>
        </label>
      </div>
      <div className="project-git-status muted small">
        <span>Saved connection: <strong>{config.connection_status}</strong></span>
        <span>Last checked: <strong>{checkedAt(config.last_checked_at)}</strong></span>
        {config.last_error && <span>Last error: <strong>{config.last_error}</strong></span>}
        {connectionResult && (
          <span>
            Test result: <strong>{connectionResult.ok ? "ok" : "error"}</strong>
            {connectionResult.target ? ` (${connectionResult.target})` : ""}
            {connectionResult.message ? ` — ${connectionResult.message}` : ""}
          </span>
        )}
      </div>
      <div className="project-git-actions">
        <button type="button" className="btn primary" disabled={busy || testing} onClick={() => void saveConfig()}>Save Git settings</button>
        <button type="button" className="btn ghost" disabled={testing || busy || !canTest} onClick={() => void testConnection()}>Test Connection</button>
        <button type="button" className="btn ghost" disabled={busy || testing || !config.credentials_configured} onClick={() => void clearCredential()}>Clear credential</button>
      </div>

      <div className="project-git-repositories">
        <div className="project-git-section-head">
          <h4>Accessible repositories</h4>
          <button type="button" className="btn ghost" disabled={busy || testing || availableBusy || !config.credentials_configured} onClick={() => void loadAvailable()}>
            {availableBusy ? "Loading…" : "Load repositories"}
          </button>
        </div>
        {availableLoaded && available.length === 0 && (
          <p className="no-content">No GitHub repositories are available.</p>
        )}
        {available.length > 0 ? available.map((repository) => (
          <RepositoryCard key={repository.provider_repo_id || repository.full_name} repository={repository} />
        )) : (!availableLoaded && (
          <p className="muted small">Repositories are loaded from live provider discovery when you need them.</p>
        ))}
      </div>
    </section>
  );
}
