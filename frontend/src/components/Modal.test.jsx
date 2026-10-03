/** Behavioral tests for the real `Modal` wrapper (`./Modal.jsx`).
 *
 * The wrapper is the contract every dialog in the app renders through, and the
 * Playwright suite drives those ids, so these tests pin the open/close flag,
 * the close control, the optional subtitle/headExtra and the card classes.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import Modal from "./Modal.jsx";

let container;
let root;

function mount(props, ...children) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(Modal, props, ...children));
  });
  return document.querySelector(".modal");
}

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  document.body.innerHTML = "";
});

describe("Modal", () => {
  it("hides itself when closed", () => {
    const modal = mount({ id: "modalBug", open: false, title: "Bug" });
    expect(modal.hidden).toBe(true);
    expect(modal.dataset.bhModal).toBeDefined();
  });

  it("shows itself with title and body when open", () => {
    const modal = mount({ id: "modalBug", open: true, title: "Bug" },
      React.createElement("p", null, "body"));
    expect(modal.hidden).toBe(false);
    expect(modal.querySelector("h2").textContent).toBe("Bug");
    expect(modal.querySelector("p").textContent).toBe("body");
  });

  it("renders the subtitle only when provided", () => {
    let modal = mount({ id: "m", open: true, title: "T" });
    expect(modal.querySelector(".modal-subtitle")).toBeNull();
    act(() => {
      root.render(React.createElement(Modal, { id: "m", open: true, title: "T", subtitle: "Story #12" }));
    });
    expect(modal.querySelector(".modal-subtitle").textContent).toBe("Story #12");
  });

  it("calls onClose from the close button", () => {
    const onClose = vi.fn();
    const modal = mount({ id: "m", open: true, title: "T", onClose });
    const btn = modal.querySelector("button.modal-close");
    expect(btn.getAttribute("aria-label")).toBe("Close");
    act(() => {
      btn.click();
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("composes size and extra card classes onto the card element", () => {
    const modal = mount({ id: "m", open: true, title: "T", size: "md", cardClass: "project-modal-content" });
    const card = modal.querySelector(".modal-card");
    expect(card.className).toBe("modal-card md project-modal-content");
  });

  it("renders headExtra next to the title and trims empty classes", () => {
    const modal = mount({
      id: "m",
      open: true,
      title: "T",
      headExtra: <span id="extra">R</span>,
    });
    expect(modal.querySelector(".modal-head #extra").textContent).toBe("R");
    expect(modal.querySelector(".modal-card").className).toBe("modal-card");
  });
});
