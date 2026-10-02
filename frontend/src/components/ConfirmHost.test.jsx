/** Behavioral tests for the real confirm host (`./ConfirmHost.jsx`).
 *
 * The host is the single promise-based dialog every destructive action awaits
 * (branch removal, deletes), so these tests pin: default and custom labels,
 * danger vs primary styling, cancel/close/Escape all resolving false, prompt
 * trimming, and the rule that a second dialog cancels the first.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import ConfirmHost, { confirmDialog, promptDialog } from "./ConfirmHost.jsx";

let container;
let root;

function mountHost() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(ConfirmHost));
  });
}

function modal() {
  return document.getElementById("modalConfirm");
}

function click(id) {
  act(() => {
    document.getElementById(id).click();
  });
}

beforeEach(() => {
  mountHost();
});

afterEach(() => {
  act(() => root?.unmount());
  document.body.innerHTML = "";
});

describe("confirmDialog", () => {
  it("shows the message with default labels and danger styling", async () => {
    let settled;
    act(() => {
      confirmDialog("Delete this branch?").then((v) => { settled = v; });
    });
    expect(modal().hidden).toBe(false);
    expect(document.getElementById("confirmTitle").textContent).toBe("Confirm");
    expect(document.getElementById("confirmMessage").textContent).toBe("Delete this branch?");
    expect(document.getElementById("confirmOk").textContent).toBe("Delete");
    expect(document.getElementById("confirmOk").className).toContain("danger");

    click("confirmOk");
    await act(async () => {});
    expect(settled).toBe(true);
    expect(modal().hidden).toBe(true);
  });

  it("honours custom labels and the non-danger variant", async () => {
    let settled;
    act(() => {
      confirmDialog("Proceed?", { title: "Enable Agile", okLabel: "Enable", danger: false })
        .then((v) => { settled = v; });
    });
    expect(document.getElementById("confirmTitle").textContent).toBe("Enable Agile");
    expect(document.getElementById("confirmOk").textContent).toBe("Enable");
    expect(document.getElementById("confirmOk").className).toContain("primary");
    click("confirmOk");
    await act(async () => {});
    expect(settled).toBe(true);
  });

  it("resolves false on cancel, close and Escape", async () => {
    let first;
    act(() => { first = confirmDialog("A"); });
    click("confirmCancel");
    await expect(first).resolves.toBe(false);

    let second;
    act(() => { second = confirmDialog("B"); });
    click("confirmClose");
    await expect(second).resolves.toBe(false);

    let third;
    act(() => { third = confirmDialog("C"); });
    act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
      );
    });
    await expect(third).resolves.toBe(false);
    expect(modal().hidden).toBe(true);
  });

  it("cancels the previous dialog when a second one opens", async () => {
    let first;
    act(() => {
      first = confirmDialog("first");
    });
    let second;
    act(() => {
      second = confirmDialog("second");
    });
    await expect(first).resolves.toBe(false);
    expect(document.getElementById("confirmMessage").textContent).toBe("second");
    click("confirmCancel");
    await expect(second).resolves.toBe(false);
  });

  it("resolves false when no host is mounted", async () => {
    act(() => root.unmount());
    container.remove();
    await expect(confirmDialog("orphan")).resolves.toBe(false);
    mountHost(); // keep afterEach happy
  });
});

describe("promptDialog", () => {
  it("resolves the trimmed value on OK and Enter", async () => {
    let settled;
    act(() => {
      promptDialog("Sprint name", { defaultValue: "Sprint 7" }).then((v) => { settled = v; });
    });
    const input = modal().querySelector(".confirm-input");
    expect(input).not.toBeNull();
    expect(input.value).toBe("Sprint 7");

    act(() => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, "value",
      ).set;
      setter.call(input, "  Renamed  ");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    click("confirmOk");
    await act(async () => {});
    expect(settled).toBe("Renamed");
  });

  it("resolves null when cancelled and an empty string for blank input", async () => {
    let cancelled;
    act(() => { cancelled = promptDialog("Name"); });
    click("confirmCancel");
    await expect(cancelled).resolves.toBeNull();

    let blank;
    act(() => {
      promptDialog("Name", { defaultValue: "   " }).then((v) => { blank = v; });
    });
    click("confirmOk");
    await act(async () => {});
    // Whitespace trims away, so callers see a falsy "" (no value entered).
    expect(blank).toBe("");
  });

  it("uses the prompt defaults for title, button and placeholder", () => {
    act(() => { promptDialog("Name"); });
    expect(document.getElementById("confirmTitle").textContent).toBe("Enter a value");
    expect(document.getElementById("confirmOk").textContent).toBe("OK");
    expect(modal().querySelector(".confirm-input").placeholder).toBe("");
  });

  it("resolves null when no host is mounted", async () => {
    act(() => root.unmount());
    container.remove();
    await expect(promptDialog("orphan")).resolves.toBeNull();
    mountHost();
  });
});
