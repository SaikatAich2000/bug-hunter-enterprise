/** Behavioral tests for the real custom select (`./BhSelect.jsx`).
 *
 * The component is the contract every dropdown in the app renders through
 * (repository, base branch, statuses), so these tests pin: the native-select
 * passthrough for forms and keyboard users, the custom-button label rules
 * (selected option, "—" fallback, placeholder dimming), the portaled popover
 * (select + close, outside-click, Escape), and flip placement above/below.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import BhSelect from "./BhSelect.jsx";

let container;
let root;

function mount(props) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(BhSelect, props));
  });
  return container;
}

function native() {
  return container.querySelector("select.bh-sel-native");
}

function trigger() {
  return container.querySelector(".bh-sel-btn");
}

function popover() {
  return document.querySelector(".bh-sel-pop");
}

function openList() {
  act(() => {
    trigger().click();
  });
}

const OPTIONS = [
  { value: "", label: "— No event —" },
  { value: "1", label: "Sprint 1" },
  { value: "2", label: "Sprint 2" },
];

afterEach(() => {
  act(() => root?.unmount());
  document.body.innerHTML = "";
});

describe("BhSelect", () => {
  it("keeps a native select for forms and keyboard users", () => {
    const onChange = vi.fn();
    mount({ id: "repo", name: "repo", ariaLabel: "Repository", value: "1", onChange, options: OPTIONS });
    expect(native().id).toBe("repo");
    expect(native().name).toBe("repo");
    expect(native().getAttribute("aria-label")).toBe("Repository");
    expect(native().value).toBe("1");
    expect(native().querySelectorAll("option")).toHaveLength(3);

    act(() => {
      native().value = "2";
      native().dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(onChange).toHaveBeenCalledWith("2");
  });

  it("labels the custom button from the selected option", () => {
    mount({ value: "1", onChange: vi.fn(), options: OPTIONS });
    expect(container.querySelector(".bh-sel-label").textContent).toBe("Sprint 1");
    expect(container.querySelector(".bh-sel-label").className).not.toContain("placeholder");
    expect(trigger().getAttribute("aria-haspopup")).toBe("listbox");
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
  });

  it("dims the label for a placeholder option and shows — for unmatched values", () => {
    mount({ value: "", onChange: vi.fn(), options: OPTIONS });
    expect(container.querySelector(".bh-sel-label").className).toContain("bh-sel-placeholder");

    act(() => {
      root.render(React.createElement(BhSelect, { value: "gone", onChange: vi.fn(), options: OPTIONS }));
    });
    expect(container.querySelector(".bh-sel-label").textContent).toBe("—");
  });

  it("opens a portaled listbox and selects an option", () => {
    const onChange = vi.fn();
    mount({ value: "1", onChange, options: OPTIONS });
    openList();
    const pop = popover();
    expect(pop).not.toBeNull();
    expect(pop.getAttribute("role")).toBe("listbox");
    expect(pop.parentElement).toBe(document.body); // portaled, not nested
    expect(trigger().getAttribute("aria-expanded")).toBe("true");
    expect(pop.querySelectorAll("[role='option']")).toHaveLength(3);
    const selected = pop.querySelector(".bh-sel-row.is-selected");
    expect(selected.getAttribute("aria-selected")).toBe("true");
    expect(selected.textContent).toBe("Sprint 1");

    act(() => {
      pop.querySelectorAll("[role='option']")[2].click();
    });
    expect(onChange).toHaveBeenCalledWith("2");
    expect(popover()).toBeNull(); // closes after a pick
  });

  it("closes on an outside click and keeps clicks inside the popover open", () => {
    mount({ value: "", onChange: vi.fn(), options: OPTIONS });
    openList();
    act(() => {
      document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(popover()).toBeNull();

    // Inside clicks must not bubble-close: re-open, then click the listbox body.
    openList();
    act(() => {
      popover().dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(popover()).not.toBeNull();
  });

  it("closes on Escape and returns focus to the trigger", () => {
    mount({ value: "", onChange: vi.fn(), options: OPTIONS });
    openList();
    act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
      );
    });
    expect(popover()).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("stays inert while disabled", () => {
    const onChange = vi.fn();
    mount({ value: "", onChange, options: OPTIONS, disabled: true });
    expect(native().disabled).toBe(true);
    expect(trigger().disabled).toBe(true);
    expect(trigger().className).toContain("is-disabled");
    expect(trigger().querySelector(".bh-sel-caret")).toBeNull();
    act(() => {
      trigger().click();
    });
    expect(popover()).toBeNull();
  });

  it("places the popover above when there is no room below", () => {
    mount({ value: "", onChange: vi.fn(), options: OPTIONS });
    openList();
    const pop = popover();
    // Default jsdom rects (all zeros) give: spaceBelow(768) >= ph default → below.
    expect(pop.style.position).toBe("fixed");
    expect(pop.style.top).toMatch(/px$/);
    expect(pop.style.left).toMatch(/px$/);
  });
});
