/** Work-item types, modelled on Jira Software:
 *
 *   Epic  →  Story · Task · Bug · Requirement (standard issues)  →  Sub-task
 *
 * Task (a standard issue) and Sub-task (a piece of one standard issue) are
 * different types, with different labels and icons everywhere in the UI.
 */

const EPIC = "Epic";
const SUBTASK = "Sub-task";
export const STANDARD_TYPES = ["Story", "Task", "Bug", "Requirement"];
export const ALL_ITEM_TYPES = [EPIC, ...STANDARD_TYPES, SUBTASK];

/** Same-table conversions the backend accepts through PUT /api/bugs/{id}. */
export const CONVERTIBLE_ITEM_TYPES = ["Bug", "Requirement", "Task", "Story"];

/**
 * Types whose create flow is POST /api/agile/work-items, which needs the
 * project's Agile surface to be enabled.
 */
export const AGILE_ITEM_TYPES = [EPIC, "Story", SUBTASK];

const ICONS = {
  Epic: "⚡",
  Story: "📗",
  Task: "☑️",
  Bug: "🐞",
  Requirement: "📐",
  "Sub-task": "🔹",
};

/** Display name of a stored type (types are named as in Jira). */
export function itemTypeLabel(type) {
  return type || "Bug";
}

/** Icon for a type; each type has its own, Task and Sub-task included. */
export function itemTypeIcon(type) {
  return ICONS[type] ?? "📝";
}

export function isStandardType(type) {
  return STANDARD_TYPES.includes(type || "Bug");
}

export function isSubtask(type) {
  return type === SUBTASK;
}

export function isEpic(type) {
  return type === EPIC;
}

/**
 * Types selectable in edit mode: the convertible set plus the item's own type
 * (an Epic or Sub-task stays visible but cannot be converted from here).
 */
export function itemTypeSelectionForEdit(itemType) {
  const own = itemType || "Bug";
  if (!CONVERTIBLE_ITEM_TYPES.includes(own)) return [own];
  return [...CONVERTIBLE_ITEM_TYPES];
}

/**
 * Types selectable when creating a new item.
 *
 * `agileEnabled === false` means the project's Agile surface is known to be off,
 * so Agile-only types are filtered out (they could only fail). `undefined` means
 * the project's settings have not loaded yet, so the full list is offered.
 * Unknown types (e.g. retired "Collection"/"Feature") are never offered.
 */
export function itemTypeSelectionForCreate(metaItemTypes, { agileEnabled } = {}) {
  const known = new Set([...(metaItemTypes || []), ...CONVERTIBLE_ITEM_TYPES]);
  // Bug first: every role can create one, so it is the safe default.
  const ordered = ["Bug", "Story", "Task", "Requirement", EPIC, SUBTASK].filter(
    (t) => known.has(t) || AGILE_ITEM_TYPES.includes(t),
  );
  if (agileEnabled === false) {
    return ordered.filter((value) => !AGILE_ITEM_TYPES.includes(value));
  }
  return ordered;
}

/**
 * Item type to preselect for a new item. A remembered default (localStorage) or
 * the active list-view tab can name a type this project cannot create, so fall
 * back to the first creatable type instead of submitting a request that fails.
 */
export function resolveCreateItemType({ requested, values }) {
  if (requested && values.includes(requested)) return requested;
  return values[0] ?? requested ?? "";
}

/** `{value,label}` options for the type selector, each with its icon. */
export function itemTypeOptions(values) {
  return values.map((value) => ({ value, label: `${itemTypeIcon(value)} ${itemTypeLabel(value)}` }));
}

/** Optimistic-concurrency token sent on an edit (never on create). */
export function expectedVersionForSave({ isEdit, version }) {
  return isEdit && version != null ? version : null;
}
