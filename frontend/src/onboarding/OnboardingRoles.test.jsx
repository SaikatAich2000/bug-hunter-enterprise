/** Role-specific welcome and tour, and the setup wizard's hosting: when it opens, resumes and saves. */
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const setView = vi.fn();
const setAccountOpen = vi.fn();
const apiMock = vi.fn();
const app = { currentUser: { name: "Ada Lovelace", role: "admin" }, projects: [] };

vi.mock("../state/AppContext", () => ({
  useApp: () => ({
    ...app,
    roleRank: (r) => ({ user: 1, manager: 2, admin: 3 })[r] ?? 1,
    setView,
    setAccountOpen,
  }),
}));
vi.mock("../lib/api", () => ({ api: (...a) => apiMock(...a) }));
vi.mock("../lib/branding", () => ({ getAppName: () => "Bug Hunter" }));
// The wizard has its own tests; here it is a probe that exposes what the host passes it.
vi.mock("./SetupWizard.jsx", () => ({
  default: (p) => (p.open ? (
    <div role="dialog" aria-label="wizard" data-step={p.step}>
      <button onClick={() => p.onStep(p.step + 1)}>next</button>
      <button onClick={p.onSkip}>skip</button>
      <button onClick={p.onFinish}>finish</button>
      <button onClick={p.onClose}>close</button>
      <button onClick={p.onOpenAccount}>account</button>
      <button onClick={p.onOpenWebhooks}>webhooks</button>
      <button onClick={p.onTour}>tour</button>
    </div>
  ) : null),
}));

import OnboardingHost from "./OnboardingHost.jsx";
import { emitOnboarding } from "./onboardingBus.js";

const FRESH = {
  welcome_seen: false, welcome_hidden: false, tour_status: "none", tour_step: 0, setup_status: "none", setup_step: 0,
};
function serve(prefs) {
  apiMock.mockImplementation((path, opts) => Promise.resolve(opts?.method === "PUT" ? { ...prefs, ...opts.json } : prefs));
}
const puts = () => apiMock.mock.calls.filter((c) => c[1]?.method === "PUT").map((c) => c[1].json);

beforeEach(() => {
  apiMock.mockReset();
  setView.mockReset();
  setAccountOpen.mockReset();
  app.currentUser = { name: "Ada Lovelace", role: "admin" };
  app.projects = [];
});
afterEach(cleanup);

describe("welcome by role", () => {
  it.each([
    ["admin", [], /Set up your organization/],
    ["manager", [], /Invite your team/],
    ["user", [{ can_manage: true }], /Manage your project/],
    ["user", [], /Plan sprints/],
  ])("%s with projects %j", async (role, projects, text) => {
    app.currentUser = { name: "Ada", role };
    app.projects = projects;
    serve(FRESH);
    render(<OnboardingHost />);
    const dialog = await screen.findByRole("dialog", { name: /Welcome to Bug Hunter/ });
    expect(dialog).toHaveTextContent(text);
    expect(Boolean(screen.queryByRole("button", { name: "Set up my organization" }))).toBe(role === "admin");
  });

  it("an admin can start setup from the welcome: it is remembered as running at step 0", async () => {
    serve(FRESH);
    render(<OnboardingHost />);
    fireEvent.click(await screen.findByRole("button", { name: "Set up my organization" }));
    expect(puts()[0]).toEqual({ welcome_seen: true, setup_status: "running", setup_step: 0 });
    expect(await screen.findByRole("dialog", { name: "wizard" })).toHaveAttribute("data-step", "0");
    expect(screen.queryByRole("dialog", { name: /Welcome/ })).toBeNull();
  });
});

describe("setup wizard hosting", () => {
  it("resumes at the saved step on load for an admin, and not for others", async () => {
    serve({ ...FRESH, welcome_seen: true, setup_status: "running", setup_step: 2 });
    render(<OnboardingHost />);
    expect(await screen.findByRole("dialog", { name: "wizard" })).toHaveAttribute("data-step", "2");
    cleanup();
    app.currentUser = { name: "Bo", role: "manager" };
    render(<OnboardingHost />);
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    expect(screen.queryByRole("dialog", { name: "wizard" })).toBeNull();
  });

  it("stays out of the way once it is done or skipped", async () => {
    serve({ ...FRESH, welcome_seen: true, setup_status: "skipped" });
    render(<OnboardingHost />);
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("saves each step, skip, finish; closing only hides", async () => {
    serve({ ...FRESH, welcome_seen: true });
    render(<OnboardingHost />);
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    act(() => emitOnboarding("setup"));
    fireEvent.click(await screen.findByText("next"));
    expect(puts().at(-1)).toEqual({ setup_step: 1, setup_status: "running" });
    fireEvent.click(screen.getByText("close"));
    expect(screen.queryByRole("dialog", { name: "wizard" })).toBeNull();
    expect(puts().at(-1)).toEqual({ setup_step: 1, setup_status: "running" });

    act(() => emitOnboarding("setup"));
    fireEvent.click(await screen.findByText("skip"));
    expect(puts().at(-1)).toEqual({ setup_status: "skipped", setup_step: 0 });
    act(() => emitOnboarding("setup"));
    fireEvent.click(await screen.findByText("finish"));
    expect(puts().at(-1)).toEqual({ setup_status: "done", setup_step: 0 });
  });

  it("the extras hand off to account settings, the Organization view and the tour", async () => {
    serve({ ...FRESH, welcome_seen: true });
    render(<OnboardingHost />);
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    act(() => emitOnboarding("setup"));
    fireEvent.click(await screen.findByText("account"));
    expect(setAccountOpen).toHaveBeenCalledWith(true);
    act(() => emitOnboarding("setup"));
    fireEvent.click(await screen.findByText("webhooks"));
    expect(setView).toHaveBeenCalledWith("organization");
    act(() => emitOnboarding("setup"));
    fireEvent.click(await screen.findByText("tour"));
    expect(puts().at(-1)).toMatchObject({ tour_status: "running", tour_step: 0 });
  });
});

describe("tour by role", () => {
  it("the admin tour is longer than a user's and ends on the settings step", async () => {
    serve({ ...FRESH, welcome_seen: true, tour_status: "running", tour_step: 8 });
    render(<OnboardingHost />);
    expect(await screen.findByText("Step 9 of 9")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveAccessibleName("Settings and help");
    expect(screen.getByRole("dialog")).toHaveTextContent(/Organization setup/);
  });

  it("a project lead's tour has the project step", async () => {
    app.currentUser = { name: "Lee", role: "user" };
    app.projects = [{ can_manage: true }];
    serve({ ...FRESH, welcome_seen: true, tour_status: "running", tour_step: 1 });
    render(<OnboardingHost />);
    expect(await screen.findByRole("dialog")).toHaveAccessibleName("Your projects");
    expect(screen.getByText("Step 2 of 6")).toBeInTheDocument();
  });
});
