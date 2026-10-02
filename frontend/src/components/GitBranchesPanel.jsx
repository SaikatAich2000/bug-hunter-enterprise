import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { toast, toastError } from "../lib/toast";
import BhSelect from "./BhSelect";
import { confirmDialog } from "./ConfirmHost";
import {
  branchListEndpoint,
  buildBranchCreateBody,
  buildBranchRemovalRequest,
  buildPreviewQuery,
  canCreateBranch,
  canRemoveBranches,
  isRemovedBranch,
  partitionBranches,
  removalConfirmationMessage,
  removableBranches,
} from "../lib/gitBranchRequests";

function shortSha(value) {
  return value ? value.slice(0, 8) : "";
}

export default function GitBranchesPanel({ targetType = "work-item", targetId, visible = true, itemType = "" }) {
  const [data, setData] = useState(null);
  const [providerRepos, setProviderRepos] = useState(null);
  const [providerRepoId, setProviderRepoId] = useState("");
  const [baseBranch, setBaseBranch] = useState("");
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [reposLoading, setReposLoading] = useState(false);
  const [reposError, setReposError] = useState("");
  const loadGeneration = useRef(0);
  const reposLoaded = useRef(false);
  const endpoint = targetId ? branchListEndpoint(targetId) : "";

  const load = useCallback(async () => {
    if (!endpoint || !visible) return;
    const generation = ++loadGeneration.current;
    setLoading(true);
    try {
      const next = await api(endpoint);
      if (generation !== loadGeneration.current) return;
      setData(next);
    } catch (error) {
      if (generation !== loadGeneration.current) return;
      setData(null);
      toastError(error);
    } finally {
      if (generation === loadGeneration.current) setLoading(false);
    }
  }, [endpoint, visible]);

  useEffect(() => {
    loadGeneration.current += 1;
    setData(null);
    setProviderRepos(null);
    setProviderRepoId("");
    setBaseBranch("");
    setPreview(null);
    setBusy(false);
    setReposError("");
    reposLoaded.current = false;
    if (endpoint && visible) void load();
  }, [endpoint, visible, load]);

  // Repositories are loaded from live provider discovery, once, when the user
  // starts the create action — never on render, never from an allow-list.
  async function loadProviderRepositories() {
    if (!data?.project_id || reposLoaded.current) return;
    reposLoaded.current = true;
    setReposLoading(true);
    setReposError("");
    try {
      const repos = await api(`/git/projects/${data.project_id}/available-repositories`);
      setProviderRepos(repos);
      if (repos.length) {
        setProviderRepoId(repos[0].provider_repo_id);
        setBaseBranch(repos[0].default_branch || "");
      }
    } catch (error) {
      reposLoaded.current = false;
      setReposError(error?.message || "Repositories could not be loaded.");
    } finally {
      setReposLoading(false);
    }
  }

  const selectedRepo = providerRepos?.find(
    (repo) => repo.provider_repo_id === providerRepoId
  );

  async function showPreview() {
    if (busy || !providerRepoId || !endpoint) return;
    setBusy(true);
    try {
      const params = buildPreviewQuery({ providerRepoId, baseBranch });
      const result = await api(`${endpoint}/preview?${params}`);
      setPreview(result);
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  }

  async function createBranch() {
    if (busy || !providerRepoId || !endpoint || !preview?.can_create) return;
    setBusy(true);
    try {
      const created = await api(endpoint, {
        method: "POST",
        // Create from exactly the base the confirmation showed: the preview
        // resolved a blank field to the project default, which can differ
        // from the repository's own default branch.
        json: buildBranchCreateBody({
          providerRepoId,
          baseBranch: preview.base_branch || baseBranch,
          fallbackBaseBranch: selectedRepo?.default_branch,
        }),
      });
      toast(created?.branch_name ? `Branch ${created.branch_name} is ready` : "Git branch is ready", "success");
      setPreview(null);
      await load();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  }

  // Per-row removal: exact record id + opened version travel; the row
  // stays visible as Deleted history after a successful removal.
  async function confirmAndRemove(branch) {
    const confirmed = await confirmDialog(
      removalConfirmationMessage(branch),
      { title: "Remove Feature Branch", okLabel: "Remove", danger: true },
    );
    if (confirmed) void removeBranch(branch);
  }

  async function removeBranch(branch) {
    if (busy) return;
    setBusy(true);
    try {
      // Exact-removal contract: record id + opened version only.
      const { url, init } = buildBranchRemovalRequest(branch);
      await api(url, init);
      toast("Feature branch removed", "success");
      await load();
    } catch (error) {
      toastError(error);
    } finally {
      setBusy(false);
    }
  }

  const branches = data?.branches ?? [];
  const orderedBranches = partitionBranches(branches);
  const canCreate = canCreateBranch({ itemType, data });
  const reason = data?.reason || "";
  const removable = removableBranches(branches);
  const canRemove = removable.length > 0 && canRemoveBranches({ itemType, data });

  function renderBranchRows() {
    if (branches.length === 0) {
      return <p className="no-content">No Git branches yet</p>;
    }
    return (
      <div className="git-branch-list">
        {orderedBranches.map((branch) => {
          const isRemoved = isRemovedBranch(branch);
          return (
            <div
              className={`git-branch-row${isRemoved ? " is-removed" : ""}`}
              key={branch.id}
            >
              <div>
                <a href={branch.branch_url} target="_blank" rel="noopener noreferrer">
                  {branch.branch_name}
                </a>
                <div className="muted small">
                  {branch.repository_full_name || branch.repository_name}
                  {branch.base_commit_sha ? ` · ${shortSha(branch.base_commit_sha)}` : ""}
                </div>
              </div>
              <div className="git-branch-row-side">
                <span className="badge" data-branch-status={branch.status}>{branch.status}</span>
                {!isRemoved && canRemove && (
                  <button
                    type="button"
                    className="btn danger git-branch-remove-btn"
                    disabled={busy}
                    onClick={() => void confirmAndRemove(branch)}
                    aria-label={`Remove feature branch ${branch.branch_name}`}
                  >
                    Remove
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  function renderLoadedBody() {
    return (
      <>
        {renderBranchRows()}
        {renderCreateSection()}
        {reposError ? (
          <p className="muted small git-branch-reason" role="alert">{reposError}</p>
        ) : null}
      </>
    );
  }

  function renderBody() {
    if (loading && !data) {
      return <p className="muted small">Loading Git branches…</p>;
    }
    return renderLoadedBody();
  }

  function handleRepositoryChange(value) {
    setProviderRepoId(value);
    setBaseBranch(
      providerRepos.find((repo) => repo.provider_repo_id === value)?.default_branch || ""
    );
    setPreview(null);
  }

  function handleBaseBranchChange(event) {
    setBaseBranch(event.target.value);
    setPreview(null);
  }

  function renderLoadButton() {
    return (
      <button
        type="button"
        className="btn primary"
        disabled={busy || reposLoading}
        onClick={() => void loadProviderRepositories()}
      >
        {reposLoading ? "Loading repositories…" : "Create Feature Branch"}
      </button>
    );
  }

  function renderNoReposNotice() {
    return (
      <p className="muted small">
        No repositories are accessible with this project&apos;s Git credential.
      </p>
    );
  }

  function renderPreviewStep() {
    if (!preview) {
      return (
        <button type="button" className="btn primary" disabled={busy || !providerRepoId} onClick={() => void showPreview()}>
          {busy ? "Checking…" : "Create Feature Branch"}
        </button>
      );
    }
    return (
      <div className="git-branch-confirm" aria-label="Confirm Git branch creation">
        <div>
          <strong aria-label={`Feature branch ${preview.branch_name}. Full User Story title: ${preview.work_item_title || ""}`}>{preview.branch_name}</strong>
          <div className="muted small" title={preview.work_item_title || undefined}>From {preview.repository_full_name}:{preview.base_branch}</div>
        </div>
        {preview.can_create ? (
          <div className="git-branch-confirm-actions">
            <button type="button" className="btn ghost" disabled={busy} onClick={() => setPreview(null)}>Cancel</button>
            <button type="button" className="btn primary" disabled={busy} onClick={() => void createBranch()}>
              {busy ? "Creating…" : "Create Feature Branch"}
            </button>
          </div>
        ) : (
          <p className="muted small">{preview.reason || "This branch cannot be created."}</p>
        )}
      </div>
    );
  }

  function renderCreateForm() {
    return (
      <>
        <label className="field" htmlFor={`git-branch-repository-${targetType}-${targetId}`}>
          <span>Repository</span>
          <BhSelect
            id={`git-branch-repository-${targetType}-${targetId}`}
            ariaLabel="Repository for new Git branch"
            value={providerRepoId}
            onChange={handleRepositoryChange}
            disabled={busy}
            options={providerRepos.map((repo) => ({
              value: repo.provider_repo_id,
              label: repo.full_name || repo.name,
            }))}
          />
        </label>
        <label className="field" htmlFor={`git-branch-base-${targetType}-${targetId}`}>
          <span>Base branch</span>
          <input
            id={`git-branch-base-${targetType}-${targetId}`}
            type="text"
            value={baseBranch}
            placeholder={selectedRepo?.default_branch || "dev"}
            onChange={handleBaseBranchChange}
            disabled={busy}
          />
        </label>
        {renderPreviewStep()}
      </>
    );
  }

  function renderCreateStep() {
    if (providerRepos === null) return renderLoadButton();
    if (providerRepos.length === 0) return renderNoReposNotice();
    return renderCreateForm();
  }

  function renderCreateSection() {
    if (!canCreate) {
      if (!reason) return null;
      return <p className="muted small git-branch-reason">{reason}</p>;
    }
    return <div className="git-branch-create">{renderCreateStep()}</div>;
  }

  if (!visible || !targetId) return null;

  return (
    <section className="bug-section git-branches-section" aria-labelledby={`git-branches-${targetType}-${targetId}`}>
      <h3 id={`git-branches-${targetType}-${targetId}`}>Development</h3>
      {renderBody()}
    </section>
  );
}
