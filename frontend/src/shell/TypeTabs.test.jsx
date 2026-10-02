/** Behavioral tests for the real type tabs (`./TypeTabs.jsx`).
 *
 * The tabs read counts from the app stats and write the active tab back into
 * the shared context, so these tests (with a stubbed AppContext) pin the
 * visibility rule (list/analytics only), the unfiltered count math, and the
 * tab switching contract the list view depends on.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = {
  view: "list",
  stats: { by_type: { Bug: 3, Requirement: 2, Task: 4 } },
  activeTab: "all",
  setActiveTab: vi.fn(),
};

vi.mock("../state/AppContext", () => ({
  useApp: () => state,
}));

import TypeTabs from "./TypeTabs.jsx";

let container;
let root;

function mount() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(TypeTabs));
  });
  return container;
}

function tab(name) {
  return container.querySelector(`[data-tab="${name}"]`);
}

function count(name) {
  return tab(name).querySelector(".type-tab-count").textContent;
}

afterEach(() => {
  act(() => root?.unmount());
  document.body.innerHTML = "";
  state.view = "list";
  state.activeTab = "all";
  state.setActiveTab.mockClear();
});

describe("TypeTabs", () => {
  it("shows global, unfiltered counts with an All total", () => {
    mount();
    expect(count("Bug")).toBe("3");
    expect(count("Requirement")).toBe("2");
    expect(count("Task")).toBe("4");
    expect(count("all")).toBe("9");
  });

  it("treats missing stats as zeros", () => {
    state.stats = null;
    mount();
    expect(count("all")).toBe("0");
    expect(count("Bug")).toBe("0");
    state.stats = { by_type: { Bug: 3, Requirement: 2, Task: 4 } };
  });

  it("marks the active tab and switches on click", () => {
    state.activeTab = "Task";
    mount();
    expect(tab("Task").className).toContain("active");
    expect(tab("Task").getAttribute("aria-selected")).toBe("true");
    expect(tab("Bug").getAttribute("aria-selected")).toBe("false");

    act(() => {
      tab("Bug").click();
    });
    expect(state.setActiveTab).toHaveBeenCalledWith("Bug");
  });

  it("only renders on the list and analytics views", () => {
    mount();
    expect(container.querySelector("#typeTabs").style.display).toBe("");

    state.view = "analytics";
    mount();
    expect(container.querySelector("#typeTabs").style.display).toBe("");

    state.view = "events";
    mount();
    expect(container.querySelector("#typeTabs").style.display).toBe("none");
  });

  it("carries the accessible tablist wiring", () => {
    mount();
    const list = container.querySelector("#typeTabs");
    expect(list.getAttribute("role")).toBe("tablist");
    expect(list.getAttribute("aria-label")).toBe("Work item type");
    expect(tab("Bug").getAttribute("role")).toBe("tab");
    expect(tab("Bug").textContent).toContain("Bugs");
  });
});
