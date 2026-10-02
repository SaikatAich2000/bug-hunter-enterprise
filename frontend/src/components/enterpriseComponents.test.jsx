/** Project members, custom fields (project settings and item form) and saved views. */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.fn();
const toastMock = vi.fn();
const toastErrorMock = vi.fn();
const confirmMock = vi.fn();
const appState = {
  users: [
    { id: 1, name: "Ada", email: "ada@acme.test", is_active: true },
    { id: 2, name: "Bob", email: "bob@acme.test", is_active: true },
    { id: 3, name: "Gone", email: "gone@acme.test", is_active: false },
  ],
  loadProjects: vi.fn(),
  filters: { project_id: [4], status: ["New"], q: "" },
  setFilters: vi.fn(),
  canManage: true,
};

vi.mock("../lib/api", () => ({ api: (...a) => apiMock(...a) }));
vi.mock("../lib/toast", () => ({
  toast: (...a) => toastMock(...a),
  toastError: (...a) => toastErrorMock(...a),
}));
vi.mock("./ConfirmHost", () => ({ confirmDialog: (...a) => confirmMock(...a) }));
vi.mock("../state/AppContext", () => ({ useApp: () => appState }));

import SavedViews from "../shell/SavedViews";
import BugCustomFields, { missingRequiredFields, saveCustomValues } from "./BugCustomFields";
import ProjectCustomFields from "./ProjectCustomFields";
import ProjectMembers from "./ProjectMembers";

beforeEach(() => {
  apiMock.mockReset();
  toastMock.mockClear();
  toastErrorMock.mockClear();
  confirmMock.mockReset();
  appState.setFilters.mockReset();
  appState.loadProjects.mockReset();
});
afterEach(cleanup);

describe("ProjectMembers", () => {
  const members = [
    { user_id: 1, user_name: "Ada", user_email: "ada@acme.test", user_role: "user", project_role: "lead" },
  ];

  it("offers only active people who are not members yet, and adds one", async () => {
    apiMock.mockImplementation(async (path, opts) => (opts?.method === "POST" ? {} : members));
    render(<ProjectMembers projectId={4} editable />);
    await screen.findByText("Ada");
    const picker = screen.getByRole("combobox", { name: "Person to add" });
    expect([...picker.options].map((o) => o.textContent)).toEqual(["Add a person…", "Bob (bob@acme.test)"]);
    await userEvent.selectOptions(picker, "2");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Role of the new member" }), "lead");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/projects/4/members", { method: "POST", json: { user_id: 2, role: "lead" } }),
    );
    expect(appState.loadProjects).toHaveBeenCalled();
  });

  it("changes a role and removes a member after confirmation", async () => {
    apiMock.mockImplementation(async (path, opts) => (opts ? {} : members));
    confirmMock.mockResolvedValue(true);
    render(<ProjectMembers projectId={4} editable />);
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "Project role of Ada" }), "member");
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/projects/4/members/1", { method: "PUT", json: { role: "member" } }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/projects/4/members/1", { method: "DELETE" }));
  });

  it("is read-only for people who cannot manage the project", async () => {
    apiMock.mockResolvedValue(members);
    render(<ProjectMembers projectId={4} editable={false} />);
    await screen.findByText("Ada");
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
  });
});

describe("ProjectCustomFields", () => {
  it("adds a choice field with its options", async () => {
    apiMock.mockImplementation(async (path, opts) => (opts?.method === "POST" ? {} : []));
    render(<ProjectCustomFields projectId={4} editable />);
    await screen.findByText("No custom fields yet.");
    await userEvent.type(screen.getByRole("textbox", { name: "Field name" }), "Tier");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Field type" }), "select");
    const add = screen.getByRole("button", { name: "Add field" });
    expect(add.disabled).toBe(true);
    await userEvent.type(screen.getByRole("textbox", { name: "Choices, comma-separated" }), "Gold, Silver");
    await userEvent.click(screen.getByRole("checkbox", { name: "Required" }));
    await userEvent.click(add);
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/projects/4/custom-fields", {
        method: "POST",
        json: { name: "Tier", field_type: "select", options: ["Gold", "Silver"], is_required: true, position: 0 },
      }),
    );
  });

  it("lists fields and hides the controls from non-managers", async () => {
    apiMock.mockResolvedValue([
      { id: 9, project_id: 4, name: "Cost", field_type: "number", options: [], is_required: true, position: 0 },
    ]);
    render(<ProjectCustomFields projectId={4} editable={false} />);
    expect(await screen.findByText("Cost")).toBeTruthy();
    expect(screen.getByText(/required/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add field" })).toBeNull();
  });
});

describe("BugCustomFields", () => {
  const defs = [
    { id: 1, name: "Customer", field_type: "text", options: [], is_required: true },
    { id: 2, name: "Tier", field_type: "select", options: ["Gold", "Silver"], is_required: false },
  ];

  function mountForm(bugId = null) {
    const handleRef = { current: null };
    apiMock.mockImplementation(async (path) => {
      if (path.endsWith("/custom-fields")) return defs;
      return bugId ? [{ field_id: 2, value: "Gold" }] : [];
    });
    render(<BugCustomFields projectId={4} bugId={bugId} readOnly={false} handleRef={handleRef} />);
    return handleRef;
  }

  it("renders nothing for a project without fields", async () => {
    apiMock.mockResolvedValue([]);
    const { container } = render(
      <BugCustomFields projectId={4} bugId={null} readOnly={false} handleRef={{ current: null }} />,
    );
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    expect(container.querySelector("#bugCustomFields")).toBeNull();
  });

  it("reports missing required fields and saves the entered values", async () => {
    const handleRef = mountForm();
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: /Tier/ }), "Silver");
    await waitFor(() => expect(missingRequiredFields(handleRef.current)).toEqual(["Customer"]));
    await userEvent.type(screen.getByRole("textbox", { name: /Customer/ }), "Initech");
    await waitFor(() => expect(missingRequiredFields(handleRef.current)).toEqual([]));

    apiMock.mockClear();
    apiMock.mockResolvedValue([]);
    await saveCustomValues(42, handleRef.current);
    expect(apiMock).toHaveBeenCalledWith("/bugs/42/custom-values", {
      method: "PUT",
      json: [{ field_id: 1, value: "Initech" }, { field_id: 2, value: "Silver" }],
    });
  });

  it("loads existing values and skips saving when nothing was touched", async () => {
    const handleRef = mountForm(42);
    expect((await screen.findByRole("combobox", { name: /Tier/ })).value).toBe("Gold");
    await waitFor(() => expect(handleRef.current.values[2]).toBe("Gold"));
    apiMock.mockClear();
    await saveCustomValues(42, handleRef.current);
    expect(apiMock).not.toHaveBeenCalled();
  });

  it("does nothing without fields or an item id", async () => {
    await saveCustomValues(42, null);
    await saveCustomValues(null, { fields: [{ id: 1 }], values: {}, dirty: true, existing: false });
    await saveCustomValues(42, { fields: [], values: {}, dirty: true, existing: true });
    expect(apiMock).not.toHaveBeenCalled();
    expect(missingRequiredFields(null)).toEqual([]);
  });
});

describe("SavedViews", () => {
  const views = [
    { id: 1, name: "Open bugs", filters: { status: ["New"] }, shared_with_org: false, is_mine: true },
    { id: 2, name: "Team triage", filters: { priority: ["High"] }, shared_with_org: true, is_mine: false },
  ];

  it("applies a view's filters on top of the current ones", async () => {
    apiMock.mockResolvedValue(views);
    render(<SavedViews />);
    await screen.findByRole("option", { name: "Open bugs" });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Saved views" }), "1");
    expect(appState.setFilters).toHaveBeenCalledTimes(1);
    const updater = appState.setFilters.mock.calls[0][0];
    expect(updater({ project_id: [], status: [], q: "x" })).toEqual({ project_id: [], status: ["New"], q: "x" });
  });

  it("saves the current filters, optionally shared, and offers delete only for own views", async () => {
    apiMock.mockImplementation(async (path, opts) =>
      opts?.method === "POST" ? { id: 3, name: "Mine" } : views,
    );
    render(<SavedViews />);
    await screen.findByRole("option", { name: "Open bugs" });
    await userEvent.click(screen.getByRole("button", { name: "Save view" }));
    await userEvent.type(screen.getByRole("textbox", { name: "View name" }), "Mine");
    await userEvent.click(screen.getByRole("checkbox", { name: "Share with everyone" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith("/saved-views", {
        method: "POST",
        json: { name: "Mine", filters: appState.filters, shared_with_org: true },
      }),
    );

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Saved views" }), "2");
    expect(screen.queryByRole("button", { name: "Delete view" })).toBeNull();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Saved views" }), "1");
    expect(screen.getByRole("button", { name: "Delete view" })).toBeTruthy();
  });

  it("does not offer sharing to plain users", async () => {
    appState.canManage = false;
    apiMock.mockResolvedValue([]);
    render(<SavedViews />);
    await userEvent.click(screen.getByRole("button", { name: "Save view" }));
    expect(screen.queryByRole("checkbox", { name: "Share with everyone" })).toBeNull();
    appState.canManage = true;
  });
});
