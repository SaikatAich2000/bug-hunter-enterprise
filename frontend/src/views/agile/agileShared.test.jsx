/** Behavioral tests for the real agile visuals in `./agileShared.jsx`.
 *
 * Covers the sprint-board/backlog helpers and badges the board and backlog
 * views render with: type icons, epic colour fallback, avatar stacks,
 * story-point badges, date ranges and cadence-derived end dates.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import {
  AssigneeStack,
  StoryPointBadge,
  epicColor,
  epicTitle,
  formatDateRange,
  sprintPeriods,
  suggestEndDate,
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

describe("suggestEndDate", () => {
  it("returns an empty suggestion without a start date or for custom cadence", () => {
    expect(suggestEndDate("", "weekly")).toBe("");
    expect(suggestEndDate("2026-09-01", "")).toBe("");
    expect(suggestEndDate("2026-09-01", "custom")).toBe("");
    expect(suggestEndDate("2026-09-01", "fortnightly")).toBe("");
    expect(suggestEndDate("bad-date", "weekly")).toBe("");
  });

  it("suggests the inclusive end of one period", () => {
    expect(suggestEndDate("2026-09-01", "daily")).toBe("2026-09-01");
    expect(suggestEndDate("2026-09-01", "weekly")).toBe("2026-09-07");
    expect(suggestEndDate("2026-09-01", "monthly")).toBe("2026-09-30");
  });

  it("clamps a month-end start to the shorter next month", () => {
    expect(suggestEndDate("2026-01-31", "monthly")).toBe("2026-02-27");
  });
});

describe("sprintPeriods", () => {
  it("splits a range into inclusive, contiguous periods cut at the end date", () => {
    expect(sprintPeriods("2026-09-01", "2026-09-17", "weekly")).toEqual([
      { start_date: "2026-09-01", end_date: "2026-09-07" },
      { start_date: "2026-09-08", end_date: "2026-09-14" },
      { start_date: "2026-09-15", end_date: "2026-09-17" },
    ]);
  });

  it("anchors monthly periods to the start day across short months", () => {
    expect(sprintPeriods("2026-01-31", "2026-04-29", "monthly").map((p) => p.start_date))
      .toEqual(["2026-01-31", "2026-02-28", "2026-03-31"]);
  });

  it("returns one period for a custom cadence and none for an inverted range", () => {
    expect(sprintPeriods("2026-09-01", "2026-09-20", "custom"))
      .toEqual([{ start_date: "2026-09-01", end_date: "2026-09-20" }]);
    expect(sprintPeriods("2026-09-20", "2026-09-01", "weekly")).toEqual([]);
  });
});


  it("exposes the epic title or null", () => {
    expect(epicTitle(1, epics)).toBe("Payments");
    expect(epicTitle(99, epics)).toBeNull();
  });
});
