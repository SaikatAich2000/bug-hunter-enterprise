import { describe, expect, it } from "vitest";
import {
  categoryTotals, estimateOf, formatNumber, isEstimated, statisticLabel, statisticUnit,
} from "./estimates";
import {
  BACKLOG_KEY, containerSprintId, daysRemaining, findContainer,
  matchesIssueFilter, moveLocally, planDrop, sprintKey,
} from "./backlog";
import {
  applyQuickFilters, buildSwimlanes, columnOfStatus, dropStatus, wipState,
} from "./board";

const issue = (id, extra = {}) => ({
  id, title: `Issue ${id}`, display_id: `USRSTR-${id}`, item_type: "Story",
  status: "New", status_category: "todo", priority: "Medium", assignees: [], labels: [], ...extra,
});

describe("estimates", () => {
  it("reads the board's statistic", () => {
    const story = issue(1, { story_points: 5, original_estimate_minutes: 150 });
    expect(estimateOf(story, "story_points")).toBe(5);
    expect(estimateOf(story, "time")).toBe(2.5);
    expect(estimateOf(story, "item_count")).toBe(1);
    expect(estimateOf(issue(2), "story_points")).toBe(0);
    expect(estimateOf(null, "story_points")).toBe(0);
  });

  it("knows what is estimated", () => {
    expect(isEstimated(issue(1, { story_points: 0 }), "story_points")).toBe(true);
    expect(isEstimated(issue(1), "story_points")).toBe(false);
    expect(isEstimated(issue(1), "item_count")).toBe(true);
    expect(isEstimated(issue(1, { original_estimate_minutes: 30 }), "time")).toBe(true);
  });

  it("labels the statistic", () => {
    expect(statisticLabel("time")).toBe("Original time estimate (hours)");
    expect(statisticLabel("nonsense")).toBe("Story points");
    expect(statisticUnit("story_points")).toBe("pts");
    expect(statisticUnit("time")).toBe("h");
    expect(formatNumber(2.5)).toBe("2.5");
    expect(formatNumber(3.0)).toBe("3");
    expect(formatNumber(null)).toBe("0");
  });

  it("totals by status category, testing counting as in progress", () => {
    const totals = categoryTotals([
      issue(1, { story_points: 3 }),
      issue(2, { story_points: 5, status_category: "in_progress" }),
      issue(3, { story_points: 2, status_category: "testing" }),
      issue(4, { story_points: 8, status_category: "done" }),
      issue(5),
    ], "story_points");
    expect(totals).toEqual({ count: 5, total: 18, todo: 3, inProgress: 7, done: 8, unestimated: 1 });
  });
});

describe("backlog drag and drop", () => {
  const list = [issue(1), issue(2), issue(3), issue(4)];

  // A sprint (5, 6) above the backlog (1-4); dnd-kit has already moved the
  // dragged issue into the list it is over when the drop happens.
  const start = { [sprintKey(9)]: [issue(5), issue(6)], [BACKLOG_KEY]: list };
  const drop = (containers, activeId, overId, extra = {}) =>
    planDrop({ startContainers: start, containers, activeId, overId, selectedIds: [], ...extra });

  it("reorders within a list, sending the issue above as the neighbour", () => {
    // Drag 4 onto 2 in the backlog: 1, 4, 2, 3.
    const plan = drop(start, 4, 2);
    expect(plan).toEqual({ itemIds: [4], targetKey: BACKLOG_KEY, neighbours: { before_id: 1, after_id: null }, index: 1 });
    // Drag 3 to the very top: no issue above, so the one below.
    expect(drop(start, 3, 1).neighbours).toEqual({ before_id: null, after_id: 1 });
  });

  it("does nothing when an issue is dropped where it was", () => {
    expect(drop(start, 2, 2)).toBeNull();
    expect(drop(start, 2, null)).toBeNull();
  });

  it("plans an issue into a sprint between its issues", () => {
    const live = { [sprintKey(9)]: [issue(5), issue(1), issue(6)], [BACKLOG_KEY]: [issue(2), issue(3), issue(4)] };
    expect(drop(live, 1, 1)).toEqual({
      itemIds: [1], targetKey: sprintKey(9), neighbours: { before_id: 5, after_id: null }, index: 1,
    });
  });

  it("plans into an empty list without neighbours", () => {
    const empty = { [sprintKey(9)]: [], [BACKLOG_KEY]: list };
    const live = { [sprintKey(9)]: [issue(3)], [BACKLOG_KEY]: [issue(1), issue(2), issue(4)] };
    expect(planDrop({ startContainers: empty, containers: live, activeId: 3, overId: sprintKey(9), selectedIds: [] }))
      .toEqual({ itemIds: [3], targetKey: sprintKey(9), neighbours: { before_id: null, after_id: null }, index: 0 });
  });

  it("moves the shown selection together, in board order, positioned among the rest", () => {
    // 2 and 4 are selected; 4 is dragged to the top of the backlog.
    const plan = drop(start, 4, 1, { selectedIds: [2, 4] });
    expect(plan.itemIds).toEqual([2, 4]);
    expect(plan.neighbours).toEqual({ before_id: null, after_id: 1 });
    expect(plan.index).toBe(0);
    expect(moveLocally(start, plan.itemIds, plan.targetKey, plan.index)[BACKLOG_KEY].map((i) => i.id)).toEqual([2, 4, 1, 3]);
  });

  it("never moves a selected issue the filter hides, and ignores hidden neighbours", () => {
    const isVisible = (i) => i.id !== 2;
    // 2 (hidden) and 4 are selected; 4 is dropped onto 3.
    const plan = drop(start, 4, 3, { selectedIds: [2, 4], isVisible });
    expect(plan.itemIds).toEqual([4]);
    // Shown order is 1, 4, 3: the neighbour is 1, not the hidden 2.
    expect(plan.neighbours).toEqual({ before_id: 1, after_id: null });
  });

  it("ignores a selection the dragged issue is not part of", () => {
    expect(drop(start, 4, 1, { selectedIds: [2, 3] }).itemIds).toEqual([4]);
  });

  it("moves issues between containers optimistically, keeping their order", () => {
    const containers = { [sprintKey(7)]: [issue(1), issue(2)], [BACKLOG_KEY]: [issue(3), issue(4)] };
    const next = moveLocally(containers, [4, 1], sprintKey(7), 1);
    expect(next[sprintKey(7)].map((i) => i.id)).toEqual([2, 4, 1]);
    expect(next[sprintKey(7)].filter((i) => [4, 1].includes(i.id)).every((i) => i.sprint_id === 7)).toBe(true);
    expect(next[BACKLOG_KEY].map((i) => i.id)).toEqual([3]);
    const back = moveLocally(next, [2], BACKLOG_KEY, 0);
    expect(back[BACKLOG_KEY][0]).toMatchObject({ id: 2, sprint_id: null });
    expect(findContainer(back, 2)).toBe(BACKLOG_KEY);
    expect(findContainer(back, 999)).toBe(null);
  });

  it("maps container keys to sprint ids", () => {
    expect(containerSprintId(sprintKey(12))).toBe(12);
    expect(containerSprintId(BACKLOG_KEY)).toBe(null);
    expect(containerSprintId(undefined)).toBe(null);
  });

  it("filters by text, epic, assignee and type", () => {
    const a = issue(1, { title: "Login fails", epic_id: 9, assignees: [{ id: 3, name: "Ann" }] });
    const b = issue(2, { item_type: "Bug" });
    expect(matchesIssueFilter(a, { text: "login" })).toBe(true);
    expect(matchesIssueFilter(a, { text: "USRSTR-1" })).toBe(true);
    expect(matchesIssueFilter(b, { text: "login" })).toBe(false);
    expect(matchesIssueFilter(a, { epicId: "9" })).toBe(true);
    expect(matchesIssueFilter(b, { epicId: "none" })).toBe(true);
    expect(matchesIssueFilter(a, { epicId: "none" })).toBe(false);
    expect(matchesIssueFilter(a, { assigneeId: "3" })).toBe(true);
    expect(matchesIssueFilter(b, { assigneeId: "unassigned" })).toBe(true);
    expect(matchesIssueFilter(a, { assigneeId: "unassigned" })).toBe(false);
    expect(matchesIssueFilter(b, { type: "Bug" })).toBe(true);
    expect(matchesIssueFilter(a, { type: "Bug" })).toBe(false);
  });

  it("counts remaining sprint days inclusively", () => {
    expect(daysRemaining("2026-03-13", "2026-03-13")).toBe(1);
    expect(daysRemaining("2026-03-13", "2026-03-10")).toBe(4);
    expect(daysRemaining("2026-03-13", "2026-03-20")).toBe(0);
    expect(daysRemaining(null, "2026-03-20")).toBe(null);
  });
});

describe("board", () => {
  const ann = { id: 1, name: "Ann" };
  const bob = { id: 2, name: "Bob" };
  const now = Date.parse("2026-03-10T12:00:00Z");
  const cards = [
    issue(1, { assignees: [ann], updated_at: "2026-03-10T08:00:00Z", epic_id: 50, priority: "High" }),
    issue(2, { assignees: [bob], updated_at: "2026-03-01T08:00:00Z", labels: [{ id: 9, name: "ux" }] }),
    issue(3, { item_type: "Sub-task", parent_id: 1, parent_display_id: "USRSTR-1", parent_title: "Issue 1", assignees: [ann] }),
    issue(4, { item_type: "Bug", flagged: true }),
  ];

  it("applies built-in and custom quick filters together (AND)", () => {
    expect(applyQuickFilters(cards, [], { userId: 1 })).toBe(cards);
    expect(applyQuickFilters(cards, ["mine"], { userId: 1 }).map((c) => c.id)).toEqual([1, 3]);
    expect(applyQuickFilters(cards, ["recent"], { now }).map((c) => c.id)).toEqual([1]);
    const custom = [{ id: 7, filter_json: { item_types: ["Bug"] } }, { id: 8, filter_json: { label_ids: [9] } }];
    expect(applyQuickFilters(cards, [7], { customFilters: custom }).map((c) => c.id)).toEqual([4]);
    expect(applyQuickFilters(cards, [8], { customFilters: custom }).map((c) => c.id)).toEqual([2]);
    expect(applyQuickFilters(cards, ["mine", 7], { userId: 1, customFilters: custom })).toEqual([]);
  });

  it("groups Sub-tasks under their parent in the stories swimlane", () => {
    const lanes = buildSwimlanes(cards, "story");
    expect(lanes.map((l) => [l.key, l.cards.map((c) => c.id)])).toEqual([
      ["parent:1", [3]], ["other", [2, 4]],
    ]);
    expect(lanes[0].parent.id).toBe(1);
    expect(lanes[0].title).toBe("USRSTR-1 Issue 1");
  });

  it("groups by assignee with the viewer first and Unassigned last", () => {
    const lanes = buildSwimlanes(cards, "assignee", { currentUserId: 2 });
    expect(lanes.map((l) => l.title)).toEqual(["Bob", "Ann", "Unassigned"]);
    expect(lanes[2].cards.map((c) => c.id)).toEqual([4]);
  });

  it("groups by epic and priority", () => {
    const lanes = buildSwimlanes(cards, "epic", { epics: [{ id: 50, title: "Checkout" }, { id: 51, title: "Empty" }] });
    expect(lanes.map((l) => [l.title, l.cards.length])).toEqual([["Checkout", 1], ["Issues without epic", 3]]);
    const byPriority = buildSwimlanes(cards, "priority");
    expect(byPriority.map((l) => l.title)).toEqual(["High", "Medium"]);
    expect(buildSwimlanes(cards, "none")[0].cards).toBe(cards);
  });

  it("flags WIP over the max and under the min", () => {
    expect(wipState({ wip_limit: 2 }, 3)).toBe("over");
    expect(wipState({ wip_limit: 2 }, 2)).toBe("ok");
    expect(wipState({ min_cards: 1 }, 0)).toBe("under");
    expect(wipState({}, 50)).toBe("ok");
  });

  it("drops onto a column's first status, and not at all onto its own column", () => {
    const col = { statuses: ["In Progress", "In Review"] };
    expect(dropStatus(col, { status: "New" })).toBe("In Progress");
    expect(dropStatus(col, { status: "In Review" })).toBe(null);
    expect(dropStatus({ statuses: [] }, { status: "New" })).toBe(undefined);
    expect(columnOfStatus([{ id: 1, statuses: ["New"] }, col], "In Review")).toBe(col);
    expect(columnOfStatus([col], "Done")).toBe(null);
  });
});
