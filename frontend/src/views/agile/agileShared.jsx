/** Shared visuals for the Sprints views: type icons, epic colours, avatar
 * stacks, estimate badges and the sprint date helpers. */
import { initials } from "../../lib/format";
import { itemTypeIcon } from "../../lib/itemTypes";

/** Icon of a work-item type (Task and Sub-task differ, as in Jira). */
export function typeEmoji(t) {
  return itemTypeIcon(t);
}

/** Fallback palette so epics without a saved color still get a distinct bar. */
const EPIC_PALETTE = [
  "#7e57c2", "#2e7d32", "#1565c0", "#ad1457",
  "#ef6c00", "#00838f", "#6d4c41", "#5c6bc0",
];

export function epicColor(epicId, epicsById) {
  if (epicId == null) return null;
  const e = epicsById.get(epicId);
  if (e?.color) return e.color;
  return EPIC_PALETTE[epicId % EPIC_PALETTE.length];
}

export function epicTitle(epicId, epicsById) {
  return epicsById.get(epicId)?.title ?? null;
}

export function AssigneeStack({ assignees }) {
  // Unassigned renders nothing — the old "＋" placeholder looked like an
  // action button but had no handler behind it.
  if (!assignees?.length) return null;
  const shown = assignees.slice(0, 3);
  const extra = assignees.length - shown.length;
  return (
    <div className="card-avatars">
      {shown.map((a) => (
        <span key={a.id} className="avatar" title={a.name}>{initials(a.name)}</span>
      ))}
      {extra > 0 && <span className="avatar avatar-more" title={`+${extra} more`}>+{extra}</span>}
    </div>
  );
}

export function StoryPointBadge({ value }) {
  if (value == null) return null;
  return (
    <span className="sp-badge" title={`${value} story point${value === 1 ? "" : "s"}`}>
      {value}
    </span>
  );
}

export function formatDateRange(start, end) {
  if (!start && !end) return "";
  const fmt = (s) => {
    if (!s) return "?";
    const d = new Date(`${s}T00:00:00`);
    if (Number.isNaN(d.getTime())) return s;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };
  return `${fmt(start)} – ${fmt(end)}`;
}

export function sumStoryPoints(items) {
  return items.reduce((sum, i) => (i.story_points != null ? sum + Number(i.story_points) : sum), 0);
}

/** Local calendar date as "YYYY-MM-DD". toISOString() would convert to UTC and
 * shift the day for anyone east of UTC (e.g. IST midnight is the previous UTC day). */
export function localIsoDate(d) {
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

/** Today as a local "YYYY-MM-DD". */
export function localToday() {
  return localIsoDate(new Date());
}

function parseDay(value) {
  if (!value) return null;
  const d = new Date(`${value}T00:00:00`);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** `start` moved by `n` calendar months, clamped to the target month's last day
 * (Jan 31 + 1 month = Feb 28/29, not Mar 3). */
function addMonths(start, n) {
  const target = new Date(start.getFullYear(), start.getMonth() + n, 1);
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
  target.setDate(Math.min(start.getDate(), lastDay));
  return target;
}

/** Start of the `k`-th period after `start` for a cadence, or null when the
 * cadence has no fixed length. Anchored to `start` so month ends don't drift. */
function periodStart(start, cadence, k) {
  if (cadence === "daily") return new Date(start.getFullYear(), start.getMonth(), start.getDate() + k);
  if (cadence === "weekly") return new Date(start.getFullYear(), start.getMonth(), start.getDate() + 7 * k);
  if (cadence === "monthly") return addMonths(start, k);
  return null;
}

function dayBefore(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() - 1);
}

/** Suggested (inclusive) end date for one sprint of the given cadence: a daily
 * sprint ends the same day, a weekly one six days later, a monthly one the day
 * before the same date next month. Purely a form default the user sees. */
export function suggestEndDate(startDate, cadence) {
  const start = parseDay(startDate);
  const next = start && periodStart(start, cadence, 1);
  return next ? localIsoDate(dayBefore(next)) : "";
}

/** Inclusive sprint periods covering [startDate, endDate] for a cadence. A
 * cadence without a fixed length ("custom") yields one period for the range;
 * the last period is cut off at endDate. */
export function sprintPeriods(startDate, endDate, cadence) {
  const start = parseDay(startDate);
  const end = parseDay(endDate);
  if (!start || !end || end < start) return [];
  if (!periodStart(start, cadence, 1)) return [{ start_date: startDate, end_date: endDate }];
  const periods = [];
  for (let k = 0; ; k += 1) {
    const from = periodStart(start, cadence, k);
    if (from > end) break;
    const to = dayBefore(periodStart(start, cadence, k + 1));
    periods.push({ start_date: localIsoDate(from), end_date: localIsoDate(to > end ? end : to) });
  }
  return periods;
}
