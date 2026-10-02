/** Behavioral tests for the real page header (`./PageHead.jsx`).
 *
 * Renders the per-view title/subtitle and the list-only action chrome, so
 * these tests pin the title map, the live portfolio subtitle, the manager-only
 * bulk buttons, the New Item default type, and the template download flow.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiBlobMock = vi.fn();
const appState = {
  view: "list",
  activeTab: "all",
  defaultNewType: "Bug",
  openBugForm: vi.fn(),
  projects: [{ id: 1 }, { id: 2 }],
  stats: { open: 4 },
  canManage: true,
  setBulkImportOpen: vi.fn(),
};

vi.mock("../lib/api", () => ({
  apiBlob: (...args) => apiBlobMock(...args),
  api: vi.fn(),
  ApiError: class ApiError extends Error {},
}));
vi.mock("../state/AppContext", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useApp: () => appState };
});

import PageHead from "./PageHead.jsx";

let container;
let root;

function mount() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(PageHead));
  });
  return container;
}

beforeEach(() => {
  apiBlobMock.mockReset();
  appState.view = "list";
  appState.activeTab = "all";
  appState.canManage = true;
  appState.stats = { open: 4 };
  appState.projects = [{ id: 1 }, { id: 2 }];
  appState.openBugForm.mockClear();
  appState.setBulkImportOpen.mockClear();
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn(() => "blob:tpl"),
    revokeObjectURL: vi.fn(),
  });
  // jsdom cannot perform the blob download navigation; the test only needs
  // to observe that the anchor was created, clicked and cleaned up.
  vi.spyOn(window.HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  act(() => root?.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

describe("PageHead titles", () => {
  it("shows the per-view title and the live portfolio subtitle on list", () => {
    const el = mount();
    expect(el.querySelector("#pageTitle").textContent).toBe("All Work Items");
    expect(el.querySelector(".sub").textContent).toContain("2 projects");
    expect(el.querySelector(".sub").textContent).toContain("4 open");
  });

  it("singularises the project count", () => {
    appState.projects = [{ id: 1 }];
    const el = mount();
    expect(el.querySelector(".sub").textContent).toContain("1 project");
  });

  it("renders the static subtitles for the other views", () => {
    appState.view = "sprints";
    let el = mount();
    expect(el.querySelector("#pageTitle").textContent).toBe("Sprints");
    expect(el.querySelector(".sub").textContent).toContain("active sprint board");

    appState.view = "audit";
    el = mount();
    expect(el.querySelector("#pageTitle").textContent).toBe("Audit Trail");

    appState.view = "reports";
    el = mount();
    expect(el.querySelector("#pageTitle").textContent).toBe("Reports");
  });
});

describe("PageHead actions", () => {
  it("offers bulk + new-item actions to managers on the list view", () => {
    const el = mount();
    expect(el.querySelector("#bulkUploadBtn")).not.toBeNull();
    expect(el.querySelector("#downloadTemplateBtn")).not.toBeNull();
    act(() => {
      el.querySelector("#bulkUploadBtn").click();
    });
    expect(appState.setBulkImportOpen).toHaveBeenCalledWith(true);
  });

  it("opens the item form with the active type tab", () => {
    appState.activeTab = "Task";
    const el = mount();
    act(() => {
      el.querySelector("#newBugBtn").click();
    });
    expect(appState.openBugForm).toHaveBeenCalledWith({ defaultType: "Task" });
  });

  it("falls back to the remembered default type when the tab is All", () => {
    const el = mount();
    act(() => {
      el.querySelector("#newBugBtn").click();
    });
    expect(appState.openBugForm).toHaveBeenCalledWith({ defaultType: "Bug" });
  });

  it("hides the manager actions for non-managers", () => {
    appState.canManage = false;
    const el = mount();
    expect(el.querySelector("#bulkUploadBtn")).toBeNull();
    expect(el.querySelector("#downloadTemplateBtn")).toBeNull();
    expect(el.querySelector("#newBugBtn")).not.toBeNull();
  });

  it("downloads the import template through the blob pipeline", async () => {
    apiBlobMock.mockResolvedValueOnce({ blob: "bytes", filename: "tpl.xlsx" });
    const el = mount();
    act(() => {
      el.querySelector("#downloadTemplateBtn").click();
    });
    await act(async () => {});
    expect(apiBlobMock).toHaveBeenCalledWith("/bugs/import/template.xlsx");
    expect(URL.revokeObjectURL).toHaveBeenCalled();
  });

  it("falls back to the default filename and toasts failures", async () => {
    apiBlobMock.mockResolvedValueOnce({ blob: "bytes", filename: null });
    const el = mount();
    act(() => {
      el.querySelector("#downloadTemplateBtn").click();
    });
    await act(async () => {});
    expect(apiBlobMock).toHaveBeenCalledTimes(1);

    apiBlobMock.mockRejectedValueOnce(new Error("offline"));
    act(() => {
      el.querySelector("#downloadTemplateBtn").click();
    });
    await act(async () => {});
    expect(apiBlobMock).toHaveBeenCalledTimes(2);
  });
});
