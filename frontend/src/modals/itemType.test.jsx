/** Behavioral tests for the real item-type helpers in `../lib/itemTypes.js`
 * (the exact module BugModal, the board and the backlog import). */
import { describe, expect, it } from "vitest";
import {
  AGILE_ITEM_TYPES,
  ALL_ITEM_TYPES,
  CONVERTIBLE_ITEM_TYPES,
  STANDARD_TYPES,
  expectedVersionForSave,
  isEpic,
  isStandardType,
  isSubtask,
  itemTypeIcon,
  itemTypeLabel,
  itemTypeOptions,
  itemTypeSelectionForCreate,
  itemTypeSelectionForEdit,
  resolveCreateItemType,
} from "../lib/itemTypes.js";

describe("the Jira hierarchy", () => {
  it("has exactly Epic, four standard types and Sub-task", () => {
    expect(ALL_ITEM_TYPES).toEqual(["Epic", "Story", "Task", "Bug", "Requirement", "Sub-task"]);
    expect(STANDARD_TYPES).toEqual(["Story", "Task", "Bug", "Requirement"]);
  });

  it("never lets Task and Sub-task look alike", () => {
    expect(itemTypeLabel("Task")).toBe("Task");
    expect(itemTypeLabel("Sub-task")).toBe("Sub-task");
    expect(itemTypeIcon("Task")).not.toBe(itemTypeIcon("Sub-task"));
    const icons = ALL_ITEM_TYPES.map(itemTypeIcon);
    expect(new Set(icons).size).toBe(icons.length);
  });

  it("classifies types", () => {
    expect(isStandardType("Task")).toBe(true);
    expect(isStandardType("Sub-task")).toBe(false);
    expect(isStandardType(undefined)).toBe(true); // legacy rows default to Bug
    expect(isSubtask("Sub-task")).toBe(true);
    expect(isEpic("Epic")).toBe(true);
    expect(isEpic("Story")).toBe(false);
  });
});

describe("conversion type selection", () => {
  it("supports exactly the backend-accepted same-table types", () => {
    expect([...CONVERTIBLE_ITEM_TYPES].sort((a, b) => a.localeCompare(b))).toEqual(
      ["Bug", "Requirement", "Story", "Task"],
    );
  });

  it("offers every convertible type for a standard issue", () => {
    expect(itemTypeSelectionForEdit("Story").sort((a, b) => a.localeCompare(b)))
      .toEqual(["Bug", "Requirement", "Story", "Task"]);
    expect(itemTypeSelectionForEdit(undefined)).toContain("Bug");
  });

  it("keeps an Epic or Sub-task on its own type", () => {
    expect(itemTypeSelectionForEdit("Epic")).toEqual(["Epic"]);
    expect(itemTypeSelectionForEdit("Sub-task")).toEqual(["Sub-task"]);
  });

  it("labels options with their icon and Jira name", () => {
    const story = itemTypeOptions(["Story"])[0];
    expect(story.value).toBe("Story");
    expect(story.label).toBe(`${itemTypeIcon("Story")} Story`);
  });

  it("preserves the opened version for an edit and none for a create", () => {
    expect(expectedVersionForSave({ isEdit: true, version: 4 })).toBe(4);
    expect(expectedVersionForSave({ isEdit: false, version: 4 })).toBe(null);
    expect(expectedVersionForSave({ isEdit: true, version: null })).toBe(null);
  });
});

describe("create types vs the project's Agile surface", () => {
  it("offers the Jira types, Bug first, and never retired ones", () => {
    const values = itemTypeSelectionForCreate(["Bug", "Requirement", "Collection", "Feature"]);
    expect(values).toEqual(["Bug", "Story", "Task", "Requirement", "Epic", "Sub-task"]);
  });

  it("drops Agile-only types while Agile is disabled", () => {
    const values = itemTypeSelectionForCreate(["Bug", "Requirement"], { agileEnabled: false });
    for (const t of AGILE_ITEM_TYPES) {
      expect(values).not.toContain(t);
    }
    expect(values).toEqual(["Bug", "Task", "Requirement"]);
  });

  it("keeps every create option when Agile is on or not yet known", () => {
    const on = itemTypeSelectionForCreate(["Bug"], { agileEnabled: true });
    const loading = itemTypeSelectionForCreate(["Bug"]);
    for (const t of AGILE_ITEM_TYPES) {
      expect(on).toContain(t);
      expect(loading).toContain(t);
    }
  });

  it("falls back when a remembered default type cannot be created here", () => {
    const values = itemTypeSelectionForCreate(["Bug", "Requirement"], { agileEnabled: false });
    expect(resolveCreateItemType({ requested: "Story", values })).toBe("Bug");
    expect(resolveCreateItemType({ requested: "Epic", values })).toBe("Bug");
    expect(resolveCreateItemType({ requested: "Task", values })).toBe("Task");
    expect(resolveCreateItemType({ requested: "", values })).toBe("Bug");
  });

  it("never rewrites a valid type once Agile is enabled", () => {
    const values = itemTypeSelectionForCreate(["Bug"], { agileEnabled: true });
    expect(resolveCreateItemType({ requested: "Story", values })).toBe("Story");
  });
});
