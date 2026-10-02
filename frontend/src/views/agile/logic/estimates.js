/** The board's estimation statistic, as Jira applies it: story points,
 * original time estimate (hours) or issue count. Unestimated issues count 0. */

export const STATISTIC_LABELS = {
  story_points: "Story points",
  time: "Original time estimate (hours)",
  item_count: "Issue count",
};

export function statisticLabel(mode) {
  return STATISTIC_LABELS[mode] ?? STATISTIC_LABELS.story_points;
}

/** Short unit shown next to a number: "pts", "h" or "issues". */
export function statisticUnit(mode) {
  if (mode === "time") return "h";
  if (mode === "item_count") return "issues";
  return "pts";
}

export function estimateOf(issue, mode) {
  if (!issue) return 0;
  if (mode === "item_count") return 1;
  if (mode === "time") {
    const minutes = Number(issue.original_estimate_minutes);
    return minutes > 0 ? Math.round((minutes / 60) * 100) / 100 : 0;
  }
  const points = Number(issue.story_points);
  return Number.isFinite(points) ? points : 0;
}

/** Whether the issue carries an estimate for the statistic (issue count always does). */
export function isEstimated(issue, mode) {
  if (mode === "item_count") return true;
  if (mode === "time") return issue.original_estimate_minutes != null;
  return issue.story_points != null;
}

/** Numbers without trailing zeros: 3, 2.5, 0.25. */
export function formatNumber(value) {
  const n = Math.round(Number(value || 0) * 100) / 100;
  return String(n);
}

/**
 * Jira's sprint header lozenges: the statistic summed by status category.
 * Testing counts as in progress.
 */
export function categoryTotals(issues, mode) {
  const out = { count: 0, total: 0, todo: 0, inProgress: 0, done: 0, unestimated: 0 };
  for (const issue of issues || []) {
    const value = estimateOf(issue, mode);
    out.count += 1;
    out.total += value;
    if (!isEstimated(issue, mode)) out.unestimated += 1;
    const category = issue.status_category;
    if (category === "done") out.done += value;
    else if (category === "in_progress" || category === "testing") out.inProgress += value;
    else out.todo += value;
  }
  for (const key of ["total", "todo", "inProgress", "done"]) {
    out[key] = Math.round(out[key] * 100) / 100;
  }
  return out;
}
