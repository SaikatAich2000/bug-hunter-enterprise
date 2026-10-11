/** Help center behaviour and accessibility. */
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it } from "vitest";

import HelpCenter from "./HelpCenter.jsx";
import { requestGuide } from "./onboardingBus.js";

afterEach(cleanup);

describe("HelpCenter", () => {
  it("shows the first guide and lists all of them", () => {
    render(<HelpCenter />);
    expect(screen.getByRole("heading", { level: 1, name: "Creating and tracking items" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /./ }).filter((b) => b.classList.contains("help-list-item"))).toHaveLength(9);
    expect(screen.getByRole("button", { name: "Creating and tracking items" })).toHaveAttribute("aria-current", "page");
  });

  it("opens a guide on click and moves focus to it", () => {
    render(<HelpCenter />);
    fireEvent.click(screen.getByRole("button", { name: "Roles and permissions" }));
    expect(screen.getByRole("heading", { level: 1, name: "Roles and permissions" })).toBeInTheDocument();
    expect(screen.getByRole("article")).toHaveFocus();
  });

  it("filters by search and reports when nothing matches", () => {
    render(<HelpCenter />);
    fireEvent.change(screen.getByLabelText("Search the guides"), { target: { value: "digest" } });
    expect(screen.getByRole("button", { name: "Notifications and push" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Roles and permissions" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Search the guides"), { target: { value: "qqqq" } });
    expect(screen.getByRole("status")).toHaveTextContent("No guide matches");
  });

  it("follows in-guide links without touching the URL hash", () => {
    render(<HelpCenter />);
    const before = window.location.hash;
    fireEvent.click(screen.getAllByRole("link", { name: "Sprints" })[0]);
    expect(screen.getByRole("heading", { level: 1, name: "Sprints, end to end" })).toBeInTheDocument();
    expect(window.location.hash).toBe(before);
  });

  it("opens a requested guide while already mounted", () => {
    render(<HelpCenter />);
    act(() => requestGuide("git"));
    expect(screen.getByRole("heading", { level: 1, name: "Git branches for stories" })).toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    const { container } = render(<HelpCenter />);
    const res = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(res.violations.map((v) => v.id)).toEqual([]);
  });
});
