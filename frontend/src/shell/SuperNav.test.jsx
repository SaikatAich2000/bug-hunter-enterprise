/** Behavioral tests for the real second chrome band (`./SuperNav.jsx`).
 *
 * The band owns the type tabs and the list-only search input whose value is
 * debounce-pushed into the shared filters — these tests pin the view gating,
 * the search push, and the external-clear adoption that avoids echo loops.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const state = {
  view: "list",
  filters: { q: "" },
  setFilters: vi.fn(),
};

vi.mock("../state/AppContext", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useApp: () => state };
});

import SuperNav from "./SuperNav.jsx";

let container;
let root;

function mount() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(SuperNav));
  });
  return container;
}

function input() {
  return container.querySelector("#search");
}

function setSearch(value) {
  act(() => {
    const el = input();
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

beforeEach(() => {
  state.view = "list";
  state.filters = { q: "" };
  state.setFilters.mockClear();
  vi.useFakeTimers();
});

afterEach(() => {
  act(() => root?.unmount());
  document.body.innerHTML = "";
  vi.useRealTimers();
});

describe("SuperNav", () => {
  it("renders the type tabs and the search box on the list view", () => {
    mount();
    expect(container.querySelector("#typeTabs")).not.toBeNull();
    expect(input()).not.toBeNull();
    expect(input().getAttribute("autocomplete")).toBe("off");
  });

  it("hides the search box but keeps the tabs on analytics", () => {
    state.view = "analytics";
    mount();
    expect(container.querySelector("#typeTabs")).not.toBeNull();
    expect(input()).toBeNull();
  });

  it("renders nothing outside list/analytics", () => {
    state.view = "events";
    expect(mount().textContent).toBe("");
    state.view = "list";
  });

  it("debounces typed searches into the shared filters", () => {
    mount();
    setSearch("  checkout  ");
    expect(state.setFilters).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(state.setFilters).toHaveBeenCalledTimes(1);
    const updater = state.setFilters.mock.calls[0][0];
    expect(updater({ q: "old" }).q).toBe("checkout");
  });

  it("keeps the visible text while deferring the push", () => {
    mount();
    setSearch("abc");
    expect(input().value).toBe("abc");
  });

  it("adopts an external filter clear instead of echoing it back", () => {
    mount();
    setSearch("xyz");
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(state.setFilters).toHaveBeenCalledTimes(1);

    // The context applies the pushed value (re-render adopts it silently,
    // replacing the user's text with the shared filter value).
    state.filters = { q: "xyz" };
    setSearch("xyz ");
    expect(input().value).toBe("xyz");

    // Another surface (Clear button) empties the shared filter; the next
    // render must adopt it rather than echo the typing back.
    state.filters = { q: "" };
    setSearch("z");
    expect(input().value).toBe("");
    // No extra search was pushed by the adoption itself.
    expect(state.setFilters).toHaveBeenCalledTimes(1);
  });
});
