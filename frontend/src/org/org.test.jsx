/** Organization screens: tabs by role, invitations, webhooks (secret shown once), branding. */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.fn();
const toastMock = vi.fn();
const toastErrorMock = vi.fn();
const confirmMock = vi.fn();
const appState = {
  isAdmin: true,
  projects: [],
  currentUser: { organization_name: "Acme" },
  refreshMe: vi.fn(),
};

vi.mock("../lib/api", () => ({ api: (...a) => apiMock(...a) }));
vi.mock("../lib/toast", () => ({
  toast: (...a) => toastMock(...a),
  toastError: (...a) => toastErrorMock(...a),
}));
vi.mock("../components/ConfirmHost", () => ({ confirmDialog: (...a) => confirmMock(...a) }));
vi.mock("../state/AppContext", () => ({ useApp: () => appState }));

import OrganizationView from "../views/OrganizationView";
import BrandingTab from "./BrandingTab";
import InvitationsTab from "./InvitationsTab";
import WebhooksTab from "./WebhooksTab";

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockResolvedValue([]);
  toastMock.mockClear();
  toastErrorMock.mockClear();
  confirmMock.mockReset();
  appState.isAdmin = true;
  appState.projects = [];
});
afterEach(cleanup);

describe("OrganizationView", () => {
  it("shows every tab to admins", async () => {
    apiMock.mockResolvedValue({ name: "Acme", description: "" });
    render(<OrganizationView />);
    const tabs = screen.getAllByRole("tab").map((t) => t.textContent);
    expect(tabs).toEqual(["General", "Branding", "Invitations", "Webhooks"]);
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/organization"));
  });

  it("shows managers only the invitations tab", async () => {
    appState.isAdmin = false;
    render(<OrganizationView />);
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Invitations"]);
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/invitations"));
  });

  it("switches panels", async () => {
    apiMock.mockImplementation(async (path) => (path === "/organization" ? { name: "Acme", description: "" } : []));
    render(<OrganizationView />);
    await userEvent.click(screen.getByRole("tab", { name: "Webhooks" }));
    expect(screen.getByRole("tab", { name: "Webhooks" }).getAttribute("aria-selected")).toBe("true");
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/webhooks"));
  });
});

describe("InvitationsTab", () => {
  it("refuses an invalid address and sends a valid invitation with its projects", async () => {
    appState.projects = [{ id: 7, name: "Billing", can_manage: true }];
    apiMock.mockImplementation(async (path, opts) => {
      if (path === "/invitations" && opts?.method === "POST") return { id: 1 };
      return [];
    });
    render(<InvitationsTab />);
    const email = screen.getByRole("textbox", { name: /email/i });
    await userEvent.type(email, "not-an-email");
    await userEvent.click(screen.getByRole("button", { name: "Send invitation" }));
    expect(toastMock).toHaveBeenCalledWith("Enter a valid email address", "error");

    await userEvent.clear(email);
    await userEvent.type(email, "new@acme.test");
    await userEvent.click(screen.getByRole("checkbox", { name: "Billing" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Join as project lead" }));
    await userEvent.click(screen.getByRole("button", { name: "Send invitation" }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/invitations", {
        method: "POST",
        json: { email: "new@acme.test", role: "user", project_ids: [7], as_lead: true },
      }),
    );
  });

  it("offers managers only the projects they lead and no admin role", () => {
    appState.isAdmin = false;
    appState.projects = [
      { id: 1, name: "Mine", can_manage: true },
      { id: 2, name: "Theirs", can_manage: false },
    ];
    render(<InvitationsTab />);
    expect(screen.getByRole("checkbox", { name: "Mine" })).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: "Theirs" })).toBeNull();
    expect(screen.queryByRole("option", { name: "Admin" })).toBeNull();
  });

  it("lists invitations by status and revokes a pending one after confirmation", async () => {
    const future = new Date(Date.now() + 86400000).toISOString();
    const past = new Date(Date.now() - 86400000).toISOString();
    apiMock.mockImplementation(async (path, opts) => {
      if (opts?.method === "DELETE") return { message: "Invitation revoked" };
      return [
        { id: 1, email: "wait@acme.test", role: "user", invited_by_name: "Owner", expires_at: future },
        { id: 2, email: "old@acme.test", role: "user", invited_by_name: "Owner", expires_at: past },
        { id: 3, email: "done@acme.test", role: "user", invited_by_name: "Owner", expires_at: future, accepted_at: future },
      ];
    });
    confirmMock.mockResolvedValue(true);
    render(<InvitationsTab />);
    const row = (await screen.findByText("wait@acme.test")).closest("tr");
    expect(within(row).getByText("pending")).toBeTruthy();
    expect(within(screen.getByText("old@acme.test").closest("tr")).getByText("expired")).toBeTruthy();
    expect(within(screen.getByText("done@acme.test").closest("tr")).getByText("accepted")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Revoke" })).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/invitations/1", { method: "DELETE" }));
  });
});

describe("WebhooksTab", () => {
  it("shows the signing secret once after creating a webhook", async () => {
    apiMock.mockImplementation(async (path, opts) => {
      if (opts?.method === "POST") return { id: 5, name: "CI", secret: "s3cr3t-value" };
      return [];
    });
    render(<WebhooksTab />);
    await userEvent.type(screen.getByRole("textbox", { name: /name/i }), "CI");
    await userEvent.type(screen.getByRole("textbox", { name: /url/i }), "https://example.com/hook");
    await userEvent.click(screen.getByRole("button", { name: "Create webhook" }));

    expect(await screen.findByText("s3cr3t-value")).toBeTruthy();
    expect(apiMock).toHaveBeenCalledWith("/webhooks", {
      method: "POST",
      json: { name: "CI", url: "https://example.com/hook", events: "*" },
    });
    await userEvent.click(screen.getByRole("button", { name: "I saved it" }));
    expect(screen.queryByText("s3cr3t-value")).toBeNull();
  });

  it("lists hooks with their delivery state and pauses one", async () => {
    apiMock.mockImplementation(async (path, opts) => {
      if (opts?.method === "PUT") return {};
      return [{
        id: 3, name: "Pager", url: "https://p.example.com", events: "bug.*", is_active: true,
        consecutive_failures: 2, last_error: "HTTP 500", last_status_code: 500,
        last_delivered_at: "2026-01-01T00:00:00Z", created_at: "2026-01-01T00:00:00Z",
      }];
    });
    render(<WebhooksTab />);
    expect(await screen.findByText("Pager")).toBeTruthy();
    expect(screen.getByText(/2 failure\(s\) in a row/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/webhooks/3", { method: "PUT", json: { is_active: false } }),
    );
  });

  it("asks before rotating the secret and then shows the new one", async () => {
    apiMock.mockImplementation(async (path) => {
      if (path.endsWith("/rotate-secret")) return { id: 3, name: "Pager", secret: "fresh-secret" };
      return [{ id: 3, name: "Pager", url: "https://p.example.com", events: "*", is_active: true,
        consecutive_failures: 0, created_at: "2026-01-01T00:00:00Z" }];
    });
    confirmMock.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    render(<WebhooksTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Rotate secret" }));
    expect(screen.queryByText("fresh-secret")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Rotate secret" }));
    expect(await screen.findByText("fresh-secret")).toBeTruthy();
  });
});

describe("BrandingTab", () => {
  it("rejects a malformed accent colour and saves a valid one", async () => {
    apiMock.mockImplementation(async (path, opts) => {
      if (opts?.method === "PUT") return {};
      return { logo_data_url: null, accent_color: null, email_from_override: null };
    });
    render(<BrandingTab />);
    const accent = screen.getByPlaceholderText("#6366f1");
    await waitFor(() => expect(accent.disabled).toBe(false));
    await userEvent.type(accent, "blue");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(toastMock).toHaveBeenCalledWith(expect.stringContaining("hex value"), "error");
    expect(apiMock).not.toHaveBeenCalledWith("/branding", expect.objectContaining({ method: "PUT" }));

    await userEvent.clear(accent);
    await userEvent.type(accent, "#336699");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/branding", {
        method: "PUT",
        json: { logo_data_url: "", accent_color: "#336699", email_from_override: "" },
      }),
    );
    expect(appState.refreshMe).toHaveBeenCalled();
  });

  it("refuses logos that are too large or not images", async () => {
    apiMock.mockResolvedValue({ logo_data_url: null, accent_color: null, email_from_override: null });
    const { container } = render(<BrandingTab />);
    const input = container.querySelector('input[type="file"]');
    await waitFor(() => expect(input.disabled).toBe(false));
    await userEvent.upload(input, new File(["x"], "a.txt", { type: "text/plain" }), { applyAccept: false });
    expect(toastMock).toHaveBeenCalledWith(expect.stringContaining("PNG, JPEG"), "error");
    const big = new File([new Uint8Array(120 * 1024)], "big.png", { type: "image/png" });
    await userEvent.upload(input, big);
    expect(toastMock).toHaveBeenCalledWith(expect.stringContaining("100 KB"), "error");
  });
});
