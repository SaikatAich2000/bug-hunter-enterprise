/** Behavioral tests for the real filter dropdown in `./MsFilter.jsx`.
 *
 * Exercises the production component: the button label rules, the
 * module-level "only one panel open" registry, click-outside/Escape closing,
 * row toggling (mouse + keyboard) and the empty-options state.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import MsFilter from "./MsFilter.jsx";

let container;
let root;

function mount(props) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(MsFilter, props));
  });
  return container;
}

function mountIn(target, props) {
  const host = document.createElement("div");
  target.appendChild(host);
  const r = createRoot(host);
  act(() => {
    r.render(React.createElement(MsFilter, props));
  });
  return r;
}

function toggleBtn() {
  return container.querySelector("[data-ms-toggle]");
}

function panel() {
  return container.querySelector(".ms-panel");
}

function row(value) {
  return container.querySelector(`[data-ms-value="${value}"]`);
}

const OPTIONS = [
  ["Low", "Low"],
  ["High", "High"],
];

afterEach(() => {
  act(() => root?.unmount());
  document.body.innerHTML = "";
});

describe("button label", () => {
  const base = { filterKey: "prio", label: "Priorities", noun: "Priorities", options: OPTIONS, selected: [], onToggle: vi.fn() };

  it("shows All <label> with nothing selected", () => {
    mount(base);
    expect(toggleBtn().querySelector(".ms-btn-label").textContent).toBe("All Priorities");
    expect(toggleBtn().className).not.toContain("active");
    expect(toggleBtn().getAttribute("aria-expanded")).toBe("false");
  });

  it("shows the single selected option's label", () => {
    mount({ ...base, selected: ["High"] });
    expect(toggleBtn().querySelector(".ms-btn-label").textContent).toBe("High");
    expect(toggleBtn().className).toContain("active");
  });

  it("falls back to the raw value when it is not in the options", () => {
    mount({ ...base, selected: ["Gone"] });
    expect(toggleBtn().querySelector(".ms-btn-label").textContent).toBe("Gone");
  });

  it("counts multiple selections with the noun", () => {
    mount({ ...base, selected: ["Low", "High", "Extra"] });
    expect(toggleBtn().querySelector(".ms-btn-label").textContent).toBe("Priorities (3)");
  });
});

describe("panel open/close", () => {
  const base = { filterKey: "prio", label: "Priorities", noun: "Priorities", options: OPTIONS, selected: [], onToggle: vi.fn() };

  it("opens on click and links the panel through aria-controls", () => {
    mount(base);
    expect(panel().hidden).toBe(true);
    act(() => {
      toggleBtn().click();
    });
    expect(panel().hidden).toBe(false);
    expect(toggleBtn().getAttribute("aria-expanded")).toBe("true");
    expect(toggleBtn().getAttribute("aria-controls")).toBe("ms-panel-prio");
    expect(panel().getAttribute("role")).toBe("menu");
  });

  it("closes again on a second click", () => {
    mount(base);
    act(() => {
      toggleBtn().click();
    });
    act(() => {
      toggleBtn().click();
    });
    expect(panel().hidden).toBe(true);
  });

  it("closes on an outside document click", () => {
    mount(base);
    act(() => {
      toggleBtn().click();
    });
    act(() => {
      document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(panel().hidden).toBe(true);
  });

  it("closes on Escape without letting the key reach a modal handler", () => {
    mount(base);
    act(() => {
      toggleBtn().click();
    });
    const outer = vi.fn();
    document.addEventListener("keydown", outer);
    act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
      );
    });
    document.removeEventListener("keydown", outer);
    expect(panel().hidden).toBe(true);
    expect(outer).not.toHaveBeenCalled();
  });

  it("keeps only one panel open across instances", () => {
    mount(base);
    act(() => {
      toggleBtn().click();
    });
    const secondRoot = mountIn(document.body, { ...base, filterKey: "type", label: "Types" });
    const secondWrap = document.body.lastElementChild;
    act(() => {
      secondWrap.querySelector("[data-ms-toggle]").click();
    });
    expect(panel().hidden).toBe(true); // first panel closed by the registry
    expect(secondWrap.querySelector(".ms-panel").hidden).toBe(false);
    act(() => secondRoot.unmount());
  });
});

describe("option rows", () => {
  const base = { filterKey: "prio", label: "Priorities", noun: "Priorities", options: OPTIONS, selected: ["Low"], onToggle: vi.fn() };

  it("reflects selection state and toggles through the callback", () => {
    const onToggle = vi.fn();
    mount({ ...base, onToggle });
    act(() => {
      toggleBtn().click();
    });
    expect(row("Low").getAttribute("aria-checked")).toBe("true");
    expect(row("Low").className).toContain("on");
    expect(row("Low").querySelector(".ms-check").textContent).toBe("✓");
    expect(row("High").getAttribute("aria-checked")).toBe("false");
    act(() => {
      row("High").click();
    });
    expect(onToggle).toHaveBeenCalledWith("High");
  });

  it("toggles from the keyboard too", () => {
    const onToggle = vi.fn();
    mount({ ...base, onToggle });
    act(() => {
      toggleBtn().click();
    });
    act(() => {
      row("High").dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
      );
      row("High").dispatchEvent(
        new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true }),
      );
      row("High").dispatchEvent(
        new KeyboardEvent("keydown", { key: "Tab", bubbles: true }),
      );
    });
    expect(onToggle).toHaveBeenCalledTimes(2);
  });

  it("shows the empty state when there are no options", () => {
    mount({ ...base, options: [] });
    act(() => {
      toggleBtn().click();
    });
    expect(panel().querySelector(".ms-empty").textContent).toBe("No options");
    expect(panel().querySelectorAll(".ms-row")).toHaveLength(0);
  });
});
