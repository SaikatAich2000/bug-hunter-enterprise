/** Behavioral tests for the real `ChipPicker` component (`./ChipPicker.jsx`).
 *
 * Covers the production render + interaction contract the modals rely on:
 * selected-state ARIA, mouse and keyboard toggling, the locked (disabled)
 * mode, and the empty-state message.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import ChipPicker from "./ChipPicker.jsx";

let container;
let root;

function mount(props) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(ChipPicker, props));
  });
  return container;
}

function chip(label) {
  return Array.from(container.querySelectorAll(".chip")).find(
    (el) => el.textContent.trim() === label,
  );
}

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  document.body.innerHTML = "";
});

const ITEMS = [
  { id: 1, label: "Alice" },
  { id: 2, label: "Bob", title: "Bob's story" },
];

describe("ChipPicker", () => {
  it("renders the empty state when nothing is available", () => {
    mount({ items: [], selected: [], onToggle: vi.fn() });
    expect(container.querySelector(".chip-empty").textContent).toContain("none available");
    expect(container.querySelectorAll(".chip")).toHaveLength(0);
  });

  it("marks the selected chips with aria-pressed", () => {
    mount({ items: ITEMS, selected: [2], onToggle: vi.fn() });
    expect(chip("Alice").getAttribute("aria-pressed")).toBe("false");
    expect(chip("Bob · Bob's story").getAttribute("aria-pressed")).toBe("true");
    expect(chip("Bob · Bob's story").className).toContain("selected");
  });

  it("toggles an id on click and on Enter / Space", () => {
    const onToggle = vi.fn();
    mount({ items: ITEMS, selected: [], onToggle });

    act(() => {
      chip("Alice").click();
    });
    expect(onToggle).toHaveBeenLastCalledWith(1);

    act(() => {
      chip("Bob · Bob's story").dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
      );
    });
    expect(onToggle).toHaveBeenLastCalledWith(2);

    act(() => {
      chip("Bob · Bob's story").dispatchEvent(
        new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true }),
      );
    });
    expect(onToggle).toHaveBeenCalledTimes(3);
  });

  it("ignores unrelated keys", () => {
    const onToggle = vi.fn();
    mount({ items: ITEMS, selected: [], onToggle });
    act(() => {
      chip("Alice").dispatchEvent(
        new KeyboardEvent("keydown", { key: "Tab", bubbles: true }),
      );
    });
    expect(onToggle).not.toHaveBeenCalled();
  });

  it("is inert while locked", () => {
    const onToggle = vi.fn();
    mount({ items: ITEMS, selected: [1], onToggle, disabled: true });
    expect(container.querySelector(".chip-picker").className).toContain("locked");
    expect(chip("Alice").getAttribute("aria-disabled")).toBe("true");
    expect(chip("Alice").getAttribute("tabindex")).toBe("-1");
    act(() => {
      chip("Alice").click();
      chip("Alice").dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
      );
    });
    expect(onToggle).not.toHaveBeenCalled();
  });

  it("does not set aria-disabled while interactive", () => {
    mount({ items: ITEMS, selected: [], onToggle: vi.fn() });
    expect(chip("Alice").getAttribute("aria-disabled")).toBeNull();
  });

  it("passes the id through to the container", () => {
    mount({ items: ITEMS, selected: [], onToggle: vi.fn(), id: "assigneePicker" });
    expect(container.querySelector(".chip-picker").id).toBe("assigneePicker");
  });
});
