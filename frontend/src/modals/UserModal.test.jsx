/** The admin-only "turn off their 2FA" action of the user form. */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.fn();
const toastMock = vi.fn();
const confirmMock = vi.fn();
const appState = {
  userModal: { open: true, user: null },
  closeUserModal: vi.fn(),
  loadUsers: vi.fn(),
  refreshAll: vi.fn(),
  canManage: true,
  isAdmin: true,
  projects: [],
};

vi.mock("../lib/api", () => ({ api: (...a) => apiMock(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a) => toastMock(...a), toastError: vi.fn() }));
vi.mock("../components/ConfirmHost", () => ({ confirmDialog: (...a) => confirmMock(...a) }));
vi.mock("../state/AppContext", () => ({ useApp: () => appState }));

import UserModal from "./UserModal";

const dana = { id: 7, name: "Dana", email: "dana@acme.test", role: "user", is_active: true, project_ids: [], totp_enabled: true };

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockResolvedValue({});
  toastMock.mockClear();
  confirmMock.mockReset();
  appState.closeUserModal.mockClear();
  appState.loadUsers.mockClear();
  appState.isAdmin = true;
  appState.userModal = { open: true, user: dana };
});
afterEach(cleanup);

describe("UserModal two-factor reset", () => {
  it("is offered to admins for a user who has 2FA on, and asks first", async () => {
    confirmMock.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    render(<UserModal />);
    const button = screen.getByRole("button", { name: "Turn off their 2FA" });
    await userEvent.click(button);
    expect(apiMock).not.toHaveBeenCalled();

    await userEvent.click(button);
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/users/7/reset-2fa", { method: "POST" }));
    expect(appState.loadUsers).toHaveBeenCalled();
    expect(appState.closeUserModal).toHaveBeenCalled();
    expect(toastMock).toHaveBeenCalledWith("Two-factor sign-in turned off", "success");
  });

  it("is hidden when the user has no 2FA, for a new user, and for non-admins", () => {
    appState.userModal = { open: true, user: { ...dana, totp_enabled: false } };
    const { unmount } = render(<UserModal />);
    expect(screen.queryByRole("button", { name: "Turn off their 2FA" })).toBeNull();
    unmount();

    appState.userModal = { open: true, user: null };
    const second = render(<UserModal />);
    expect(screen.queryByRole("button", { name: "Turn off their 2FA" })).toBeNull();
    second.unmount();

    appState.isAdmin = false;
    appState.userModal = { open: true, user: dana };
    render(<UserModal />);
    expect(screen.queryByRole("button", { name: "Turn off their 2FA" })).toBeNull();
  });
});
