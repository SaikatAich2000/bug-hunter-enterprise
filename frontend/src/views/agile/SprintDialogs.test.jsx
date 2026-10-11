/** Complete-sprint dialog: per-issue destinations reach the API; delete prompt wording. */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./agileApi", () => ({
  agileApi: { completeSprint: vi.fn(() => Promise.resolve({})) },
  notifyAgileChanged: vi.fn(),
}));
vi.mock("../../lib/toast", () => ({ toast: vi.fn(), toastError: vi.fn() }));

import { agileApi } from "./agileApi";
import { CompleteSprintDialog, deleteSprintPrompt, destinationFields } from "./SprintDialogs.jsx";

const sprint = { id: 5, name: "Sprint 5", version: 3 };
const issues = [
  { id: 1, display_id: "P-1", title: "Done one", status_category: "done", subtask_count: 0, subtasks_done: 0 },
  { id: 2, display_id: "P-2", title: "Open two", status_category: "in_progress" },
  { id: 3, display_id: "P-3", title: "Open three", status_category: "todo" },
];
const future = [{ id: 9, name: "Sprint 6" }];

let container;
let root;

beforeEach(() => {
  vi.clearAllMocks();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(CompleteSprintDialog,
      { open: true, sprint, issues, futureSprints: future, onClose: vi.fn() }));
  });
});

afterEach(() => {
  act(() => root.unmount());
  document.body.innerHTML = "";
});

function pick(label, value) {
  const sel = document.querySelector(`select[aria-label="${label}"]`);
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set;
    setter.call(sel, value);
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

async function submit() {
  await act(async () => {
    document.querySelector("#completeSprintDialog form").dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true }));
  });
}

describe("CompleteSprintDialog per-issue destinations", () => {
  it("lists a destination picker for each open issue only", () => {
    expect(document.querySelector('select[aria-label="Destination for Open two"]')).not.toBeNull();
    expect(document.querySelector('select[aria-label="Destination for Open three"]')).not.toBeNull();
    expect(document.querySelector('select[aria-label="Destination for Done one"]')).toBeNull();
  });

  it("sends no dispositions when nothing is overridden", async () => {
    await submit();
    const body = agileApi.completeSprint.mock.calls[0][1];
    expect(body.default_destination).toBe("backlog");
    expect(body.dispositions).toBeUndefined();
  });

  it("sends an override for just the issue that was changed", async () => {
    pick("Destination for Open two", "sprint:9");
    await submit();
    const [id, body] = agileApi.completeSprint.mock.calls[0];
    expect(id).toBe(5);
    expect(body.default_destination).toBe("backlog");
    expect(body.dispositions).toEqual([{ item_id: 2, destination: "sprint", target_sprint_id: 9 }]);
  });
});

describe("helpers", () => {
  it("maps picker values to API fields", () => {
    expect(destinationFields("backlog")).toEqual({ default_destination: "backlog" });
    expect(destinationFields("new")).toEqual({ default_destination: "new_sprint" });
    expect(destinationFields("sprint:4")).toEqual({ default_destination: "sprint", default_target_sprint_id: 4 });
  });

  it("warns that deleting an active sprint ends it", () => {
    expect(deleteSprintPrompt({ name: "S", state: "active" }, 2)).toMatch(/running sprint S.*ends it without completing/);
    expect(deleteSprintPrompt({ name: "S", state: "future" }, 0)).toBe("Delete S?");
  });
});
