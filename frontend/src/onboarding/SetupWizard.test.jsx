/** First-admin setup wizard: steps, saving, idempotent resume, partial invitation failures, focus. */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.fn();
const refreshMe = vi.fn();
const loadProjects = vi.fn();
const app = { currentUser: { organization_name: "Acme", name: "Ada" }, projects: [], loadProjects, refreshMe };

vi.mock("../state/AppContext", () => ({ useApp: () => app }));
vi.mock("../lib/api", () => ({ api: (...a) => apiMock(...a) }));

import SetupWizard, { parseEmails, senderFrom } from "./SetupWizard.jsx";

const calls = (path, method) => apiMock.mock.calls.filter((c) => c[0] === path && (c[1]?.method ?? "GET") === method);
const handlers = {};

function Wizard({ step = 0 }) {
  return <SetupWizard open step={step} {...handlers} />;
}

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockImplementation(async (path) => (path === "/branding" ? { logo_data_url: "", accent_color: "", email_from_override: "" } : {}));
  refreshMe.mockReset();
  loadProjects.mockReset();
  app.projects = [];
  for (const k of ["onStep", "onClose", "onSkip", "onFinish", "onOpenAccount", "onOpenWebhooks", "onOpenGuide", "onTour"]) handlers[k] = vi.fn();
});
afterEach(cleanup);

describe("helpers", () => {
  it("builds the sender and parses address lists", () => {
    expect(senderFrom("", "")).toEqual({ value: "" });
    expect(senderFrom("Acme Bugs", "bugs@acme.com")).toEqual({ value: "Acme Bugs <bugs@acme.com>" });
    expect(senderFrom("", "bugs@acme.com")).toEqual({ value: "bugs@acme.com" });
    expect(senderFrom("Acme", "").error).toMatch(/email address/);
    expect(senderFrom("Acme", "nope").error).toMatch(/valid/);
    expect(senderFrom("A<b", "a@b.co").error).toMatch(/cannot contain/);
    expect(parseEmails("a@x.co, b@x.co;a@x.co\n c@x.co")).toEqual(["a@x.co", "b@x.co", "c@x.co"]);
  });
});

describe("SetupWizard", () => {
  it("is a labelled dialog with a progress list and the current step marked", () => {
    render(<Wizard step={1} />);
    expect(screen.getByRole("dialog", { name: "Set up Acme" })).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Setup progress" });
    expect(list.querySelector('[aria-current="step"]')).toHaveTextContent("First project");
    expect(screen.getByRole("heading", { name: /Step 2 of 5: First project/ })).toBeInTheDocument();
  });

  it("saves the name and branding together, then moves on", async () => {
    render(<Wizard />);
    const name = screen.getByLabelText(/Organization name/);
    await userEvent.clear(name);
    await userEvent.type(name, "Acme Labs");
    await userEvent.type(screen.getByPlaceholderText("#6366f1"), "#336699");
    await userEvent.type(screen.getByLabelText("Email sender name (optional)"), "Acme Bugs");
    await userEvent.type(screen.getByLabelText("Sender email address"), "bugs@acme.com");
    await userEvent.click(screen.getByRole("button", { name: "Save and continue" }));
    await waitFor(() => expect(handlers.onStep).toHaveBeenCalledWith(1));
    expect(calls("/organization", "PUT")[0][1].json).toEqual({ name: "Acme Labs" });
    expect(calls("/branding", "PUT")[0][1].json).toEqual({
      logo_data_url: "", accent_color: "#336699", email_from_override: "Acme Bugs <bugs@acme.com>",
    });
    expect(refreshMe).toHaveBeenCalled();
  });

  it("refuses a bad accent colour or a name with no address, without calling the server", async () => {
    render(<Wizard />);
    await userEvent.type(screen.getByPlaceholderText("#6366f1"), "blue");
    await userEvent.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/hex value/);
    await userEvent.clear(screen.getByPlaceholderText("#6366f1"));
    await userEvent.type(screen.getByLabelText("Email sender name (optional)"), "Acme");
    await userEvent.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/email address/);
    expect(calls("/branding", "PUT")).toHaveLength(0);
  });

  it("shows a server error and stays on the step", async () => {
    apiMock.mockImplementation(async (path, opts) => {
      if (opts?.method === "PUT") throw new Error("Nope");
      return { logo_data_url: "", accent_color: "", email_from_override: "" };
    });
    render(<Wizard />);
    await userEvent.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Nope");
    expect(handlers.onStep).not.toHaveBeenCalled();
  });

  it("creates the first project with Scrum, once", async () => {
    render(<Wizard step={1} />);
    await userEvent.type(screen.getByLabelText(/Project name/), "Website");
    await userEvent.click(screen.getByRole("checkbox", { name: /Use Scrum/ }));
    await userEvent.click(screen.getByRole("button", { name: "Create project" }));
    await waitFor(() => expect(handlers.onStep).toHaveBeenCalledWith(2));
    expect(calls("/projects", "POST")[0][1].json).toEqual({ name: "Website", agile_enabled: true });
    expect(loadProjects).toHaveBeenCalled();
  });

  it("continues without creating a second project when one exists and none is typed", async () => {
    app.projects = [{ id: 4, name: "Website" }];
    render(<Wizard step={1} />);
    expect(screen.getByRole("status")).toHaveTextContent("Website");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(handlers.onStep).toHaveBeenCalledWith(2);
    expect(calls("/projects", "POST")).toHaveLength(0);
  });

  it("invites each address, reports failures and keeps only the failed ones", async () => {
    app.projects = [{ id: 4, name: "Website" }];
    apiMock.mockImplementation(async (path, opts) => {
      if (path === "/invitations" && opts.json.email === "bad@x.co") throw new Error("Already a member");
      return {};
    });
    render(<Wizard step={2} />);
    await userEvent.type(screen.getByLabelText(/Email addresses/), "ok@x.co, bad@x.co");
    await userEvent.selectOptions(screen.getByLabelText("Role"), "manager");
    await userEvent.click(screen.getByRole("button", { name: "Send invitations" }));
    expect(await screen.findByText("Invitation sent to ok@x.co")).toBeInTheDocument();
    expect(screen.getByText("bad@x.co: Already a member")).toBeInTheDocument();
    expect(screen.getByLabelText(/Email addresses/)).toHaveValue("bad@x.co");
    expect(handlers.onStep).not.toHaveBeenCalled();
    expect(calls("/invitations", "POST")[0][1].json).toEqual({ email: "ok@x.co", role: "manager", project_ids: [4], as_lead: false });
  });

  it("rejects a malformed address and lets an empty list be skipped", async () => {
    render(<Wizard step={2} />);
    await userEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(handlers.onStep).toHaveBeenCalledWith(3);
    await userEvent.type(screen.getByLabelText(/Email addresses/), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Send invitations" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Not a valid email address: nope");
    expect(calls("/invitations", "POST")).toHaveLength(0);
  });

  it("offers the optional extras and the final step", () => {
    const { rerender } = render(<Wizard step={3} />);
    fireEvent.click(screen.getByRole("button", { name: "Open security settings" }));
    fireEvent.click(screen.getByRole("button", { name: "Open Organization" }));
    fireEvent.click(screen.getByRole("button", { name: "Read how" }));
    expect(handlers.onOpenAccount).toHaveBeenCalled();
    expect(handlers.onOpenWebhooks).toHaveBeenCalled();
    expect(handlers.onOpenGuide).toHaveBeenCalledWith("organizations");
    rerender(<Wizard step={4} />);
    fireEvent.click(screen.getByRole("button", { name: "Finish" }));
    expect(handlers.onFinish).toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Skip setup" })).toBeNull();
  });

  it("Skip setup calls through, and a new step takes focus", async () => {
    const { rerender } = render(<Wizard step={1} />);
    fireEvent.click(screen.getByRole("button", { name: "Skip setup" }));
    expect(handlers.onSkip).toHaveBeenCalled();
    rerender(<Wizard step={2} />);
    await waitFor(() => expect(screen.getByRole("heading", { name: /Step 3 of 5/ })).toHaveFocus());
  });
});
