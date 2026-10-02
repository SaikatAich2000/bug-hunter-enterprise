/** Behavioral tests for the real KPI strip (`./KpiStrip.jsx`).
 *
 * The tiles drive the shared status filter, so these tests (with a stubbed
 * AppContext) pin the numbers, the active-tile comparison as a set, the
 * toggle-off-when-active rule, and the list/analytics visibility rule.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const state = {
  view: "list",
  stats: { bugs: 10, open: 4, resolved: 3, closed: 2, resolve_later: 1 },
  filters: { status: [] },
  setFilters: vi.fn(),
};

vi.mock("../state/AppContext", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useApp: () => state };
});

import KpiStrip from "./KpiStrip.jsx";

let container;
let root;

function mount() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(KpiStrip));
  });
  return container;
}

function tile(key) {
  return container.querySelector(`[data-kpi="${key}"]`);
}

beforeEach(() => {
  state.filters = { status: [] };
  state.setFilters.mockClear();
});

afterEach(() => {
  act(() => root?.unmount());
  document.body.innerHTML = "";
});

describe("KpiStrip", () => {
  it("renders the five tiles with their numbers and labels", () => {
    mount();
    expect(tile("total").textContent).toContain("10");
    expect(tile("open").textContent).toContain("4");
    expect(tile("resolved").textContent).toContain("3");
    expect(tile("closed").textContent).toContain("2");
    expect(tile("resolve_later").textContent).toContain("1");
    expect(tile("total").getAttribute("aria-label")).toBe("Show all bugs");
    expect(container.querySelectorAll(".kpi")).toHaveLength(5);
    expect(tile("total").className).toContain("active"); // empty filter = All
  });

  it("treats missing stats as zeros", () => {
    state.stats = null;
    mount();
    expect(tile("total").textContent).toContain("0");
    state.stats = { bugs: 10, open: 4, resolved: 3, closed: 2, resolve_later: 1 };
  });

  it("marks only the tile whose status set matches the filter", () => {
    state.filters = { status: ["Resolved"] };
    mount();
    expect(tile("total").className).not.toContain("active");
    expect(tile("resolved").className).toContain("active");
    expect(tile("open").className).not.toContain("active");
  });

  it("applies a tile's filter set on click", () => {
    mount();
    act(() => {
      tile("resolved").click();
    });
    expect(state.setFilters).toHaveBeenCalledTimes(1);
    const updater = state.setFilters.mock.calls[0][0];
    expect(updater({ status: ["Old"] }).status).toEqual(["Resolved"]);
  });

  it("clears the filter again when the active tile is clicked", () => {
    state.filters = { status: ["Resolved"] };
    mount();
    act(() => {
      tile("resolved").click();
    });
    const updater = state.setFilters.mock.calls[0][0];
    expect(updater({ status: ["Resolved"] }).status).toEqual([]);
  });

  it("re-applies the same set from a cleared filter (toggle back on)", () => {
    state.filters = { status: [] };
    mount();
    act(() => {
      tile("resolved").click();
    });
    const updater = state.setFilters.mock.calls[0][0];
    expect(updater({ status: [] }).status).toEqual(["Resolved"]);
  });

  it("renders only on the list and analytics views", () => {
    mount();
    expect(container.querySelector("#kpiStrip").style.display).toBe("");
    state.view = "analytics";
    mount();
    expect(container.querySelector("#kpiStrip").style.display).toBe("");
    state.view = "events";
    mount();
    expect(container.querySelector("#kpiStrip").style.display).toBe("none");
    state.view = "list";
  });
});
