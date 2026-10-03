/** Behavioral tests for the real change-password dialog (`./ChangePasswordModal.jsx`).
 *
 * The dialog reads its open state from the app context (so these tests stub
 * AppContext), and validates mismatch + the shared policy before POSTing to
 * /api/auth/change-password. Pins: the open flag, the mismatch rule, the
 * policy gate, the success path (fields cleared, dialog closed, toast) and
 * the server-error path (stays open, error toasted).
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.fn();
const toastMock = vi.fn();
const toastErrorMock = vi.fn();
const appState = { changePasswordOpen: true, setChangePasswordOpen: vi.fn() };

vi.mock("../lib/api", () => ({
  api: (...args) => apiMock(...args),
  ApiError: class ApiError extends Error {},
}));
vi.mock("../lib/toast", () => ({
  toast: (...args) => toastMock(...args),
  toastError: (...args) => toastErrorMock(...args),
}));
vi.mock("../state/AppContext", () => ({
  useApp: () => appState,
}));

import ChangePasswordModal from "./ChangePasswordModal.jsx";

let container;
let root;

function mount() {
  // The modal portals to <body>, so a previous mount has to go before the next one.
  if (root) act(() => root.unmount());
  container?.remove();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(ChangePasswordModal));
  });
  return document.body;
}

function setInput(name, value) {
  const el = document.querySelector(`input[name="${name}"]`);
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
  });
}

function submit() {
  act(() => {
    document.querySelector("#formChangePassword").dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true }),
    );
  });
  return act(async () => {});
}

afterEach(() => {
  act(() => root?.unmount());
  document.body.innerHTML = "";
  apiMock.mockReset();
  toastMock.mockClear();
  toastErrorMock.mockClear();
  appState.changePasswordOpen = true;
  appState.setChangePasswordOpen.mockClear();
});

describe("ChangePasswordModal", () => {
  it("stays hidden without the open flag and shows the hint when open", () => {
    appState.changePasswordOpen = false;
    let el = mount();
    expect(el.querySelector("#modalChangePassword").hidden).toBe(true);

    appState.changePasswordOpen = true;
    el = mount();
    expect(el.querySelector("#modalChangePassword").hidden).toBe(false);
    expect(el.textContent).toContain("At least");
  });

  it("rejects a mismatched confirmation without calling the API", async () => {
    mount();
    setInput("current_password", "old-secret");
    setInput("new_password", "N3w-valid-pass");
    setInput("confirm_password", "Different-9");
    await submit();
    expect(apiMock).not.toHaveBeenCalled();
    expect(toastMock).toHaveBeenCalledWith("New passwords don't match", "error");
  });

  it("enforces the shared password policy on the new password", async () => {
    mount();
    setInput("current_password", "old-secret");
    setInput("new_password", "short");
    setInput("confirm_password", "short");
    await submit();
    expect(apiMock).not.toHaveBeenCalled();
    expect(toastMock.mock.calls[0][0]).toContain("at least");
    expect(toastMock.mock.calls[0][1]).toBe("error");
  });

  it("sends the change, clears the fields, closes and toasts on success", async () => {
    apiMock.mockResolvedValueOnce({ ok: true });
    mount();
    setInput("current_password", "old-secret");
    setInput("new_password", "N3w-valid-pass");
    setInput("confirm_password", "N3w-valid-pass");
    await submit();
    expect(apiMock).toHaveBeenCalledWith("/auth/change-password", {
      method: "POST",
      json: { current_password: "old-secret", new_password: "N3w-valid-pass" },
    });
    expect(document.querySelector("input[name='current_password']").value).toBe("");
    expect(appState.setChangePasswordOpen).toHaveBeenCalledWith(false);
    expect(toastMock).toHaveBeenCalledWith("Password updated", "success");
  });

  it("toasts the server error and stays open", async () => {
    apiMock.mockRejectedValueOnce(new Error("Current password is incorrect"));
    mount();
    setInput("current_password", "wrong");
    setInput("new_password", "N3w-valid-pass");
    setInput("confirm_password", "N3w-valid-pass");
    await submit();
    expect(toastErrorMock).toHaveBeenCalled();
    expect(appState.setChangePasswordOpen).not.toHaveBeenCalled();
  });

  it("closes from the cancel button", () => {
    mount();
    act(() => {
      document.querySelector("[data-close-modal]").click();
    });
    expect(appState.setChangePasswordOpen).toHaveBeenCalledWith(false);
    expect(apiMock).not.toHaveBeenCalled();
  });
});
