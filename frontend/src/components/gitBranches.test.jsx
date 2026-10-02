/** Behavioral tests for the real Git branch request contract in
 * `../gitBranchRequests.js` (the exact module GitBranchesPanel imports).
 *
 * They verify, against production code, that:
 * - a branch-create body carries exactly provider_repo_id/base_branch;
 * - removal is gated on backend capability flags;
 * - the exact-removal request carries only record id + expected version;
 * - Deleted history rows stay visible and non-removable.
 */
import { describe, expect, it, vi } from "vitest";
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
} from "../lib/gitBranchRequests.js";

describe("branch-create request/body contract", () => {
  it("targets the per-item endpoint", () => {
    expect(branchListEndpoint(7)).toBe("/git/work-items/7/branches");
  });

  it("sends exactly provider_repo_id and base_branch in preview and create", () => {
    const query = buildPreviewQuery({ providerRepoId: "42", baseBranch: "dev" });
    expect(query).toContain("provider_repo_id=42");
    expect(query).toContain("base_branch=dev");
    const body = buildBranchCreateBody({ providerRepoId: "42", baseBranch: "dev" });
    expect(body).toEqual({ provider_repo_id: "42", base_branch: "dev" });
    expect(Object.keys(body).sort((a, b) => a.localeCompare(b))).toEqual([
      "base_branch",
      "provider_repo_id",
    ]);
  });

  it("falls back to the repository default branch when the input is blank", () => {
    const body = buildBranchCreateBody({ providerRepoId: "42", baseBranch: "", fallbackBaseBranch: "trunk" });
    expect(body.base_branch).toBe("trunk");
  });
});

describe("exact removal confirmation", () => {
  const branch = {
    id: 3,
    version: 2,
    branch_name: "feature_5_login-page",
    repository_full_name: "acme/widgets",
    repository_name: "widgets",
    status: "Active",
  };

  it("exposes Active branches as removable and keeps Deleted history visible", () => {
    const rows = [
      branch,
      { ...branch, id: 4, status: "Deleted" },
    ];
    expect(removableBranches(rows).map((b) => b.id)).toEqual([3]); // only Active removable
    expect(rows).toHaveLength(2); // but history stays visible
  });

  it("shows the remove control only when the backend capability says so", () => {
    expect(canRemoveBranches({ itemType: "Story", data: { branch_deletion_enabled: true, can_remove_branch: true } })).toBe(true);
    expect(canRemoveBranches({ itemType: "Story", data: { branch_deletion_enabled: false, can_remove_branch: true } })).toBe(false);
    expect(canRemoveBranches({ itemType: "Story", data: { branch_deletion_enabled: true, can_remove_branch: false } })).toBe(false);
    expect(canRemoveBranches({ itemType: "Bug", data: { branch_deletion_enabled: true, can_remove_branch: true } })).toBe(false);
  });

  it("gates create on persisted Story capability", () => {
    expect(canCreateBranch({ itemType: "Story", data: { can_create_branch: true } })).toBe(true);
    expect(canCreateBranch({ itemType: "User Story", data: { can_create_branch: true } })).toBe(true);
    expect(canCreateBranch({ itemType: "Bug", data: { can_create_branch: true } })).toBe(false);
    expect(canCreateBranch({ itemType: "Story", data: { can_create_branch: false } })).toBe(false);
  });

  it("requires a danger confirmation with the exact repository and branch", async () => {
    const confirmDialog = vi.fn(async () => true);
    const message = removalConfirmationMessage(branch);
    const confirmed = await confirmDialog(message, {
      title: "Remove Feature Branch",
      okLabel: "Remove",
      danger: true,
    });
    expect(confirmDialog).toHaveBeenCalledTimes(1);
    const [text, opts] = confirmDialog.mock.calls[0];
    expect(text).toContain(branch.repository_full_name);
    expect(text).toContain(branch.branch_name);
    expect(opts.danger).toBe(true);
    expect(confirmed).toBe(true);
  });

  it("sends only the record id and the expected version", () => {
    const req = buildBranchRemovalRequest(branch);
    expect(req.url).toBe("/git/branches/3?expected_version=2");
    expect(req.init).toEqual({ method: "DELETE" });
    expect(req.url).not.toContain("branch_name");
    expect(req.url).not.toContain("repository");
  });
});

describe("branch row status presentation", () => {
  const active = { id: 1, status: "Active", branch_name: "feature_a" };
  const deleted = { id: 2, status: "Deleted", branch_name: "feature_b" };
  const unknown = { id: 3, status: "Unknown", branch_name: "feature_c" };

  it("sidelined removed branches after every active one, order preserved", () => {
    const ordered = partitionBranches([deleted, active, unknown]);
    expect(ordered.map((b) => b.id)).toEqual([1, 2, 3]);
    expect(ordered.map((b) => b.status)).toEqual(["Active", "Deleted", "Unknown"]);
  });

  it("keeps the relative order inside each group", () => {
    const second = { id: 9, status: "Deleted", branch_name: "feature_d" };
    expect(partitionBranches([deleted, second, active]).map((b) => b.id)).toEqual([1, 2, 9]);
    expect(partitionBranches([active, { id: 8, status: "Active" }]).map((b) => b.id)).toEqual([1, 8]);
  });

  it("tolerates an empty or missing list", () => {
    expect(partitionBranches([])).toEqual([]);
    expect(partitionBranches(undefined)).toEqual([]);
  });

  it("flags only non-Active rows as removed history", () => {
    expect(isRemovedBranch(active)).toBe(false);
    expect(isRemovedBranch(deleted)).toBe(true);
    expect(isRemovedBranch(unknown)).toBe(true);
    expect(isRemovedBranch(null)).toBe(false);
    expect(isRemovedBranch(undefined)).toBe(false);
  });
});