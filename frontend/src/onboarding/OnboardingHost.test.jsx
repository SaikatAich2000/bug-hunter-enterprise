/** Welcome screen, tour persistence, hints and empty-state actions. */
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const setView = vi.fn();
const apiMock = vi.fn();

vi.mock("../state/AppContext", () => ({
  useApp: () => ({
    currentUser: { name: "Ada Lovelace", role: "user" },
    roleRank: (r) => ({ user: 1, manager: 2, admin: 3 })[r] ?? 1,
    setView,
  }),
}));
vi.mock("../lib/api", () => ({ api: (...a) => apiMock(...a) }));
vi.mock("../lib/branding", () => ({ getAppName: () => "Bug Hunter" }));

import EmptyGuide from "./EmptyGuide.jsx";
import HelpHint from "./HelpHint.jsx";
import OnboardingHost from "./OnboardingHost.jsx";
import { emitOnboarding, takePendingGuide } from "./onboardingBus.js";

const FRESH = { welcome_seen: false, welcome_hidden: false, tour_status: "none", tour_step: 0 };

function serve(prefs) {
  apiMock.mockImplementation((path, opts) => Promise.resolve(opts?.method === "PUT" ? { ...prefs, ...opts.json } : prefs));
}
const puts = () => apiMock.mock.calls.filter((c) => c[1]?.method === "PUT").map((c) => c[1].json);

beforeEach(() => {
  apiMock.mockReset();
  setView.mockReset();
});
afterEach(cleanup);

describe("OnboardingHost", () => {
  it("greets a first-time user by name", async () => {
    serve(FRESH);
    render(<OnboardingHost />);
    expect(await screen.findByRole("dialog", { name: /Welcome to Bug Hunter, Ada/ })).toBeVisible();
  });

  it("stays quiet once seen or hidden", async () => {
    serve({ ...FRESH, welcome_seen: true });
    render(<OnboardingHost />);
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("stays quiet when preferences cannot be loaded", async () => {
    apiMock.mockRejectedValue(new Error("down"));
    render(<OnboardingHost />);
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Maybe later only hides it, Don't show again is saved", async () => {
    serve(FRESH);
    render(<OnboardingHost />);
    fireEvent.click(await screen.findByRole("button", { name: "Maybe later" }));
    expect(puts()).toEqual([]);
    act(() => emitOnboarding("welcome"));
    fireEvent.click(await screen.findByLabelText("Don't show this again"));
    fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
    expect(puts()).toEqual([{ welcome_seen: true, welcome_hidden: true }]);
  });

  it("Take the tour saves progress, then Skip tour pauses it and Resume continues", async () => {
    serve(FRESH);
    render(<OnboardingHost />);
    fireEvent.click(await screen.findByRole("button", { name: "Take the tour" }));
    expect(puts()[0]).toEqual({ welcome_seen: true, tour_status: "running", tour_step: 0 });
    expect(setView).toHaveBeenCalledWith("list");
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    expect(puts().at(-1)).toEqual({ tour_step: 1 });
    fireEvent.click(screen.getByRole("button", { name: "Skip tour" }));
    expect(puts().at(-1)).toEqual({ tour_status: "dismissed", tour_step: 1 });
    await waitFor(() => expect(screen.queryByText(/Step \d of/)).toBeNull());
    act(() => emitOnboarding("tour-resume"));
    expect(await screen.findByText("Step 2 of 5")).toBeInTheDocument();
  });

  it("resumes a running tour on load and finishing marks it done", async () => {
    serve({ ...FRESH, welcome_seen: true, tour_status: "running", tour_step: 4 });
    render(<OnboardingHost />);
    expect(await screen.findByText("Step 5 of 5")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Finish" }));
    expect(puts().at(-1)).toEqual({ tour_status: "done", tour_step: 0 });
  });

  it("Browse the guides opens the help center", async () => {
    serve(FRESH);
    render(<OnboardingHost />);
    fireEvent.click(await screen.findByRole("button", { name: "Browse the guides" }));
    expect(setView).toHaveBeenCalledWith("help");
  });
});

describe("HelpHint", () => {
  it("toggles, closes on Escape and returns focus", () => {
    render(<HelpHint label="Epic" guide="sprints">About epics</HelpHint>);
    const btn = screen.getByRole("button", { name: "Help: Epic" });
    expect(btn).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("About epics")).toBeVisible();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByText("About epics")).toBeNull();
    expect(btn).toHaveFocus();
  });

  it("Read the guide opens that guide", () => {
    render(<HelpHint label="Epic" guide="sprints">About epics</HelpHint>);
    fireEvent.click(screen.getByRole("button", { name: "Help: Epic" }));
    fireEvent.click(screen.getByRole("button", { name: "Read the guide" }));
    expect(setView).toHaveBeenCalledWith("help");
    expect(takePendingGuide()).toBe("sprints");
  });
});

describe("EmptyGuide", () => {
  it("offers the action and the guide", () => {
    const onClick = vi.fn();
    render(<EmptyGuide guide="items" action={{ label: "+ New Item", onClick }} hint="Start here" />);
    fireEvent.click(screen.getByRole("button", { name: "+ New Item" }));
    expect(onClick).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Read the guide" }));
    expect(takePendingGuide()).toBe("items");
    expect(screen.getByText("Start here")).toBeInTheDocument();
  });

  it("omits the action when the person may not take it", () => {
    render(<EmptyGuide guide="items" action={null} />);
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });
});
