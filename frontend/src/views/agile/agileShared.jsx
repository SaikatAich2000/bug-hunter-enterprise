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
