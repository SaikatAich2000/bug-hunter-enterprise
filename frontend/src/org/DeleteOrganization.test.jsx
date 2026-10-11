/** Organization deletion UI: dry-run summary first, then password plus the exact typed name. */
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.fn();
const toastErrorMock = vi.fn();
const replace = vi.fn();

vi.mock("../lib/api", () => ({ api: (...a) => apiMock(...a) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn(), toastError: (...a) => toastErrorMock(...a) }));

import DeleteOrganization from "./DeleteOrganization";

const SUMMARY = {
  organization: "Acme",
  will_delete: { users: 3, projects: 2, items: 41, comments: 9, attachments: 1, webhooks: 1, invitations: 0, saved_views: 2, audit_entries: 120 },
};

beforeEach(() => {
  apiMock.mockReset();
  toastErrorMock.mockReset();
  replace.mockReset();
  vi.stubGlobal("location", { replace });
  apiMock.mockImplementation(async (path) => (path === "/organization/deletion-summary" ? SUMMARY : null));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("DeleteOrganization", () => {
  it("shows nothing destructive until the dry run, which only reads", async () => {
    render(<DeleteOrganization orgName="Acme" />);
    expect(screen.queryByLabelText(/Your password/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show what will be deleted" }));
    expect(await screen.findByText("41")).toBeInTheDocument();
    expect(screen.getByText("Work items")).toBeInTheDocument();
    expect(apiMock).toHaveBeenCalledTimes(1);
    expect(apiMock).toHaveBeenCalledWith("/organization/deletion-summary");
    // The summary heading takes focus so the change is announced.
    await waitFor(() => expect(screen.getByRole("heading", { name: "This will permanently delete" })).toHaveFocus());
  });

  it("enables deletion only with the password and the exact name", async () => {
    render(<DeleteOrganization orgName="Acme" />);
    await userEvent.click(screen.getByRole("button", { name: "Show what will be deleted" }));
    const button = await screen.findByRole("button", { name: "Delete organization forever" });
    expect(button).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Your password/), "Secret123");
    await userEvent.type(screen.getByLabelText(/Type the organization name/), "acme");
    expect(button).toBeDisabled();
    await userEvent.clear(screen.getByLabelText(/Type the organization name/));
    await userEvent.type(screen.getByLabelText(/Type the organization name/), "Acme");
    expect(button).toBeEnabled();
  });

  it("deletes with the password and typed name, then leaves for the sign-in page", async () => {
    render(<DeleteOrganization orgName="Acme" />);
    await userEvent.click(screen.getByRole("button", { name: "Show what will be deleted" }));
    await userEvent.type(await screen.findByLabelText(/Your password/), "Secret123");
    await userEvent.type(screen.getByLabelText(/Type the organization name/), "Acme");
    await userEvent.click(screen.getByRole("button", { name: "Delete organization forever" }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login.html"));
    expect(apiMock).toHaveBeenLastCalledWith("/organization/delete", {
      method: "POST", json: { password: "Secret123", confirm_name: "Acme" },
    });
  });

  it("stays put and shows the server's refusal, and Cancel goes back to the start", async () => {
    apiMock.mockImplementation(async (path) => {
      if (path === "/organization/deletion-summary") return SUMMARY;
      throw new Error("Current password is incorrect.");
    });
    render(<DeleteOrganization orgName="Acme" />);
    await userEvent.click(screen.getByRole("button", { name: "Show what will be deleted" }));
    await userEvent.type(await screen.findByLabelText(/Your password/), "wrong");
    await userEvent.type(screen.getByLabelText(/Type the organization name/), "Acme");
    await userEvent.click(screen.getByRole("button", { name: "Delete organization forever" }));
    await waitFor(() => expect(toastErrorMock).toHaveBeenCalled());
    expect(replace).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Show what will be deleted" })).toBeInTheDocument();
  });
});
