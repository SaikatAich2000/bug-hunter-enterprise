/** Behavioral tests for the real agile visuals in `./agileShared.jsx`.
 *
 * Covers the sprint-board/backlog helpers and badges the board and backlog
 * views render with: type icons, epic colour fallback, avatar stacks,
 * story-point badges and date ranges.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import {
  AssigneeStack,
  StoryPointBadge,
  defaultSprintName,
  epicColor,
  epicTitle,
  formatDateRange,
  sumStoryPoints,
  typeEmoji,
} from "./agileShared.jsx";

let container;
let root;

function mount(element) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(element);
  });
  return container;
}

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  document.body.innerHTML = "";
});

describe("typeEmoji", () => {
  it("gives every type its own icon, Task and Sub-task included", () => {
    expect(typeEmoji("Epic")).toBe("⚡");
    expect(typeEmoji("Task")).toBe("☑️");
    expect(typeEmoji("Sub-task")).toBe("🔹");
    expect(typeEmoji("Task")).not.toBe(typeEmoji("Sub-task"));
  });

  it("maps the standard types and falls back for anything unknown", () => {
    expect(typeEmoji("Story")).toBe("📗");
    expect(typeEmoji("Bug")).toBe("🐞");
    expect(typeEmoji("Requirement")).toBe("📐");
    expect(typeEmoji("Collection")).toBe("📝");
    expect(typeEmoji(undefined)).toBe("📝");
  });
});

describe("epic colouring", () => {
  const epics = new Map([
    [1, { title: "Payments", color: "#123456" }],
    [2, { title: "Search" }],
  ]);

  it("uses the saved epic colour when present", () => {
    expect(epicColor(1, epics)).toBe("#123456");
  });

  it("falls back to a stable palette slot when the epic has no colour", () => {
    const first = epicColor(2, epics);
    expect(first).toMatch(/^#/);
    // Deterministic for the same id, so board re-renders cannot reshuffle bars.
    expect(epicColor(2, epics)).toBe(first);
    expect(epicColor(9, epics)).toBe(epicColor(9, epics));
  });

  it("returns null when no epic is attached", () => {
    expect(epicColor(null, epics)).toBeNull();
    expect(epicColor(undefined, epics)).toBeNull();
  });

  it("exposes the epic title or null", () => {
    expect(epicTitle(1, epics)).toBe("Payments");
    expect(epicTitle(99, epics)).toBeNull();
  });
});

describe("AssigneeStack", () => {
  it("renders nothing when unassigned", () => {
    expect(mount(React.createElement(AssigneeStack, { assignees: [] })).textContent).toBe("");
    expect(mount(React.createElement(AssigneeStack, {})).textContent).toBe("");
  });

  it("shows up to three initials with an overflow counter", () => {
    const assignees = [
      { id: 1, name: "Kunal Ash" },
      { id: 2, name: "Manish" },
      { id: 3, name: "Ritam Hudait" },
      { id: 4, name: "Saikat Aich" },
    ];
    const el = mount(React.createElement(AssigneeStack, { assignees }));
    const avatars = el.querySelectorAll(".avatar");
    expect(avatars).toHaveLength(4);
    expect(avatars[0].textContent).toBe("KA");
    expect(avatars[2].getAttribute("title")).toBe("Ritam Hudait");
    expect(avatars[3].textContent).toBe("+1");
    expect(avatars[3].getAttribute("title")).toBe("+1 more");
  });

  it("has no overflow chip at exactly three assignees", () => {
    const el = mount(React.createElement(AssigneeStack, {
      assignees: [{ id: 1, name: "A B" }, { id: 2, name: "C D" }, { id: 3, name: "E F" }],
    }));
    expect(el.querySelectorAll(".avatar")).toHaveLength(3);
    expect(el.querySelector(".avatar-more")).toBeNull();
  });
});

describe("StoryPointBadge", () => {
  it("renders nothing without a value", () => {
    expect(mount(React.createElement(StoryPointBadge, { value: null })).textContent).toBe("");
    expect(mount(React.createElement(StoryPointBadge, {})).textContent).toBe("");
  });

  it("pluralises the tooltip and keeps 0 visible", () => {
    mount(React.createElement(StoryPointBadge, { value: 1 }));
    expect(container.querySelector(".sp-badge").getAttribute("title")).toBe("1 story point");
    act(() => {
      root.render(React.createElement(StoryPointBadge, { value: 5 }));
    });
    expect(container.querySelector(".sp-badge").getAttribute("title")).toBe("5 story points");
    act(() => {
      root.render(React.createElement(StoryPointBadge, { value: 0 }));
    });
    expect(container.querySelector(".sp-badge").textContent).toBe("0");
  });
});

describe("date range + story points", () => {
  it("renders a start and end separated by the range dash", () => {
    const out = formatDateRange("2026-09-01", "2026-09-14");
    expect(out).toContain("–");
    expect(out).not.toContain("?");
    const [start, end] = out.split(" – ");
    expect(start).not.toBe("");
    expect(end).not.toBe("");
    expect(start).not.toBe(end);
  });

  it("uses a placeholder for a missing end and blanks both-missing", () => {
    const out = formatDateRange("2026-09-01", "");
    expect(out).toContain("?");
    expect(out.split(" – ")).toHaveLength(2);
    expect(formatDateRange("", "")).toBe("");
  });

  it("passes an unparseable date through unchanged", () => {
    expect(formatDateRange("not-a-date", "2026-09-14")).toContain("not-a-date");
  });

  it("sums story points while ignoring missing values", () => {
    expect(sumStoryPoints([{ story_points: 3 }, { story_points: null }, { story_points: "2" }])).toBe(5);
    expect(sumStoryPoints([])).toBe(0);
  });
});


describe("defaultSprintName", () => {
  it("never doubles the word Sprint", () => {
    expect(defaultSprintName("Sprint Board", [{ name: "Sprint 1" }])).toBe("Sprint 2");
    expect(defaultSprintName("", [])).toBe("Sprint 1");
  });

  it("keeps a board's own name and skips names in use", () => {
    expect(defaultSprintName("Mobile Board", [])).toBe("Mobile Sprint 1");
    expect(defaultSprintName("Mobile Board", [{ name: "x" }, { name: "Mobile Sprint 3" }])).toBe("Mobile Sprint 4");
  });
});
