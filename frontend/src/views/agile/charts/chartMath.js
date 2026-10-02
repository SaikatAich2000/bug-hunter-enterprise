/** Geometry for the report charts (plain SVG, no chart library). */

/** Linear map from ``domain`` to ``range``; a flat domain maps to the range start. */
export function linearScale([d0, d1], [r0, r1]) {
  const span = d1 - d0;
  const fn = (value) => (span === 0 ? r0 : r0 + ((value - d0) / span) * (r1 - r0));
  fn.domain = [d0, d1];
  fn.range = [r0, r1];
  return fn;
}

/** "Nice" round tick values covering [min, max] (1, 2, 2.5 or 5 x 10^n steps). */
export function niceTicks(min, max, count = 5) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0];
  if (max < min) [min, max] = [max, min];
  if (max === min) max = min + 1;
  const raw = (max - min) / Math.max(1, count);
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? 10 * magnitude;
  const start = Math.floor(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 1e-9; v += step) {
    ticks.push(Math.round(v * 1e6) / 1e6);
  }
  if (ticks[ticks.length - 1] < max) ticks.push(Math.round((ticks[ticks.length - 1] + step) * 1e6) / 1e6);
  return ticks;
}

/** SVG path of a step series: each value holds until the next point (step-after). */
export function stepPath(points, x, y) {
  if (!points.length) return "";
  let d = `M${x(points[0].x).toFixed(1)},${y(points[0].y).toFixed(1)}`;
  for (let i = 1; i < points.length; i += 1) {
    d += ` H${x(points[i].x).toFixed(1)} V${y(points[i].y).toFixed(1)}`;
  }
  return d;
}

/** SVG path through the points with straight segments. */
export function linePath(points, x, y) {
  return points
    .map((p, i) => `${i ? "L" : "M"}${x(p.x).toFixed(1)},${y(p.y).toFixed(1)}`)
    .join(" ");
}

/** Closed area path between an upper and a lower series sharing x values. */
export function bandPath(xs, upper, lower, x, y) {
  if (!xs.length) return "";
  const top = xs.map((v, i) => `${i ? "L" : "M"}${x(v).toFixed(1)},${y(upper[i]).toFixed(1)}`).join(" ");
  const bottom = xs
    .map((v, i) => [v, lower[i]])
    .reverse()
    .map(([v, l]) => `L${x(v).toFixed(1)},${y(l).toFixed(1)}`)
    .join(" ");
  return `${top} ${bottom} Z`;
}

/** Midnight-aligned day ticks between two instants (local time), thinned to ``max``. */
export function dayTicks(startMs, endMs, max = 10) {
  if (!(endMs > startMs)) return [startMs];
  const first = new Date(startMs);
  first.setHours(0, 0, 0, 0);
  if (first.getTime() < startMs) first.setDate(first.getDate() + 1);
  const days = [];
  for (const d = new Date(first); d.getTime() <= endMs; d.setDate(d.getDate() + 1)) {
    days.push(d.getTime());
  }
  // A range inside one day still gets a tick, at its start.
  if (!days.length) return [startMs];
  return thin(days, max);
}

function thin(values, max) {
  if (values.length <= max) return values;
  const every = Math.ceil(values.length / max);
  return values.filter((_, i) => i % every === 0);
}

/**
 * Day ticks from the server's board-local days (``report.days``): each tick
 * sits at the board's midnight and is labelled with the board's date, so a
 * viewer in another timezone sees the board's calendar.
 */
export function boardDayTicks(days, max = 10, domain = [-Infinity, Infinity]) {
  const [lo, hi] = domain;
  const ticks = (days || []).map((d) => ({ at: toMs(d.start), label: boardDayLabel(d.date) }))
    .filter((t) => t.at >= lo && t.at <= hi);
  return thin(ticks, max);
}

/** The board's non-working days as [from, to) instant ranges, clipped to the plotted ``domain``. */
export function nonWorkingBands(days, domain = [-Infinity, Infinity]) {
  const [lo, hi] = domain;
  return (days || []).filter((d) => !d.working)
    .map((d) => ({ from: Math.max(toMs(d.start), lo), to: Math.min(toMs(d.end), hi) }))
    .filter((band) => band.to > band.from);
}

/** "Mar 5" for an ISO date, independent of the viewer's timezone. */
export function boardDayLabel(isoDate) {
  return new Date(`${isoDate}T12:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

/** Short day label ("Mar 5"). */
export function dayLabel(ms) {
  return new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Parse an ISO date-time or date to epoch ms. */
export function toMs(value) {
  return typeof value === "number" ? value : Date.parse(value);
}

/** Epoch ms of the start of an ISO calendar date, local time. */
export function dateMs(isoDate) {
  return new Date(`${isoDate}T00:00:00`).getTime();
}
