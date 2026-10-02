/** Analytics view. Charts scope to the active type-tab; stats come from AppContext. */
import { Fragment, useEffect } from "react";
import { useApp } from "../state/AppContext";
import { initials } from "../lib/format";
const TAB_NOUNS = {
  all: "Items",
  Bug: "Bugs",
  Requirement: "Requirements",
  Task: "Tasks",
};

/** Maps status/priority/env category keys to their chart fill colors. */
function kindColor(kind, k) {
  const map = {
    status: {
      "New": "#5a9fd4", "In Progress": "#d4a05a", "Resolved": "#7ca860",
      "Closed": "#8b8270", "Reopened": "#a87fb8",
      "Not a Bug": "#64748b", "Resolve Later": "#f59e0b",
    },
    priority: { Low: "#8b8270", Medium: "#5a9fd4", High: "#d4a05a", Critical: "#c5524a" },
    env: { DEV: "#5a9fd4", UAT: "#d4a05a", PROD: "#c5524a" },
  };
  return map[kind]?.[k] ?? "#8b8270";
}

/** SVG line + area chart for time-series data. */
function TimelineChart({ data }) {
  if (!data.length) return <p className="muted">No data</p>;
  const W = 600;
  const H = 200;
  const P = 30;
  const max = Math.max(1, ...data.map((d) => d.count));
  const stepX = (W - 2 * P) / Math.max(1, data.length - 1);
  const pts = data.map((d, i) => ({
    d,
    i,
    x: P + i * stepX,
    y: H - P - (d.count / max) * (H - 2 * P),
  }));
  const path = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ");
  const area =
    `M ${P} ${H - P} ` +
    pts.map((p) => `L ${p.x} ${p.y}`).join(" ") +
    ` L ${W - P} ${H - P} Z`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" style={{ color: "var(--accent)" }}>
      <path d={area} fill="currentColor" opacity="0.18" />
      <path d={path} stroke="currentColor" strokeWidth="2" fill="none" />
      {pts.map((p) => (
        <circle key={p.d.date} cx={p.x} cy={p.y} r={3} fill="currentColor">
          <title>{`${p.d.date}: ${p.d.count}`}</title>
        </circle>
      ))}
      {pts.map((p) =>
        p.i % 3 === 0 ? (
          <text
            key={p.d.date}
            x={p.x}
            y={H - 8}
            textAnchor="middle"
            fill="currentColor"
            fontSize={10}
            opacity={0.6}
          >
            {p.d.date.slice(5)}
          </text>
        ) : null,
      )}
    </svg>
  );
}

/** SVG vertical bar chart for categorical counts. */
function BarsChart({ data, kind }) {
  const entries = Object.entries(data ?? {});
  if (!entries.length) return <p className="muted">No data</p>;
  const W = 600;
  const H = 200;
  const P = 30;
  const max = Math.max(1, ...entries.map(([, v]) => v));
  const slot = (W - 2 * P) / entries.length;
  const bw = slot - 8;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
      {entries.map(([k, v], i) => {
        const x = P + i * slot;
        const h = (v / max) * (H - 2 * P);
        const y = H - P - h;
        return (
          <Fragment key={k}>
            <rect x={x} y={y} width={bw} height={h} fill={kindColor(kind, k)} rx={3}>
              <title>{`${k}: ${v}`}</title>
            </rect>
            <text x={x + bw / 2} y={H - 12} textAnchor="middle" fill="currentColor" fontSize={10} opacity={0.7}>
              {k}
            </text>
            <text x={x + bw / 2} y={y - 4} textAnchor="middle" fill="currentColor" fontSize={11} fontWeight={600}>
              {v}
            </text>
          </Fragment>
        );
      })}
    </svg>
  );
}

/** Horizontal bar rows for project breakdown, using each project's color. */
function ProjectBars({ rows }) {
  if (!rows.length) return <p className="muted">No data</p>;
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <>
      {rows.map((r) => (
        <div className="bar-row" key={r.id}>
          <div className="bar-label">
            <span>
              <span className="swatch dot" style={{ background: r.color }}></span>
              {r.name}
            </span>
            <span>{r.count}</span>
          </div>
          <div className="bar-track">
            <div
              className="bar-fill"
              style={{ width: `${(r.count / max) * 100}%`, background: r.color }}
            ></div>
          </div>
        </div>
      ))}
    </>
  );
}

/** Horizontal bar rows for top assignees, with avatar initials. */
function AssigneeBars({ rows }) {
  if (!rows.length) return <p className="muted">No assignments yet</p>;
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <>
      {rows.map((r) => (
        <div className="bar-row" key={r.id}>
          <div className="bar-label">
            <span>
              <span className="avatar mini">{initials(r.name)}</span>
              {r.name}
            </span>
            <span>{r.count}</span>
          </div>
          <div className="bar-track">
            <div
              className="bar-fill"
              style={{ width: `${(r.count / max) * 100}%`, background: "var(--accent)" }}
            ></div>
          </div>
        </div>
      ))}
    </>
  );
}

export default function AnalyticsView() {
  const { stats, activeTab, refreshStats, filters } = useApp();

  // Re-fetch on status-filter change so charts react to KPI tile clicks.
  const statusKey = filters.status.join(",");
  useEffect(() => {
    void refreshStats();
  }, [refreshStats, statusKey]);

  const noun = TAB_NOUNS[activeTab] ?? "Items";
  // Environment is not meaningful for Requirements or Tasks.
  const hideEnv = activeTab === "Requirement" || activeTab === "Task";

  return (
    <section className="view" id="viewAnalytics">
      <div className="charts-grid">
        <div className="chart-card">
          <h3 id="chartTimelineTitle">{`${noun} over the last 14 days`}</h3>
          <div id="chartTimeline" className="chart-host">
            {stats ? <TimelineChart data={stats.timeline} /> : null}
          </div>
        </div>
        <div className="chart-card">
          <h3 id="chartStatusTitle">{`By Status (${noun})`}</h3>
          <div id="chartStatus" className="chart-host">
            {stats ? <BarsChart data={stats.by_status} kind="status" /> : null}
          </div>
        </div>
        <div className="chart-card">
          <h3 id="chartPriorityTitle">{`By Priority (${noun})`}</h3>
          <div id="chartPriority" className="chart-host">
            {stats ? <BarsChart data={stats.by_priority} kind="priority" /> : null}
          </div>
        </div>
        <div className="chart-card" id="chartEnvironmentCard" hidden={hideEnv}>
          <h3 id="chartEnvironmentTitle">{`By Environment (${noun})`}</h3>
          <div id="chartEnvironment" className="chart-host">
            {stats ? <BarsChart data={stats.by_environment} kind="env" /> : null}
          </div>
        </div>
        <div className="chart-card">
          <h3 id="chartProjectTitle">{`By Project (${noun})`}</h3>
          <div id="chartProject" className="chart-host">
            {stats ? <ProjectBars rows={stats.by_project} /> : null}
          </div>
        </div>
        <div className="chart-card">
          <h3 id="chartAssigneeTitle">{`Top Assignees (${noun})`}</h3>
          <div id="chartAssignee" className="chart-host">
            {stats ? <AssigneeBars rows={stats.by_assignee} /> : null}
          </div>
        </div>
      </div>
    </section>
  );
}
