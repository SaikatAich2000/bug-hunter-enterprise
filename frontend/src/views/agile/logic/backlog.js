/** Pure helpers for the Backlog view (sprints + backlog, drag-and-drop). */

export const BACKLOG_KEY = "backlog";

/** Container key of a sprint in the backlog view. */
export function sprintKey(sprintId) {
  return `sprint:${sprintId}`;
}

/** Sprint id of a container key, or null for the backlog. */
export function containerSprintId(key) {
  if (!key || key === BACKLOG_KEY) return null;
  const id = Number(String(key).replace("sprint:", ""));
  return Number.isFinite(id) ? id : null;
}

/**
 * Optimistic local version of a move: take ``itemIds`` out of wherever they
 * are and insert them, in their current order, into ``targetKey`` at
 * ``index`` (counted without the moving issues). Returns new containers.
 */
export function moveLocally(containers, itemIds, targetKey, index) {
  const ids = new Set(itemIds);
  const moved = [];
  const next = {};
  for (const [key, issues] of Object.entries(containers)) {
    next[key] = [];
    for (const issue of issues) {
      if (ids.has(issue.id)) moved.push(issue);
      else next[key].push(issue);
    }
  }
  const byId = new Map(moved.map((issue) => [issue.id, issue]));
  const ordered = itemIds.map((id) => byId.get(id)).filter(Boolean);
  const target = next[targetKey] ?? [];
  const at = Math.max(0, Math.min(index, target.length));
  const sprintId = containerSprintId(targetKey);
  next[targetKey] = [
    ...target.slice(0, at),
    ...ordered.map((issue) => ({ ...issue, sprint_id: sprintId })),
    ...target.slice(at),
  ];
  return next;
}

/** Which container holds ``issueId``, or null. */
export function findContainer(containers, issueId) {
  for (const [key, issues] of Object.entries(containers)) {
    if (issues.some((issue) => issue.id === issueId)) return key;
  }
  return null;
}

/**
 * Backlog search/filter bar: free text (key or summary), epic ("none" for
 * issues without an epic), assignee ("unassigned" allowed) and type.
 */
export function matchesIssueFilter(issue, filter = {}) {
  const text = (filter.text || "").trim().toLowerCase();
  if (text) {
    const hay = `${issue.display_id || ""} ${issue.title || ""}`.toLowerCase();
    if (!hay.includes(text)) return false;
  }
  if (filter.epicId === "none") {
    if (issue.epic_id != null) return false;
  } else if (filter.epicId != null && filter.epicId !== "") {
    if (issue.epic_id !== Number(filter.epicId)) return false;
  }
  if (filter.assigneeId === "unassigned") {
    if ((issue.assignees || []).length) return false;
  } else if (filter.assigneeId != null && filter.assigneeId !== "") {
    if (!(issue.assignees || []).some((a) => a.id === Number(filter.assigneeId))) return false;
  }
  if (filter.type && issue.item_type !== filter.type) return false;
  return true;
}

/** Remaining days of a sprint (end date inclusive), never negative; null without an end date. */
export function daysRemaining(endDate, today) {
  if (!endDate) return null;
  const end = new Date(`${endDate}T00:00:00`);
  const now = new Date(`${today}T00:00:00`);
  if (Number.isNaN(end.getTime()) || Number.isNaN(now.getTime())) return null;
  return Math.max(0, Math.round((end - now) / 86_400_000) + 1);
}

/**
 * What a finished backlog drag means, or null when nothing changed.
 *
 * ``startContainers`` is the state when the drag began; ``containers`` is the
 * live state with the dragged issue already where it was dropped (dnd-kit's
 * onDragOver moves it between lists, the final reorder is applied here).
 * ``selectedIds`` move together with the dragged issue when it is one of
 * them, but only those the filter shows (``isVisible``): a hidden issue is
 * never moved unseen. Neighbours are taken among the visible issues, so a
 * filtered-out issue never makes the drop position ambiguous.
 *
 * Returns ``{ itemIds, targetKey, neighbours, index }``: the ids in board
 * order, the list, the API's before_id/after_id, and the insert position for
 * the optimistic local move (counted without the moving issues).
 */
export function planDrop({ startContainers, containers, activeId, overId, selectedIds, isVisible = () => true }) {
  const origin = findContainer(startContainers, activeId);
  const targetKey = findContainer(containers, activeId);
  if (!origin || !targetKey) return null;
  let list = containers[targetKey] || [];
  const from = list.findIndex((i) => i.id === activeId);
  const to = list.findIndex((i) => i.id === overId);
  if (to >= 0 && from !== to) {
    list = [...list];
    const [moved] = list.splice(from, 1);
    list.splice(to, 0, moved);
  }
  const selected = new Set(selectedIds || []);
  let itemIds = [activeId];
  if (selected.has(activeId) && selected.size > 1) {
    const boardOrder = Object.values(startContainers).flat();
    itemIds = boardOrder
      .filter((i) => selected.has(i.id) && (i.id === activeId || isVisible(i)))
      .map((i) => i.id);
  }
  const moving = new Set(itemIds);
  const shown = list.filter((i) => i.id === activeId || (!moving.has(i.id) && isVisible(i)));
  const at = shown.findIndex((i) => i.id === activeId);
  const above = at > 0 ? shown[at - 1] : null;
  const below = at >= 0 && at < shown.length - 1 ? shown[at + 1] : null;
  if (itemIds.length === 1 && origin === targetKey) {
    const before = (startContainers[targetKey] || []).map((i) => i.id).join(",");
    if (before === list.map((i) => i.id).join(",")) return null;
  }
  const rest = list.filter((i) => !moving.has(i.id));
  let index = rest.length;
  if (above) index = rest.findIndex((i) => i.id === above.id) + 1;
  else if (below) index = rest.findIndex((i) => i.id === below.id);
  else if (targetKey === BACKLOG_KEY) index = 0;
  return {
    itemIds,
    targetKey,
    neighbours: above ? { before_id: above.id, after_id: null } : { before_id: null, after_id: below?.id ?? null },
    index,
  };
}
