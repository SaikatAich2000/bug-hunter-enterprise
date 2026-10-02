/** Small building blocks every Sprints view renders an issue with. */
import { itemTypeIcon, itemTypeLabel } from "../../lib/itemTypes";
import { epicColor } from "./agileShared";
import { estimateOf, formatNumber, isEstimated, statisticUnit } from "./logic/estimates";

export function TypeIcon({ type }) {
  const label = itemTypeLabel(type);
  return (
    <span className="ag-type" title={label} role="img" aria-label={label}>
      {itemTypeIcon(type)}
    </span>
  );
}

/** The issue key, opening the issue on click. */
export function IssueKey({ issue, onOpen }) {
  const key = issue.display_id || `#${issue.id}`;
  if (!onOpen) return <span className="ag-key">{key}</span>;
  return (
    <button type="button" className="ag-key ag-link" onClick={(e) => { e.stopPropagation(); onOpen(issue.id); }}>
      {key}
    </button>
  );
}

const CATEGORY_LABEL = { todo: "To do", in_progress: "In progress", testing: "In progress", done: "Done" };

/** Status lozenge coloured by its Jira category (grey / blue / green). */
export function StatusLozenge({ status, category }) {
  const cat = category === "testing" ? "in_progress" : category || "todo";
  return (
    <span className={`ag-lozenge ag-cat-${cat}`} title={`${status} (${CATEGORY_LABEL[cat] ?? cat})`}>
      {status}
    </span>
  );
}

export function EpicLozenge({ epicId, epicsById }) {
  if (epicId == null) return null;
  const epic = epicsById?.get(epicId);
  if (!epic) return null;
  return (
    <span className="ag-epic" style={{ "--epic-color": epicColor(epicId, epicsById) }} title={`Epic: ${epic.title}`}>
      {epic.title}
    </span>
  );
}

// Jira-style priority marks: chevrons up for urgent work, an equals sign
// for medium and a chevron down for low.
const PRIORITY_PATHS = {
  Critical: ["M3 9l5-4 5 4", "M3 13l5-4 5 4"],
  High: ["M3 11l5-4 5 4"],
  Medium: ["M3 6h10", "M3 10h10"],
  Low: ["M3 6l5 4 5-4"],
};

export function PriorityIcon({ priority }) {
  const paths = PRIORITY_PATHS[priority] ?? PRIORITY_PATHS.Medium;
  return (
    <span className={`ag-priority ag-priority-${(priority || "medium").toLowerCase()}`} title={`Priority: ${priority}`}
      role="img" aria-label={`Priority ${priority}`}>
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
        {paths.map((d) => <path key={d} d={d} fill="none" stroke="currentColor" strokeWidth="2"
          strokeLinecap="round" strokeLinejoin="round" />)}
      </svg>
    </span>
  );
}

/** The issue's estimate in the board's statistic; "–" when unestimated. */
export function EstimateBadge({ issue, mode }) {
  if (mode === "item_count") return null;
  const estimated = isEstimated(issue, mode);
  const unit = statisticUnit(mode);
  const text = estimated ? formatNumber(estimateOf(issue, mode)) : "–";
  return (
    <span className={`ag-estimate${estimated ? "" : " ag-estimate-none"}`}
      title={estimated ? `${text} ${unit}` : "Not estimated"}>
      {text}
    </span>
  );
}

export function SubtaskProgress({ issue }) {
  if (!issue.subtask_count) return null;
  return (
    <span className="ag-subtasks" title={`${issue.subtasks_done} of ${issue.subtask_count} sub-tasks done`}>
      🔹 {issue.subtasks_done}/{issue.subtask_count}
    </span>
  );
}

/** Lozenges for the sprint/backlog header: to do / in progress / done totals. */
export function CategoryTotals({ totals, mode }) {
  const unit = statisticUnit(mode);
  return (
    <span className="ag-totals" title={`${unit}: to do / in progress / done`}>
      <span className="ag-total ag-cat-todo">{formatNumber(totals.todo)}</span>
      <span className="ag-total ag-cat-in_progress">{formatNumber(totals.inProgress)}</span>
      <span className="ag-total ag-cat-done">{formatNumber(totals.done)}</span>
    </span>
  );
}
