/** Signed-out pages of the enterprise edition: sign-up, accept invitation, delete account,
 *  privacy notice, and the two-factor step of the sign-in page. */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AcceptInvitePage from "./accept/AcceptInvitePage";
import DeleteAccountPage from "./deleteAccount/DeleteAccountPage";
import LoginPage from "./login/LoginPage";
import PrivacyPage from "./privacy/PrivacyPage";
import SignupPage from "./signup/SignupPage";
import VerifyPage from "./verify/VerifyPage";

const replace = vi.fn();
const fetchMock = vi.fn();

function reply(body, status = 200) {
  return Promise.resolve({ ok: status < 400, status, json: async () => body });
}

/** Route fetch() by "METHOD /path"; unmatched calls answer 404. */
function route(table) {
  fetchMock.mockImplementation((url, init = {}) => {
    const key = `${init.method || "GET"} ${String(url).replace(/\?.*$/, "")}`;
    const hit = table[key];
    return hit ? reply(...(Array.isArray(hit) ? hit : [hit])) : reply({ detail: "not found" }, 404);
  });
}

function calls(key) {
  return fetchMock.mock.calls.filter(([url, init = {}]) => `${init.method || "GET"} ${url}` === key);
}

beforeEach(() => {
  fetchMock.mockReset();
  replace.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("location", { replace, search: "", hash: "", origin: "http://localhost" });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function fill(label, value) {
  await userEvent.type(screen.getByLabelText(label), value);
}

describe("SignupPage", () => {
  beforeEach(() => route({ "GET /api/meta": { signup_enabled: true } }));

  const next = (name = "Continue") => userEvent.click(screen.getByRole("button", { name }));

  /** Walk the three steps; stops on the password step unless `submit`. */
  async function fillForm(password = "Passw0rd!x", confirm = password) {
    await fill(/Organization name/, "Acme");
    await next();
    await fill(/Your name/, "Alex");
    await fill(/Work email/, "alex@acme.test");
    await next();
    await fill(/^Password/, password);
    await fill(/Confirm password/, confirm);
  }

  it("creates the organization and goes to the app", async () => {
    route({ "GET /api/meta": { signup_enabled: true }, "POST /api/auth/signup": [{ id: 1 }, 201] });
    render(<SignupPage />);
    await fillForm();
    await next("Create organization");
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    const [, init] = calls("POST /api/auth/signup")[0];
    expect(JSON.parse(init.body)).toEqual({
      organization_name: "Acme", name: "Alex", email: "alex@acme.test", password: "Passw0rd!x",
    });
  });

  it("moves through the steps, validates each, and moves focus to the new step's heading", async () => {
    render(<SignupPage />);
    expect(screen.getByRole("list", { name: "Sign-up progress" })).toBeTruthy();
    await next();
    expect((await screen.findByRole("alert")).textContent).toMatch(/name of at least 2/);
    await fill(/Organization name/, "Acme");
    await next();
    const heading = await screen.findByRole("heading", { name: "Tell us about you" });
    await waitFor(() => expect(document.activeElement).toBe(heading));
    expect(screen.getByText("Step 2: You").closest("li").getAttribute("aria-current")).toBe("step");
    await fill(/Your name/, "Alex");
    await fill(/Work email/, "not-an-email");
    await next();
    expect((await screen.findByRole("alert")).textContent).toMatch(/valid work email/);
    // Back keeps what was typed.
    await next("Back");
    expect(screen.getByLabelText(/Organization name/).value).toBe("Acme");
  });

  it("stops on mismatched or weak passwords without calling the server", async () => {
    render(<SignupPage />);
    await fillForm("Passw0rd!x", "different");
    await next("Create organization");
    expect((await screen.findByRole("alert")).textContent).toMatch(/don't match/);
    expect(calls("POST /api/auth/signup")).toHaveLength(0);
  });

  it("shows the server's reason, such as a taken email", async () => {
    route({
      "GET /api/meta": { signup_enabled: true },
      "POST /api/auth/signup": [{ detail: "An account with that email already exists." }, 409],
    });
    render(<SignupPage />);
    await fillForm();
    await next("Create organization");
    expect(await screen.findByText(/already exists/)).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });

  it("asks to check the inbox when verification is required, and resends with a cooldown", async () => {
    route({
      "GET /api/meta": { signup_enabled: true },
      "POST /api/auth/signup": [{ verification_required: true, email: "alex@acme.test" }, 202],
      "POST /api/auth/resend-verification": [null, 204],
    });
    render(<SignupPage />);
    await fillForm();
    await next("Create organization");
    const heading = await screen.findByRole("heading", { name: "Check your inbox" });
    await waitFor(() => expect(document.activeElement).toBe(heading));
    expect(replace).not.toHaveBeenCalled();
    expect(screen.getAllByText("alex@acme.test").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Send a new link" }));
    await waitFor(() => expect(calls("POST /api/auth/resend-verification")).toHaveLength(1));
    expect(JSON.parse(calls("POST /api/auth/resend-verification")[0][1].body)).toEqual({ email: "alex@acme.test" });
    const again = await screen.findByRole("button", { name: /Send a new link \(\d+s\)/ });
    expect(again.disabled).toBe(true);
  });

  it("hides the form when sign-up is switched off", async () => {
    route({ "GET /api/meta": { signup_enabled: false } });
    const { container } = render(<SignupPage />);
    expect(await screen.findByText(/turned off on this server/)).toBeTruthy();
    expect(container.querySelector("#signupSteps").hidden).toBe(true);
  });
});

describe("VerifyPage", () => {
  it("confirms a good link and offers sign-in", async () => {
    vi.stubGlobal("location", { replace, search: "?token=good", hash: "", origin: "http://localhost" });
    route({ "POST /api/auth/verify-email": [null, 204] });
    render(<VerifyPage />);
    expect(screen.getByText(/Confirming your email address/)).toBeTruthy();
    const heading = await screen.findByRole("heading", { name: "Your email is confirmed" });
    await waitFor(() => expect(document.activeElement).toBe(heading));
    expect(screen.getByRole("link", { name: "Sign in" }).getAttribute("href")).toBe("/login.html");
    expect(JSON.parse(calls("POST /api/auth/verify-email")[0][1].body)).toEqual({ token: "good" });
  });

  it("explains a used or expired link and lets the person ask for a new one", async () => {
    vi.stubGlobal("location", { replace, search: "?token=old", hash: "", origin: "http://localhost" });
    route({
      "POST /api/auth/verify-email": [{ detail: "This link is invalid or has expired." }, 400],
      "POST /api/auth/resend-verification": [null, 204],
    });
    render(<VerifyPage />);
    expect(await screen.findByRole("heading", { name: "This link did not work" })).toBeTruthy();
    expect(screen.getByText(/invalid or has expired/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Sign in" })).toBeNull();
    await fill(/Email/, "alex@acme.test");
    await userEvent.click(screen.getByRole("button", { name: "Send a new link" }));
    expect(await screen.findByText(/new link is on its way/)).toBeTruthy();
    expect((await screen.findByRole("button", { name: /Send a new link \(/ })).disabled).toBe(true);
  });

  it("without a token it tells the person to check their inbox", async () => {
    render(<VerifyPage />);
    expect(await screen.findByRole("heading", { name: "Check your inbox" })).toBeTruthy();
    expect(calls("POST /api/auth/verify-email")).toHaveLength(0);
  });
});

describe("AcceptInvitePage", () => {
  it("previews the organization, then joins", async () => {
    vi.stubGlobal("location", { replace, search: "?token=tok123", hash: "", origin: "http://localhost" });
    route({
      "GET /api/invitations/preview/tok123": {
        email: "new@acme.test", organization_name: "Acme", role: "manager", invited_by_name: "Owner",
      },
      "POST /api/invitations/accept": [{ id: 9 }, 200],
    });
    render(<AcceptInvitePage />);
    expect(await screen.findByText("Acme")).toBeTruthy();
    expect(screen.getByText("a manager")).toBeTruthy();
    expect(screen.getByText("new@acme.test")).toBeTruthy();
    await fill(/Your name/, "Nia");
    await fill(/Choose a password/, "Passw0rd!x");
    await fill(/Confirm password/, "Passw0rd!x");
    await userEvent.click(screen.getByRole("button", { name: "Join" }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    expect(JSON.parse(calls("POST /api/invitations/accept")[0][1].body)).toEqual({
      token: "tok123", name: "Nia", password: "Passw0rd!x",
    });
  });

  it("explains a link without a token or with an invalid one", async () => {
    render(<AcceptInvitePage />);
    expect(screen.getByText(/missing its token/)).toBeTruthy();
    cleanup();
    vi.stubGlobal("location", { replace, search: "?token=bad", hash: "", origin: "http://localhost" });
    route({ "GET /api/invitations/preview/bad": [{ detail: "This invitation has expired." }, 400] });
    render(<AcceptInvitePage />);
    expect(await screen.findByText("This invitation has expired.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Join" })).toBeNull();
  });
});

describe("DeleteAccountPage", () => {
  it("signs in, then deletes with the password", async () => {
    route({
      "POST /api/auth/login": [{ id: 1 }, 200],
      "DELETE /api/auth/account": [null, 204],
    });
    render(<DeleteAccountPage />);
    await fill(/Email/, "alex@acme.test");
    await fill(/^Password/, "Passw0rd!x");
    await userEvent.click(screen.getByRole("button", { name: "Permanently delete my account" }));
    expect(await screen.findByText(/has been deleted/)).toBeTruthy();
    expect(JSON.parse(calls("DELETE /api/auth/account")[0][1].body)).toEqual({ password: "Passw0rd!x" });
  });

  it("asks for the 2FA code before deleting", async () => {
    route({
      "POST /api/auth/login": [{ requires_totp: true, pending_token: "pend" }, 200],
      "POST /api/auth/login/totp": [{ id: 1 }, 200],
      "DELETE /api/auth/account": [null, 204],
    });
    render(<DeleteAccountPage />);
    await fill(/Email/, "alex@acme.test");
    await fill(/^Password/, "Passw0rd!x");
    await userEvent.click(screen.getByRole("button", { name: "Permanently delete my account" }));
    await fill(/2FA code/, "123456");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText(/has been deleted/)).toBeTruthy();
    expect(JSON.parse(calls("POST /api/auth/login/totp")[0][1].body)).toEqual({
      pending_token: "pend", code: "123456",
    });
  });

  it("does not delete when sign-in fails", async () => {
    route({ "POST /api/auth/login": [{ detail: "Invalid email or password" }, 401] });
    render(<DeleteAccountPage />);
    await fill(/Email/, "alex@acme.test");
    await fill(/^Password/, "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "Permanently delete my account" }));
    expect(await screen.findByText("Invalid email or password")).toBeTruthy();
    expect(calls("DELETE /api/auth/account")).toHaveLength(0);
  });
});

describe("PrivacyPage", () => {
  it("states the operator's retention period and contact address", async () => {
    route({ "GET /api/meta": { audit_retention_days: 90, privacy_contact_email: "privacy@acme.test" } });
    render(<PrivacyPage />);
    expect(screen.getByRole("heading", { name: "Privacy policy" })).toBeTruthy();
    expect(await screen.findByText(/privacy@acme\.test/)).toBeTruthy();
    expect(document.body.textContent).toMatch(/90 days/);
  });

  it("still renders when the server cannot be reached", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    render(<PrivacyPage />);
    expect(screen.getByRole("heading", { name: "Privacy policy" })).toBeTruthy();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  });
});

describe("LoginPage two-factor step", () => {
  it("asks for the code after a correct password and then signs in", async () => {
    route({
      "GET /api/meta": { signup_enabled: true },
      "POST /api/auth/login": [{ requires_totp: true, pending_token: "pend" }, 200],
      "POST /api/auth/login/totp": [{ id: 1 }, 200],
    });
    const { container } = render(<LoginPage />);
    await userEvent.type(container.querySelector('#loginForm input[type="email"]'), "alex@acme.test");
    await userEvent.type(container.querySelector('#loginForm input[type="password"]'), "Passw0rd!x");
    await userEvent.click(container.querySelector('#loginForm button[type="submit"]'));
    await waitFor(() => expect(container.querySelector("#totpForm").hidden).toBe(false));
    expect(replace).not.toHaveBeenCalled();

    await userEvent.type(container.querySelector('#totpForm input[name="code"]'), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(replace).toHaveBeenCalled());
    expect(JSON.parse(calls("POST /api/auth/login/totp")[0][1].body)).toEqual({
      pending_token: "pend", code: "123456",
    });
  });

  it("shows a wrong code and stays on the step", async () => {
    route({
      "GET /api/meta": { signup_enabled: false },
      "POST /api/auth/login": [{ requires_totp: true, pending_token: "pend" }, 200],
      "POST /api/auth/login/totp": [{ detail: "Invalid code. Try again or use a recovery code." }, 400],
    });
    const { container } = render(<LoginPage />);
    await userEvent.type(container.querySelector('#loginForm input[type="email"]'), "alex@acme.test");
    await userEvent.type(container.querySelector('#loginForm input[type="password"]'), "Passw0rd!x");
    await userEvent.click(container.querySelector('#loginForm button[type="submit"]'));
    await waitFor(() => expect(container.querySelector("#totpForm").hidden).toBe(false));
    await userEvent.type(container.querySelector('#totpForm input[name="code"]'), "000000");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(container.querySelector("#totpForm .auth-alert").textContent).toMatch(/Invalid code/));
    expect(container.querySelector("#totpForm .auth-alert").hidden).toBe(false);
    expect(replace).not.toHaveBeenCalled();
  });

  it("links to sign-up only when the server allows it", async () => {
    route({ "GET /api/meta": { signup_enabled: true } });
    const { container } = render(<LoginPage />);
    await waitFor(() => expect(container.querySelector("#showSignup")).not.toBeNull());
    cleanup();
    route({ "GET /api/meta": { signup_enabled: false } });
    const second = render(<LoginPage />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(second.container.querySelector("#showSignup")).toBeNull();
  });
});
