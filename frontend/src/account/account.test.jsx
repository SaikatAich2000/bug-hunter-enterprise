/** Account settings: profile and email change, two-factor enrolment, push preferences, privacy. */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.fn();
const apiBlobMock = vi.fn();
const toastMock = vi.fn();
const toastErrorMock = vi.fn();
const confirmMock = vi.fn();
const appState = {
  accountOpen: true,
  setAccountOpen: vi.fn(),
  setChangePasswordOpen: vi.fn(),
  refreshMe: vi.fn(),
  currentUser: { name: "Alex", email: "alex@acme.test", role: "admin", organization_name: "Acme" },
};

vi.mock("../lib/api", () => ({
  api: (...a) => apiMock(...a),
  apiBlob: (...a) => apiBlobMock(...a),
}));
vi.mock("../lib/toast", () => ({
  toast: (...a) => toastMock(...a),
  toastError: (...a) => toastErrorMock(...a),
}));
vi.mock("../components/ConfirmHost", () => ({ confirmDialog: (...a) => confirmMock(...a) }));
vi.mock("../state/AppContext", () => ({ useApp: () => appState }));

import AccountModal from "./AccountModal";
import NotificationsTab from "./NotificationsTab";
import PrivacyTab from "./PrivacyTab";
import ProfileTab from "./ProfileTab";
import SecurityTab from "./SecurityTab";

beforeEach(() => {
  apiMock.mockReset();
  apiBlobMock.mockReset();
  toastMock.mockClear();
  toastErrorMock.mockClear();
  confirmMock.mockReset();
  appState.refreshMe.mockReset();
});
afterEach(cleanup);

describe("AccountModal", () => {
  it("opens on the profile tab and moves between tabs", async () => {
    apiMock.mockResolvedValue({ mentions: true, assignments: true, activity: true, enabled: false, available: true });
    render(<AccountModal />);
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
      "Profile", "Security", "Notifications", "Privacy",
    ]);
    expect(screen.getByRole("tab", { name: "Profile" }).getAttribute("aria-selected")).toBe("true");
    await userEvent.click(screen.getByRole("tab", { name: "Notifications" }));
    expect(await screen.findByText("Push notifications")).toBeTruthy();
  });
});

describe("ProfileTab", () => {
  it("saves a changed name", async () => {
    apiMock.mockResolvedValue({});
    render(<ProfileTab />);
    const save = screen.getByRole("button", { name: "Save" });
    expect(save.disabled).toBe(true);
    const name = screen.getByRole("textbox", { name: /^Name/ });
    await userEvent.clear(name);
    await userEvent.type(name, "Alexandra");
    await userEvent.click(save);
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/auth/profile", { method: "PUT", json: { name: "Alexandra" } }),
    );
    expect(appState.refreshMe).toHaveBeenCalled();
  });

  it("changes the email in two steps: password, then the mailed code", async () => {
    apiMock.mockResolvedValue({});
    render(<ProfileTab />);
    await userEvent.type(screen.getByRole("textbox", { name: /New email/ }), "alexa@acme.test");
    await userEvent.type(document.getElementById("profileEmailPassword"), "Current-pw-1");
    await userEvent.click(screen.getByRole("button", { name: "Send code" }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/auth/email-change/request", {
        method: "POST",
        json: { new_email: "alexa@acme.test", current_password: "Current-pw-1" },
      }),
    );

    await userEvent.type(await screen.findByRole("textbox", { name: /6-digit code/ }), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/auth/email-change/confirm", { method: "POST", json: { code: "123456" } }),
    );
    expect(appState.refreshMe).toHaveBeenCalled();
  });

  it("rejects a malformed new address before calling the server", async () => {
    render(<ProfileTab />);
    await userEvent.type(screen.getByRole("textbox", { name: /New email/ }), "nope");
    await userEvent.type(document.getElementById("profileEmailPassword"), "Current-pw-1");
    await userEvent.click(screen.getByRole("button", { name: "Send code" }));
    expect(toastMock).toHaveBeenCalledWith("Enter a valid email address", "error");
    expect(apiMock).not.toHaveBeenCalled();
  });
});

describe("SecurityTab", () => {
  it("enrols two-factor authentication and shows the recovery codes once", async () => {
    apiMock.mockImplementation(async (path) => {
      if (path === "/auth/2fa/status") return { enabled: false, available: true, unused_recovery_codes: 0 };
      if (path === "/auth/2fa/begin") return { secret: "JBSWY3DPEHPK3PXP", otpauth_uri: "otpauth://totp/Acme:alex?secret=JBSWY3DPEHPK3PXP" };
      if (path === "/auth/2fa/confirm") return { enabled: true, recovery_codes: ["AAAA-1111", "BBBB-2222"] };
      return {};
    });
    render(<SecurityTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Enable 2FA" }));
    expect(await screen.findByText("JBSWY3DPEHPK3PXP")).toBeTruthy();
    expect(screen.getByRole("img", { name: "Authenticator setup QR code" })).toBeTruthy();

    const turnOn = screen.getByRole("button", { name: "Turn on" });
    expect(turnOn.disabled).toBe(true);
    await userEvent.type(screen.getByRole("textbox", { name: /Code/ }), "123456");
    await userEvent.click(turnOn);
    expect(await screen.findByText("AAAA-1111")).toBeTruthy();
    expect(screen.getByText("BBBB-2222")).toBeTruthy();
    expect(apiMock).toHaveBeenCalledWith("/auth/2fa/confirm", { method: "POST", json: { code: "123456" } });

    await userEvent.click(screen.getByRole("button", { name: "I saved them" }));
    expect(screen.queryByText("AAAA-1111")).toBeNull();
  });

  it("turns two-factor off only with the password", async () => {
    apiMock.mockImplementation(async (path) =>
      path === "/auth/2fa/status" ? { enabled: true, available: true, unused_recovery_codes: 7 } : {},
    );
    render(<SecurityTab />);
    expect(await screen.findByText(/7 recovery code\(s\) left/)).toBeTruthy();
    const off = screen.getByRole("button", { name: "Turn off 2FA" });
    expect(off.disabled).toBe(true);
    await userEvent.type(document.getElementById("securityPassword"), "Current-pw-1");
    await userEvent.click(off);
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/auth/2fa/disable", { method: "POST", json: { password: "Current-pw-1" } }),
    );
  });

  it("says so when the server has two-factor switched off", async () => {
    apiMock.mockResolvedValue({ enabled: false, available: false });
    render(<SecurityTab />);
    expect(await screen.findByText(/turned off on this server/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Enable 2FA" })).toBeNull();
  });
});

describe("NotificationsTab", () => {
  it("toggles a channel and reverts when saving fails", async () => {
    apiMock.mockImplementation(async (path, opts) => {
      if (opts?.method === "PUT") throw new Error("boom");
      return { mentions: true, assignments: true, activity: true };
    });
    render(<NotificationsTab />);
    const box = await screen.findByRole("checkbox", { name: "Other activity" });
    expect(box.checked).toBe(true);
    await userEvent.click(box);
    await waitFor(() => expect(toastErrorMock).toHaveBeenCalled());
    expect(screen.getByRole("checkbox", { name: "Other activity" }).checked).toBe(true);
  });

  it("sends only the changed channel", async () => {
    apiMock.mockImplementation(async (path, opts) =>
      opts?.method === "PUT" ? { mentions: false, assignments: true, activity: true }
        : { mentions: true, assignments: true, activity: true },
    );
    render(<NotificationsTab />);
    await userEvent.click(await screen.findByRole("checkbox", { name: "Mentions" }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/notifications/preferences", { method: "PUT", json: { mentions: false } }),
    );
  });
});

describe("PrivacyTab", () => {
  it("exports the user's data", async () => {
    apiBlobMock.mockResolvedValue({ blob: new Blob(["{}"]), filename: "x.json" });
    URL.createObjectURL = vi.fn(() => "blob:x");
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<PrivacyTab />);
    await userEvent.click(screen.getByRole("button", { name: "Download my data" }));
    await waitFor(() => expect(apiBlobMock).toHaveBeenCalledWith("/auth/data-export"));
    await waitFor(() => expect(click).toHaveBeenCalled());
    click.mockRestore();
  });

  it("needs the password and a confirmation before deleting the account", async () => {
    apiMock.mockResolvedValue({});
    confirmMock.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    render(<PrivacyTab />);
    const del = screen.getByRole("button", { name: "Delete my account" });
    expect(del.disabled).toBe(true);
    await userEvent.type(document.getElementById("deleteAccountPassword"), "Current-pw-1");
    await userEvent.click(del);
    expect(apiMock).not.toHaveBeenCalled();

    const replace = vi.fn();
    vi.stubGlobal("location", { replace });
    await userEvent.click(del);
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/auth/account", { method: "DELETE", json: { password: "Current-pw-1" } }),
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login.html"));
    vi.unstubAllGlobals();
  });
});
