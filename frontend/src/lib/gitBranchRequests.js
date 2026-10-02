/** Real request construction for the Story feature-branch panel.
 *
 * Kept as pure helpers so tests can assert the exact wire contract the backend
 * enforces (provider_repo_id + base_branch only; removal by record id and
 * expected_version) without re-implementing the strings inside the test file.
 */

/** Branch endpoint for one persisted work item. */
export function branchListEndpoint(targetId) {
  return `/git/work-items/${targetId}/branches`;
}

/** Preview query string — exactly the two accepted fields. */
export function buildPreviewQuery({ providerRepoId, baseBranch }) {
  return new URLSearchParams({
    provider_repo_id: providerRepoId,
    base_branch: baseBranch || "",
  }).toString();
}

/** Create body — exactly the two accepted fields, with the repo default fallback. */
export function buildBranchCreateBody({ providerRepoId, baseBranch, fallbackBaseBranch }) {
  return {
    provider_repo_id: providerRepoId,
    base_branch: baseBranch || fallbackBaseBranch || "",
  };
}

/** Exact-removal request: only the record id and the opened version travel. */
export function buildBranchRemovalRequest(branch) {
  return {
    url: `/git/branches/${branch.id}?expected_version=${encodeURIComponent(branch.version)}`,
    init: { method: "DELETE" },
  };
}

/** Active branches are removable; Deleted history is display-only. */
export function removableBranches(branches) {
  return (branches || []).filter((branch) => branch.status === "Active");
}

/** Display order: Active branches first, Deleted history sidelined to the end.
 *
 * Stable within each group so the server's creation order is preserved; a
 * Deleted row stays visible (it is history) but never leads the list.
 */
export function partitionBranches(branches) {
  const rows = branches || [];
  return [
    ...rows.filter((branch) => branch.status === "Active"),
    ...rows.filter((branch) => branch.status !== "Active"),
  ];
}

/** True when a branch row is historical (removed) rather than live. */
export function isRemovedBranch(branch) {
  return Boolean(branch) && branch.status !== "Active";
}

/** Backend capability gate for the removal control (Story + flags). */
export function canRemoveBranches({ itemType, data }) {
  const isUserStory = itemType === "Story" || itemType === "User Story";
  return Boolean(
    isUserStory && data?.branch_deletion_enabled && data?.can_remove_branch,
  );
}

/** Capability gate for the create control. */
export function canCreateBranch({ itemType, data }) {
  const isUserStory = itemType === "Story" || itemType === "User Story";
  return Boolean(isUserStory && data?.can_create_branch);
}

/** Confirmation text shown before an exact removal. */
export function removalConfirmationMessage(branch) {
  return (
    `Repository: ${branch.repository_full_name || branch.repository_name}\n` +
    `Branch: ${branch.branch_name}\n\n` +
    "The exact remote branch will be deleted and this history record will be retained."
  );
}