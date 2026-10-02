/** Behavioral tests for the real item-linking picker (`./ItemPicker.jsx`).
 *
 * Exercises the production search combobox the bug modal links items through:
 * chip rendering/removal, open + initial search, debounced querying with type
 * tabs, exclusion of current/linked items, stale-response dropping,
 * keyboard selection, and disabled-state wiring.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.fn();

vi.mock("../lib/api", () => ({
  api: (...args) => apiMock(...args),
  ApiError: class ApiError extends Error {},
  apiBlob: vi.fn(),
}));

import ItemPicker from "./ItemPicker.jsx";

const ITEMS = [
  { id: 1, title: "Checkout crashes", item_type: "Bug", status: "New" },
  { id: 2, title: "Receipts PDF", item_type: "Requirement", status: "Planned" },
];

let container;
let root;

function mount(props) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(ItemPicker, {
      selected: [],
      onChange: vi.fn(),
      excludeIds: [],
      ...props,
    }));
  });
  return container;
}

function trigger() {
  return container.querySelector(".item-picker-trigger");
}

function searchInput() {
  return container.querySelector(".item-picker-search");
}

function rows() {
  return Array.from(container.querySelectorAll(".item-picker-row"));
}

async function openAndFlush() {
  act(() => {
    trigger().click();
  });
  await act(async () => {});
}

function search(q) {
  act(() => {
    const el = searchInput();
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(el, q);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockResolvedValue({ items: ITEMS });
  vi.useFakeTimers();
});

afterEach(() => {
  act(() => root?.unmount());
  document.body.innerHTML = "";
  vi.useRealTimers();
});

describe("ItemPicker chips", () => {
  it("renders selected chips and removes through the chip button", () => {
    const onChange = vi.fn();
    mount({ selected: [ITEMS[0]], onChange });
    expect(container.querySelector(".item-picker-chip-id").textContent).toBe("#1");
    act(() => {
      container.querySelector(".item-picker-clear").click();
    });
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("labels an empty picker with the search placeholder", () => {
    mount({ selected: [] });
    expect(container.querySelector(".bh-sel-label").textContent).toContain("Search items");
    expect(container.querySelector(".item-picker-chips")).toBeNull();
  });

  it("stays inert while disabled", () => {
    const onChange = vi.fn();
    const el = mount({ selected: [ITEMS[0]], onChange, disabled: true });
    expect(trigger().disabled).toBe(true);
    act(() => {
      trigger().click();
    });
    expect(container.querySelector(".item-picker-pop")).toBeNull();
    expect(el.querySelector(".item-picker-clear").disabled).toBe(true);
    expect(el.querySelector(".bh-sel-label").textContent).toContain("1 selected");
  });
});

describe("ItemPicker search", () => {
  it("searches on open and drops excluded ids from the results", async () => {
    mount({ excludeIds: [1] });
    await openAndFlush();
    expect(apiMock).toHaveBeenCalledWith(expect.stringContaining("/bugs?"));
    const titles = rows().map((r) => r.querySelector(".item-picker-row-title").textContent);
    expect(titles).toEqual(["Receipts PDF"]);
    expect(container.querySelector(".item-picker-list").getAttribute("role")).toBe("listbox");
  });

  it("debounces typing and passes the trimmed query and type tab", async () => {
    mount({});
    await openAndFlush();
    search("  crash ");
    expect(apiMock).toHaveBeenCalledTimes(1); // debounce has not fired yet
    await act(async () => {
      vi.advanceTimersByTime(200);
    });
    const last = apiMock.mock.calls.at(-1).at(0);
    expect(last).toContain("q=crash");

    act(() => {
      container.querySelectorAll(".item-picker-tab")[1].click();
    });
    await act(async () => {});
    expect(apiMock.mock.calls.at(-1).at(0)).toContain("item_type=Bug");
  });

  it("toggles an item from the results and keeps the dropdown open", async () => {
    const onChange = vi.fn();
    mount({ onChange });
    await openAndFlush();
    act(() => {
      rows()[0].click();
    });
    expect(onChange).toHaveBeenCalledWith([ITEMS[0]]);
    expect(container.querySelector(".item-picker-pop")).not.toBeNull();
  });

  it("marks already-selected rows as checked", async () => {
    mount({ selected: [ITEMS[1]] });
    await openAndFlush();
    expect(rows()[1].getAttribute("aria-selected")).toBe("true");
    expect(rows()[1].className).toContain("is-checked");
    expect(rows()[0].getAttribute("aria-selected")).toBe("false");
  });

  it("hides the type tabs for a fixed-type picker", async () => {
    mount({ fixedType: "Bug" });
    await openAndFlush();
    const tabsGone = container.querySelector(".item-picker-tabs");
    expect(tabsGone).toBeNull();
    const last = apiMock.mock.calls.at(-1).at(0);
    expect(last).toContain("item_type=Bug");
  });

  it("shows an empty state when the search fails or finds nothing", async () => {
    apiMock.mockRejectedValueOnce(new Error("offline"));
    mount({});
    await openAndFlush();
    expect(container.textContent).toContain("No matching items");
  });
});

describe("ItemPicker keyboard", () => {
  it("selects with Enter and closes with Escape", async () => {
    const onChange = vi.fn();
    mount({ onChange });
    await openAndFlush();
    act(() => {
      searchInput().dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
      );
    });
    expect(onChange).toHaveBeenCalledWith([ITEMS[0]]);

    act(() => {
      searchInput().dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
      );
    });
    expect(container.querySelector(".item-picker-pop")).toBeNull();
  });

  it("moves the active row with the arrow keys", async () => {
    mount({});
    await openAndFlush();
    const active = () => container.querySelector(
      ".item-picker-row.is-active .item-picker-row-title",
    ).textContent;
    expect(active()).toBe("Checkout crashes");
    act(() => {
      searchInput().dispatchEvent(
        new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }),
      );
    });
    expect(active()).toBe("Receipts PDF");
    act(() => {
      searchInput().dispatchEvent(
        new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true, cancelable: true }),
      );
    });
    expect(active()).toBe("Checkout crashes");
  });
});
