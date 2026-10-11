/** The active sprint's "Delete sprint" menu item asks with the running-sprint wording, then cancels it. */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./agileApi", () => ({
  agileApi: { planning: vi.fn(), deleteSprint: vi.fn(() => Promise.resolve({})) },
  notifyAgileChanged: vi.fn(),
  onAgileChanged: vi.fn(() => () => {}),
}));
vi.mock("../../lib/toast", () => ({ toast: vi.fn(), toastError: vi.fn() }));
vi.mock("../../components/ConfirmHost", () => ({ confirmDialog: vi.fn(() => Promise.resolve(true)) }));
vi.mock("../../state/AppContext", () => ({
  useApp: () => ({ openBugDetail: vi.fn(), openBugForm: vi.fn(), users: [] }),
}));

import { agileApi } from "./agileApi";
import { confirmDialog } from "../../components/ConfirmHost";
import BacklogPanel from "./BacklogPanel.jsx";

const issue = { id: 1, display_id: "P-1", title: "One", item_type: "Task", status_category: "todo" };
const planning = {
  epics: [], backlog: [],
  sprints: [{ id: 5, name: "Sprint 5", state: "active", version: 3, issues: [issue] }],
};
let container;
let root;

beforeEach(async () => {
  vi.clearAllMocks();
  agileApi.planning.mockResolvedValue(planning);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(React.createElement(BacklogPanel, {
      canManage: true, board: { id: 2, project_id: 1, estimation_mode: "points" }, onOpenTab: vi.fn(),
    }));
  });
});

afterEach(() => {
  act(() => root.unmount());
  document.body.innerHTML = "";
});

describe("delete active sprint", () => {
  it("offers Delete sprint on a running sprint, confirms with the running wording and cancels it", async () => {
    const menu = document.querySelector('button[aria-label="Sprint actions for Sprint 5"]');
    expect(menu).toBeTruthy();
    act(() => menu.click());
    const item = [...document.querySelectorAll('[role="menuitem"]')].find((b) => b.textContent === "Delete sprint");
    expect(item).toBeTruthy();
    await act(async () => item.click());
    expect(confirmDialog.mock.calls[0][0]).toContain("the running sprint Sprint 5");
    expect(agileApi.deleteSprint).toHaveBeenCalledWith(
      5, expect.objectContaining({ version: 3, default_destination: "backlog" }),
    );
  });
});
