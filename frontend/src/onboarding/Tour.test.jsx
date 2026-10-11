/** Product tour: stepping, pausing, keyboard, focus trap and return. */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import Tour, { placeCard } from "./Tour.jsx";
import { stepsFor } from "./tourSteps.js";

// The plain-user track: work items, sprints, events, Sleuth, settings.
const TOUR_STEPS = stepsFor(() => true, "user");

afterEach(cleanup);

function Harness({ onPause = () => {}, onFinish = () => {} }) {
  const [i, setI] = useState(0);
  return <Tour steps={TOUR_STEPS} index={i} onIndex={setI} onPause={onPause} onFinish={onFinish} />;
}

describe("Tour", () => {
  it("walks forward and back and finishes on the last step", () => {
    const onFinish = vi.fn();
    render(<Harness onFinish={onFinish} />);
    expect(screen.getByRole("dialog")).toHaveAccessibleName("Work items");
    expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("dialog")).toHaveAccessibleName("Sprints");
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByText("Step 1 of 5")).toBeInTheDocument();
    for (let n = 0; n < 4; n++) fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Finish" }));
    expect(onFinish).toHaveBeenCalled();
  });

  it("pauses with Skip tour and with Escape", () => {
    const onPause = vi.fn();
    render(<Harness onPause={onPause} />);
    fireEvent.click(screen.getByRole("button", { name: "Skip tour" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onPause).toHaveBeenCalledTimes(2);
  });

  it("takes focus, keeps Tab inside and gives focus back when closed", () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    const { unmount } = render(<Harness />);
    const card = screen.getByRole("dialog");
    expect(card).toHaveFocus();
    const next = screen.getByRole("button", { name: "Next" });
    next.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(screen.getByRole("button", { name: "Skip tour" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(next).toHaveFocus();
    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it("points at its target when present", () => {
    const btn = document.createElement("button");
    btn.id = "profileBtn";
    btn.getBoundingClientRect = () => ({ top: 10, left: 20, width: 100, height: 30 });
    document.body.appendChild(btn);
    const { container } = render(<Tour steps={TOUR_STEPS} index={TOUR_STEPS.length - 1} onIndex={() => {}} onPause={() => {}} onFinish={() => {}} />);
    expect(container.querySelector(".tour-spot")).not.toBeNull();
    btn.remove();
  });
});

describe("placeCard", () => {
  it("centres when there is no target and stays on screen", () => {
    const p = placeCard(null, { width: 340, height: 180 }, { width: 1000, height: 800 });
    expect(p.left).toBe(330);
    const narrow = placeCard({ top: 5, left: 0, width: 50, height: 20 }, { width: 340, height: 180 }, { width: 320, height: 600 });
    expect(narrow.left).toBeGreaterThanOrEqual(12);
    expect(narrow.left + narrow.width).toBeLessThanOrEqual(320);
  });

  it("flips above the target near the bottom edge", () => {
    const p = placeCard({ top: 700, left: 100, width: 50, height: 30 }, { width: 340, height: 180 }, { width: 1000, height: 800 });
    expect(p.top).toBeLessThan(700);
  });
});

describe("stepsFor", () => {
  it("hides steps the role cannot open", () => {
    expect(stepsFor(() => false).some((s) => s.id === "reports")).toBe(false);
    expect(stepsFor(() => true, "manager").some((s) => s.id === "reports")).toBe(true);
    expect(stepsFor(() => true, "user").some((s) => s.id === "reports")).toBe(false);
    expect(stepsFor(() => true, "admin")).toHaveLength(9);
  });
});
