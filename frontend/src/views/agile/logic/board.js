/** Pure helpers for the Board (active sprint): quick filters, swimlanes,
 * WIP state and drop targets. */

export const SWIMLANE_MODES = [
  { value: "none", label: "No swimlanes" },
  { value: "story", label: "Stories (parent issues)" },
  { value: "assignee", label: "Assignees" },
  { value: "epic", label: "Epics" },
  { value: "priority", label: "Priority" },
];

const PRIORITY_ORDER = ["Critical", "High", "Medium", "Low"];
const DAY_MS = 86_400_000;

/** Jira's built-in quick filters. */
export const BUILTIN_QUICK_FILTERS = [
  { key: "mine", name: "Only my issues" },
  { key: "recent", name: "Recently updated" },
];

function matchesCustomFilter(card, spec) {
  const has = (list) => Array.isArray(list) && list.length > 0;
  if (has(spec.assignee_ids)
      && !(card.assignees || []).some((a) => spec.assignee_ids.includes(a.id))) return false;
  if (has(spec.item_types) && !spec.item_types.includes(card.item_type)) return false;
  if (has(spec.priorities) && !spec.priorities.includes(card.priority)) return false;
  if (has(spec.epic_ids) && !spec.epic_ids.includes(card.epic_id)) return false;
  if (has(spec.label_ids)
      && !(card.labels || []).some((label) => spec.label_ids.includes(label.id))) return false;
  if (spec.flagged && !card.flagged) return false;
  if (spec.text) {
    const hay = `${card.display_id || ""} ${card.title || ""}`.toLowerCase();
    if (!hay.includes(String(spec.text).toLowerCase())) return false;
  }
  return true;
}

/**
 * Cards passing every active quick filter (Jira combines them with AND).
 * ``active`` holds built-in keys and custom filter ids.
 */
export function applyQuickFilters(cards, active, { userId, customFilters = [], now = Date.now() } = {}) {
  const selected = new Set(active || []);
  if (!selected.size) return cards;
  const custom = customFilters.filter((f) => selected.has(f.id));
  return cards.filter((card) => {
    if (selected.has("mine") && !(card.assignees || []).some((a) => a.id === userId)) return false;
    if (selected.has("recent")) {
      const updated = card.updated_at ? Date.parse(card.updated_at) : Number.NaN;
      if (!(now - updated <= DAY_MS)) return false;
    }
    return custom.every((f) => matchesCustomFilter(card, f.filter_json || {}));
  });
}

/**
 * Swimlanes for ``mode``. Each lane is { key, title, cards }. In "story" mode
 * every parent issue that has Sub-tasks on the board gets a lane holding its
 * Sub-tasks (the parent is shown in the lane header); all other issues go to
 * "Other issues", as in Jira.
 */
export function buildSwimlanes(cards, mode, { epics = [], currentUserId } = {}) {
  if (!mode || mode === "none") return [{ key: "all", title: "", cards }];
  if (mode === "story") {
    const parentIds = new Set(cards.filter((c) => c.parent_id != null).map((c) => c.parent_id));
    const lanes = new Map();
    const other = [];
    for (const card of cards) {
      if (card.parent_id != null) {
        if (!lanes.has(card.parent_id)) {
          lanes.set(card.parent_id, {
            key: `parent:${card.parent_id}`,
            title: `${card.parent_display_id || `#${card.parent_id}`} ${card.parent_title || ""}`.trim(),
            parentId: card.parent_id,
            cards: [],
          });
        }
        lanes.get(card.parent_id).cards.push(card);
      } else if (!parentIds.has(card.id)) {
        other.push(card);
      }
    }
    // A parent whose Sub-tasks are on the board is represented by its lane.
    const parents = new Map(cards.filter((c) => parentIds.has(c.id)).map((c) => [c.id, c]));
    const out = [...lanes.values()].map((lane) => ({ ...lane, parent: parents.get(lane.parentId) || null }));
    out.push({ key: "other", title: "Other issues", cards: other });
    return out;
  }
  if (mode === "assignee") {
    const lanes = new Map();
    const unassigned = [];
    for (const card of cards) {
      const people = card.assignees || [];
      if (!people.length) {
        unassigned.push(card);
        continue;
      }
      const first = people[0];
      if (!lanes.has(first.id)) lanes.set(first.id, { key: `user:${first.id}`, title: first.name, cards: [] });
      lanes.get(first.id).cards.push(card);
    }
    const out = [...lanes.values()].sort((a, b) => {
      if (a.key === `user:${currentUserId}`) return -1;
      if (b.key === `user:${currentUserId}`) return 1;
      return a.title.localeCompare(b.title);
    });
    out.push({ key: "unassigned", title: "Unassigned", cards: unassigned });
    return out;
  }
  if (mode === "epic") {
    const lanes = new Map(epics.map((e) => [e.id, { key: `epic:${e.id}`, title: e.title, color: e.color, cards: [] }]));
    const none = [];
    for (const card of cards) {
      if (card.epic_id != null && lanes.has(card.epic_id)) lanes.get(card.epic_id).cards.push(card);
      else none.push(card);
    }
    const out = [...lanes.values()].filter((lane) => lane.cards.length);
    out.push({ key: "no-epic", title: "Issues without epic", cards: none });
    return out;
  }
  if (mode === "priority") {
    return PRIORITY_ORDER.map((p) => ({
      key: `priority:${p}`, title: p, cards: cards.filter((c) => c.priority === p),
    })).filter((lane) => lane.cards.length);
  }
  return [{ key: "all", title: "", cards }];
}

/** Jira's column constraint colouring: over the max or under the min. */
export function wipState(column, count) {
  if (column.wip_limit != null && count > column.wip_limit) return "over";
  if (column.min_cards != null && count < column.min_cards) return "under";
  return "ok";
}

/**
 * Status to request when ``card`` is dropped on ``column``: its first status
 * in workflow order, or null when the card is already in that column.
 */
export function dropStatus(column, card) {
  const statuses = column?.statuses || [];
  if (!statuses.length) return undefined;
  if (statuses.includes(card.status)) return null;
  return statuses[0];
}

/** Which column a status belongs to. */
export function columnOfStatus(columns, status) {
  return (columns || []).find((col) => (col.statuses || []).includes(status)) || null;
}
