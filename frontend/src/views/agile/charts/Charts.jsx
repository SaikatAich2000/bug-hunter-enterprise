/** Report charts drawn as plain SVG. Each chart has a text alternative (the
 * tables in ReportsPanel carry the same numbers). */
import { useEffect, useMemo, useState } from "react";
import {
  bandPath, boardDayTicks, dateMs, dayLabel, dayTicks, linePath, linearScale, niceTicks, nonWorkingBands, stepPath, toMs,
} from "./chartMath";

const W = 760;
const H = 320;
const M = { top: 16, right: 20, bottom: 44, left: 48 };

function Axes({ x, y, xTicks, yTicks, xFormat = dayLabel, yLabel }) {
  const [x0, x1] = x.range;
  const [y0, y1] = y.range;
  return (
    <g className="ag-axes">
      {yTicks.map((t) => (
        <g key={`y${t}`}>
          <line x1={x0} x2={x1} y1={y(t)} y2={y(t)} className="ag-grid" />
          <text x={x0 - 8} y={y(t)} dy="0.32em" textAnchor="end" className="ag-tick">{t}</text>
        </g>
      ))}
      {xTicks.map((tick) => {
        // A tick is an instant, or {at, label} when the label is given.
        const at = typeof tick === "number" ? tick : tick.at;
        return (
          <g key={`x${at}`}>
            <line x1={x(at)} x2={x(at)} y1={y0} y2={y0 + 5} className="ag-axis" />
            <text x={x(at)} y={y0 + 18} textAnchor="middle" className="ag-tick">
              {typeof tick === "number" ? xFormat(tick) : tick.label}
            </text>
          </g>
        );
      })}
      <line x1={x0} x2={x1} y1={y0} y2={y0} className="ag-axis" />
      <line x1={x0} x2={x0} y1={y0} y2={y1} className="ag-axis" />
      {yLabel && (
        <text x={14} y={(y0 + y1) / 2} transform={`rotate(-90 14 ${(y0 + y1) / 2})`} textAnchor="middle" className="ag-axis-label">
          {yLabel}
        </text>
      )}
    </g>
  );
}

function Legend({ items }) {
  return (
    <ul className="ag-legend">
      {items.map((item) => (
        <li key={item.label}>
          <span className={`ag-legend-swatch ${item.className}`} aria-hidden="true" />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

/** Burndown (remaining + guideline) or burnup (completed + scope) of a sprint. */
export function SprintChart({ report, kind = "burndown", unitLabel }) {
  const [now, setNow] = useState(null);
  useEffect(() => { setNow(Date.now()); }, [report]);
  const geometry = useMemo(() => {
    if (!report?.start || !report.points?.length) return null;
    const start = toMs(report.start);
    const end = Math.max(toMs(report.end || report.start), report.planned_end ? toMs(report.planned_end) : 0);
    const series = report.points.map((p) => ({ x: toMs(p.at), remaining: p.remaining, scope: p.scope, completed: p.completed }));
    const guide = (report.guideline || []).map((g) => ({ x: toMs(g.at), y: g.value }));
    const committed = guide.length ? guide[0].y : series[0].remaining;
    const maxY = Math.max(1, ...series.map((p) => Math.max(p.remaining, p.scope, p.completed)), ...guide.map((g) => g.y));
    const yTicks = niceTicks(0, maxY, 5);
    const x = linearScale([start, Math.max(end, start + 3_600_000)], [M.left, W - M.right]);
    const y = linearScale([0, yTicks[yTicks.length - 1]], [H - M.bottom, M.top]);
    const xTicks = report.days?.length ? boardDayTicks(report.days, 10, [start, end]) : dayTicks(start, end, 10);
    return { series, guide, committed, x, y, yTicks, xTicks, start, end };
  }, [report]);

  if (!geometry) return <p className="muted ag-empty">This sprint has not started, so there is nothing to chart yet.</p>;
  const { series, guide, committed, x, y, yTicks, xTicks } = geometry;
  const nonWorking = report.days
    ? nonWorkingBands(report.days, [geometry.start, geometry.end])
    : (report.non_working_days || []).map((d) => ({ from: dateMs(d), to: dateMs(d) + 86_400_000 }));
  const showToday = report.state === "active" && now != null && now > geometry.start && now < geometry.end;
  const line = (key) => stepPath(series.map((p) => ({ x: p.x, y: p[key] })), x, y);
  const burnupGuide = guide.map((g) => ({ x: g.x, y: committed - g.y }));

  const title = kind === "burndown" ? "Burndown chart" : "Burnup chart";
  return (
    <figure className="ag-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}: ${kind === "burndown" ? `${report.remaining_estimate} remaining of ${report.committed_estimate} committed` : `${report.completed_estimate} completed of ${report.scope_estimate} in scope`}`}>
        {nonWorking.map((d) => (
          <rect key={d.from} x={x(d.from)} y={M.top} width={Math.max(0, x(d.to) - x(d.from))}
            height={H - M.top - M.bottom} className="ag-nonworking" />
        ))}
        <Axes x={x} y={y} xTicks={xTicks} yTicks={yTicks} yLabel={unitLabel} />
        {kind === "burndown" ? (
          <>
            {guide.length > 1 && <path d={linePath(guide, x, y)} className="ag-line-guide" />}
            <path d={line("remaining")} className="ag-line-remaining" />
            {report.events.map((e) => (
              <circle key={`${e.at}-${e.work_item_id}-${e.event}`} cx={x(toMs(e.at))} cy={y(e.remaining)} r={3.5}
                className={e.scope_change ? "ag-dot-scope" : "ag-dot"}>
                <title>{`${new Date(e.at).toLocaleString()}: ${e.display_id} ${e.event} (${e.change > 0 ? "+" : ""}${e.change}) → ${e.remaining}`}</title>
              </circle>
            ))}
          </>
        ) : (
          <>
            {burnupGuide.length > 1 && <path d={linePath(burnupGuide, x, y)} className="ag-line-guide" />}
            <path d={line("scope")} className="ag-line-scope" />
            <path d={line("completed")} className="ag-line-completed" />
          </>
        )}
        {showToday && (
          <g>
            <line x1={x(now)} x2={x(now)} y1={M.top} y2={H - M.bottom} className="ag-today" />
            <text x={x(now) + 4} y={M.top + 10} className="ag-tick">Today</text>
          </g>
        )}
      </svg>
      <Legend items={kind === "burndown"
        ? [{ label: "Remaining", className: "remaining" }, { label: "Guideline", className: "guide" },
          { label: "Scope change", className: "scope" }, { label: "Non-working day", className: "nonworking" }]
        : [{ label: "Completed", className: "completed" }, { label: "Total scope", className: "scope-line" },
          { label: "Guideline", className: "guide" }]} />
    </figure>
  );
}

/** Commitment vs completed per sprint, with the average completed. */
export function VelocityChart({ report, unitLabel }) {
  const points = report?.points || [];
  if (!points.length) return <p className="muted ag-empty">No completed sprints yet.</p>;
  const maxY = Math.max(1, ...points.map((p) => Math.max(p.committed_estimate, p.completed_estimate)));
  const yTicks = niceTicks(0, maxY, 5);
  const y = linearScale([0, yTicks[yTicks.length - 1]], [H - M.bottom, M.top]);
  const slot = (W - M.left - M.right) / points.length;
  const bar = Math.min(34, slot / 3);
  const x = linearScale([0, points.length], [M.left, W - M.right]);
  return (
    <figure className="ag-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Velocity chart, average ${report.average_completed} completed per sprint`}>
        <Axes x={x} y={y} xTicks={[]} yTicks={yTicks} yLabel={unitLabel} />
        {points.map((p, n) => {
          const cx = M.left + slot * n + slot / 2;
          return (
            <g key={p.sprint_id}>
              <rect x={cx - bar - 2} y={y(p.committed_estimate)} width={bar} height={y(0) - y(p.committed_estimate)} className="ag-bar-commit">
                <title>{`${p.sprint_name}: commitment ${p.committed_estimate}`}</title>
              </rect>
              <rect x={cx + 2} y={y(p.completed_estimate)} width={bar} height={y(0) - y(p.completed_estimate)} className="ag-bar-done">
                <title>{`${p.sprint_name}: completed ${p.completed_estimate}`}</title>
              </rect>
              <text x={cx} y={H - M.bottom + 18} textAnchor="middle" className="ag-tick">
                {p.sprint_name.length > 16 ? `${p.sprint_name.slice(0, 15)}…` : p.sprint_name}
              </text>
            </g>
          );
        })}
        <line x1={M.left} x2={W - M.right} y1={y(report.average_completed)} y2={y(report.average_completed)} className="ag-line-average" />
      </svg>
      <Legend items={[{ label: "Commitment", className: "commit" }, { label: "Completed", className: "completed" },
        { label: `Average completed (${report.average_completed})`, className: "average" }]} />
    </figure>
  );
}

const CATEGORY_CLASS = { todo: "todo", in_progress: "doing", testing: "testing", done: "done" };

/** Issues per board column per day, stacked (right-most column at the bottom). */
export function FlowChart({ report }) {
  const points = report?.points || [];
  const columns = report?.columns || [];
  if (!points.length || !columns.length) return <p className="muted ag-empty">No data for this range.</p>;
  const xs = points.map((p) => dateMs(p.date));
  const order = [...columns].reverse();
  const totals = points.map((p) => columns.reduce((sum, c) => sum + (p.counts[String(c.id)] || 0), 0));
  const yTicks = niceTicks(0, Math.max(1, ...totals), 5);
  const x = linearScale([xs[0], Math.max(xs[xs.length - 1], xs[0] + 1)], [M.left, W - M.right]);
  const y = linearScale([0, yTicks[yTicks.length - 1]], [H - M.bottom, M.top]);
  const bands = order.reduce((acc, col) => {
    const lower = acc.length ? acc[acc.length - 1].upper : points.map(() => 0);
    const upper = points.map((p, i) => lower[i] + (p.counts[String(col.id)] || 0));
    return [...acc, { col, upper, d: bandPath(xs, upper, lower, x, y) }];
  }, []);
  return (
    <figure className="ag-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Cumulative flow diagram">
        <Axes x={x} y={y} xTicks={dayTicks(xs[0], xs[xs.length - 1], 10)} yTicks={yTicks} yLabel="Issues" />
        {bands.map(({ col, d }) => (
          <path key={col.id} d={d} className={`ag-band ag-band-${CATEGORY_CLASS[col.category] || "todo"}`}>
            <title>{col.name}</title>
          </path>
        ))}
      </svg>
      <Legend items={columns.map((c) => ({ label: c.name, className: `band-${CATEGORY_CLASS[c.category] || "todo"}` }))} />
    </figure>
  );
}

/** Cycle time per completed issue with the rolling and overall averages. */
export function CycleChart({ report }) {
  const entries = report?.entries || [];
  if (!entries.length) return <p className="muted ag-empty">No issues were completed in this range.</p>;
  const xs = entries.map((e) => toMs(e.completed_at));
  const lo = Math.min(...xs);
  const hi = Math.max(...xs);
  const pad = Math.max(3_600_000, (hi - lo) * 0.05);
  const yTicks = niceTicks(0, Math.max(1, report.max_days), 5);
  const x = linearScale([lo - pad, hi + pad], [M.left, W - M.right]);
  const y = linearScale([0, yTicks[yTicks.length - 1]], [H - M.bottom, M.top]);
  const rolling = (report.rolling_average || []).map((p) => ({ x: toMs(p.completed_at), y: p.value }));
  return (
    <figure className="ag-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Control chart: average cycle time ${report.average_days} days over ${report.count} issues`}>
        <Axes x={x} y={y} xTicks={dayTicks(lo - pad, hi + pad, 10)} yTicks={yTicks} yLabel="Cycle time (days)" />
        <line x1={M.left} x2={W - M.right} y1={y(report.average_days)} y2={y(report.average_days)} className="ag-line-average" />
        {rolling.length > 1 && <path d={linePath(rolling, x, y)} className="ag-line-rolling" />}
        {entries.map((e) => (
          <circle key={`${e.work_item_id}-${e.completed_at}`} cx={x(toMs(e.completed_at))} cy={y(e.cycle_time_days)} r={4} className="ag-dot-issue">
            <title>{`${e.display_id} ${e.title}: ${e.cycle_time_days} days`}</title>
          </circle>
        ))}
      </svg>
      <Legend items={[{ label: "Issue", className: "issue" }, { label: "Rolling average", className: "rolling" },
        { label: `Average (${report.average_days} days)`, className: "average" }]} />
    </figure>
  );
}
