/** Reports (Jira Software): burndown, burnup, sprint report, velocity,
 * cumulative flow, control chart, epic report, daily summary, workload. */
import { useEffect, useMemo, useState } from "react";
import { useApp } from "../../state/AppContext";
import BhSelect from "../../components/BhSelect";
import BhDateInput from "../../components/BhDateInput";
import { apiBlob } from "../../lib/api";
import { toastError } from "../../lib/toast";
import { agileApi } from "./agileApi";
import { useLatestLoad } from "./useAgileProject";
import { IssueKey, StatusLozenge, TypeIcon } from "./IssueBits";
import { formatDateRange, localIsoDate, localToday } from "./agileShared";
import { CycleChart, FlowChart, SprintChart, VelocityChart } from "./charts/Charts";
import { formatNumber, statisticLabel } from "./logic/estimates";

const REPORTS = [
  { key: "burndown", label: "Burndown chart", scope: "sprint", help: "Work left in the sprint against the ideal guideline." },
  { key: "burnup", label: "Burnup chart", scope: "sprint", help: "Work completed against the sprint's total scope." },
  { key: "sprint", label: "Sprint report", scope: "sprint", help: "What was committed, completed, carried over and removed." },
  { key: "velocity", label: "Velocity chart", scope: "board", help: "Commitment and completed work of recent sprints." },
  { key: "cfd", label: "Cumulative flow diagram", scope: "range", help: "Issues in each board column, day by day." },
  { key: "control", label: "Control chart", scope: "range", help: "Cycle time of completed issues." },
  { key: "epic", label: "Epic report", scope: "epic", help: "An epic's progress and the work each sprint completed." },
  { key: "daily", label: "Daily summary", scope: "sprint", help: "Where every sprint issue stood at the end of a day." },
  { key: "workload", label: "Workload", scope: "sprint", help: "Assigned work per person against their capacity." },
];

function daysAgo(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return localIsoDate(d);
}

async function download(path, fallbackName, method = "GET") {
  try {
    const { blob, filename } = await apiBlob(path, { method });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || fallbackName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    toastError(err);
  }
}

function IssueTable({ title, rows, mode, onOpen, showStart }) {
  return (
    <section className="ag-report-section">
      <h4>{title} <span className="muted small">({rows.length})</span></h4>
      {rows.length === 0 ? <p className="muted small">None.</p> : (
        <table className="reports-table ag-table">
          <thead>
            <tr><th>Key</th><th>Summary</th><th>Type</th><th>Status</th>
              {mode !== "item_count" && <th className="num">{showStart ? "Estimate (start → end)" : "Estimate"}</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td><IssueKey issue={r} onOpen={onOpen} />{r.added_during_sprint && <span title="Added after the sprint started"> *</span>}</td>
                <td>{r.title}</td>
                <td><TypeIcon type={r.item_type} /> {r.item_type}</td>
                <td><StatusLozenge status={r.status} category={r.status_category} /></td>
                {mode !== "item_count" && (
                  <td className="num">
                    {showStart && r.estimate_at_start != null && r.estimate_at_start !== r.estimate
                      ? `${formatNumber(r.estimate_at_start)} → ${formatNumber(r.estimate)}` : formatNumber(r.estimate)}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function SprintReport({ report, onOpen }) {
  const mode = report.estimation_mode;
  const rate = report.committed_estimate ? Math.round((report.completed_estimate / report.committed_estimate) * 100) : 0;
  return (
    <div>
      <div className="report-kpis">
        <div className="report-kpi"><span className="report-kpi-num">{formatNumber(report.committed_estimate)}</span><span>Committed at start</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{formatNumber(report.completed_estimate)}</span><span>Completed</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{formatNumber(report.incomplete_estimate)}</span><span>Not completed</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{formatNumber(report.added_estimate)}</span><span>Added after start</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{formatNumber(report.removed_estimate)}</span><span>Removed</span></div>
        {mode !== "item_count" && (
          <div className="report-kpi"><span className="report-kpi-num">{rate}%</span><span>Completed vs committed</span></div>
        )}
      </div>
      {!report.history_complete && (
        <p className="ag-notice">This sprint started before full change history was recorded; its figures come from the sprint&apos;s start and completion records.</p>
      )}
      <IssueTable title="Completed issues" rows={report.completed} mode={mode} onOpen={onOpen} showStart />
      <IssueTable title="Issues not completed" rows={report.not_completed} mode={mode} onOpen={onOpen} showStart />
      <IssueTable title="Issues removed from sprint" rows={report.removed} mode={mode} onOpen={onOpen} />
      {report.completed_outside.length > 0 && (
        <IssueTable title="Issues completed outside of this sprint" rows={report.completed_outside} mode={mode} onOpen={onOpen} />
      )}
      <p className="muted small">* Added to the sprint after it started. Estimates in {statisticLabel(mode).toLowerCase()}.</p>
    </div>
  );
}

function BurnTable({ report }) {
  if (!report.events.length) return <p className="muted small">No changes since the sprint started.</p>;
  return (
    <table className="reports-table ag-table">
      <caption className="muted small">Changes during the sprint</caption>
      <thead><tr><th>When</th><th>Issue</th><th>Event</th><th className="num">Change</th><th className="num">Remaining</th></tr></thead>
      <tbody>
        {report.events.map((e) => (
          <tr key={`${e.at}-${e.work_item_id}-${e.event}`}>
            <td>{new Date(e.at).toLocaleString()}</td>
            <td>{e.display_id} {e.title}</td>
            <td>{e.event}{e.scope_change && <span className="ag-scope-tag"> scope change</span>}</td>
            <td className="num">{e.change > 0 ? "+" : ""}{formatNumber(e.change)}</td>
            <td className="num">{formatNumber(e.remaining)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function EpicReport({ report, onOpen }) {
  const mode = report.estimation_mode;
  const p = report.progress;
  return (
    <div>
      <div className="report-kpis">
        <div className="report-kpi"><span className="report-kpi-num">{p.completed_child_count}/{p.child_count}</span><span>Issues done</span></div>
        {mode !== "item_count" && <div className="report-kpi"><span className="report-kpi-num">{formatNumber(p.completed_estimate)}/{formatNumber(p.total_estimate)}</span><span>Estimate done</span></div>}
        <div className="report-kpi"><span className="report-kpi-num">{p.unestimated_count}</span><span>Unestimated</span></div>
      </div>
      <section className="ag-report-section">
        <h4>By sprint</h4>
        {report.sprints.length === 0 ? <p className="muted small">No sprint has worked on this epic yet.</p> : (
          <table className="reports-table ag-table">
            <thead><tr><th>Sprint</th><th className="num">Completed in sprint</th><th className="num">Epic scope at sprint end</th><th className="num">Remaining at sprint end</th></tr></thead>
            <tbody>
              {report.sprints.map((s) => (
                <tr key={s.sprint_id}>
                  <td>{s.sprint_name}{s.state === "active" && " (active)"}</td>
                  <td className="num">{formatNumber(s.completed_estimate)} ({s.completed_count})</td>
                  <td className="num">{formatNumber(s.scope_estimate)}</td>
                  <td className="num">{formatNumber(s.remaining_estimate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <IssueTable title="Done" rows={report.done} mode={mode} onOpen={onOpen} />
      <IssueTable title="In progress" rows={report.in_progress} mode={mode} onOpen={onOpen} />
      <IssueTable title="To do" rows={report.todo} mode={mode} onOpen={onOpen} />
    </div>
  );
}

function DailyReport({ report }) {
  return (
    <div>
      <div className="report-kpis">
        <div className="report-kpi"><span className="report-kpi-num">{report.todo_count}</span><span>To do</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{report.in_progress_count}</span><span>In progress</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{report.testing_count}</span><span>Testing</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{report.done_count}</span><span>Done</span></div>
        <div className="report-kpi"><span className="report-kpi-num">{report.blocked_count}</span><span>Blocked or flagged</span></div>
      </div>
      <p className="report-points">
        <span>Completed: <strong>{formatNumber(report.completed_estimate)}</strong></span>
        <span>Remaining: <strong>{formatNumber(report.remaining_estimate)}</strong></span>
        <span>Completed this day: <strong>{formatNumber(report.completed_estimate_today)}</strong></span>
        <span>Added this day: <strong>{report.scope_added_today}</strong></span>
        <span>Removed this day: <strong>{report.scope_removed_today}</strong></span>
      </p>
      <h4>Issues at the end of the day</h4>
      <table className="reports-table ag-table">
        <thead><tr><th>Key</th><th>Summary</th><th>Status</th><th>Column</th><th className="num">Estimate</th></tr></thead>
        <tbody>
          {report.items.map((i) => (
            <tr key={i.work_item_id}><td>{i.display_id}</td><td>{i.title}</td><td>{i.status}</td><td>{i.column}</td><td className="num">{formatNumber(i.story_points)}</td></tr>
          ))}
        </tbody>
      </table>
      <h4>What moved that day</h4>
      {report.transitions.length === 0 ? <p className="muted small">Nothing changed.</p> : (
        <table className="reports-table ag-table">
          <thead><tr><th>When</th><th>Issue</th><th>Event</th><th>Detail</th></tr></thead>
          <tbody>
            {report.transitions.map((t) => (
              <tr key={`${t.occurred_at}-${t.work_item_id}-${t.event_type}`}>
                <td>{new Date(t.occurred_at).toLocaleTimeString()}</td><td>{t.title}</td><td>{t.event_type.replace("_", " ")}</td><td>{t.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {report.blockers.length > 0 && (
        <>
          <h4>Blocked or flagged now</h4>
          <ul>{report.blockers.map((b) => <li key={b.work_item_id}>{b.title} — {b.blocked_reason}</li>)}</ul>
        </>
      )}
    </div>
  );
}

export default function ReportsPanel({ projectId, board }) {
  const { openBugDetail } = useApp();
  const mode = board.estimation_mode;
  const [reportKey, setReportKey] = useState("burndown");
  const [sprintId, setSprintId] = useState(null);
  const [epicId, setEpicId] = useState(null);
  const [range, setRange] = useState(() => ({ from: daysAgo(30), to: localToday() }));
  const [day, setDay] = useState(localToday);

  const sprints = useLatestLoad(() => agileApi.sprints(board.id), [board.id]);
  const epics = useLatestLoad(() => agileApi.epics(projectId), [projectId]);
  const reportable = useMemo(() => (sprints.data || [])
    .filter((s) => s.state === "active" || s.state === "closed")
    .sort((a, b) => b.sequence_number - a.sequence_number), [sprints.data]);

  useEffect(() => {
    if (!reportable.length) setSprintId(null);
    else if (!reportable.some((s) => s.id === sprintId)) setSprintId(reportable[0].id);
  }, [reportable, sprintId]);
  useEffect(() => {
    const list = epics.data || [];
    if (!list.length) setEpicId(null);
    else if (!list.some((e) => e.id === epicId)) setEpicId(list[0].id);
  }, [epics.data, epicId]);

  const meta = REPORTS.find((r) => r.key === reportKey);
  const rangeValid = range.from && range.to && range.from <= range.to;
  const result = useLatestLoad(async () => {
    if (meta.scope === "sprint" && !sprintId) return null;
    if (meta.scope === "epic" && !epicId) return null;
    if (meta.scope === "range" && !rangeValid) return null;
    switch (reportKey) {
      case "burndown":
      case "burnup": return agileApi.burndown(sprintId);
      case "sprint": return agileApi.sprintReport(sprintId);
      case "velocity": return agileApi.velocity(board.id, 7);
      case "cfd": return agileApi.cumulativeFlow(board.id, range.from, range.to);
      case "control": return agileApi.controlChart(board.id, range.from, range.to);
      case "epic": return agileApi.epicReport(epicId);
      case "daily": return agileApi.daily(sprintId, day);
      case "workload": return agileApi.workload(sprintId);
      default: return null;
    }
  }, [reportKey, sprintId, epicId, range.from, range.to, day, board.id]);

  const sprint = reportable.find((s) => s.id === sprintId);
  const unitLabel = statisticLabel(mode);
  const exportKey = { burndown: "burndown", burnup: "burnup", sprint: "sprint", velocity: "velocity", cfd: "cumulative-flow", control: "control-chart", epic: "epic", workload: "workload" }[reportKey];
  const exportPath = () => {
    const q = new URLSearchParams({ report_key: exportKey });
    if (meta.scope === "sprint") q.set("sprint_id", sprintId);
    if (meta.scope === "board" || meta.scope === "range") q.set("board_id", board.id);
    if (meta.scope === "range") { q.set("date_from", range.from); q.set("date_to", range.to); }
    if (meta.scope === "epic") q.set("epic_id", epicId);
    return `/agile/reports/export?${q}`;
  };

  const data = result.data;
  let body = null;
  if (meta.scope === "sprint" && !reportable.length) body = <p className="muted ag-empty">Start a sprint to see this report.</p>;
  else if (meta.scope === "epic" && !(epics.data || []).length) body = <p className="muted ag-empty">Create an epic to see this report.</p>;
  else if (meta.scope === "range" && !rangeValid) body = <p className="ag-error">Pick a start date on or before the end date.</p>;
  else if (!data) body = result.loading ? <div className="sprints-loading">Loading…</div> : null;
  else if (reportKey === "burndown" || reportKey === "burnup") {
    body = (
      <>
        <div className="report-kpis">
          <div className="report-kpi"><span className="report-kpi-num">{formatNumber(data.committed_estimate)}</span><span>Committed</span></div>
          <div className="report-kpi"><span className="report-kpi-num">{formatNumber(data.remaining_estimate)}</span><span>Remaining</span></div>
          <div className="report-kpi"><span className="report-kpi-num">{formatNumber(data.completed_estimate)}</span><span>Completed</span></div>
          <div className="report-kpi"><span className="report-kpi-num">{formatNumber(data.scope_estimate)}</span><span>Total scope</span></div>
        </div>
        <SprintChart report={data} kind={reportKey} unitLabel={unitLabel} />
        <BurnTable report={data} />
      </>
    );
  } else if (reportKey === "sprint") body = <SprintReport report={data} onOpen={openBugDetail} />;
  else if (reportKey === "velocity") {
    body = (
      <>
        <VelocityChart report={data} unitLabel={unitLabel} />
        {data.points.length > 0 && (
          <table className="reports-table ag-table">
            <thead><tr><th>Sprint</th><th>Dates</th><th className="num">Commitment</th><th className="num">Completed</th></tr></thead>
            <tbody>{data.points.map((p) => (
              <tr key={p.sprint_id}><td>{p.sprint_name}</td><td>{formatDateRange(p.start_date, p.end_date)}</td>
                <td className="num">{formatNumber(p.committed_estimate)}</td><td className="num">{formatNumber(p.completed_estimate)}</td></tr>
            ))}</tbody>
          </table>
        )}
      </>
    );
  } else if (reportKey === "cfd") body = <FlowChart report={data} />;
  else if (reportKey === "control") {
    body = (
      <>
        <div className="report-kpis">
          <div className="report-kpi"><span className="report-kpi-num">{data.count}</span><span>Issues</span></div>
          <div className="report-kpi"><span className="report-kpi-num">{data.average_days}</span><span>Average (days)</span></div>
          <div className="report-kpi"><span className="report-kpi-num">{data.median_days}</span><span>Median (days)</span></div>
          <div className="report-kpi"><span className="report-kpi-num">{data.min_days}–{data.max_days}</span><span>Min–max (days)</span></div>
        </div>
        <p className="muted small">Cycle time = time spent in {data.work_columns.join(", ") || "the working columns"}.</p>
        <CycleChart report={data} />
      </>
    );
  } else if (reportKey === "epic") body = <EpicReport report={data} onOpen={openBugDetail} />;
  else if (reportKey === "daily") body = <DailyReport report={data} />;
  else if (reportKey === "workload") {
    body = data.entries.length === 0 && !data.unassigned_count ? <p className="muted ag-empty">Nobody has work in this sprint yet.</p> : (
      <table className="reports-table ag-table">
        <thead><tr><th>Person</th><th className="num">Issues</th><th className="num">Assigned</th><th className="num">Capacity</th><th className="num">Load</th></tr></thead>
        <tbody>
          {data.entries.map((e) => (
            <tr key={e.user_id} className={e.utilization_pct > 100 ? "ag-over" : ""}>
              <td>{e.user_name}</td><td className="num">{e.assigned_count}</td><td className="num">{formatNumber(e.assigned_estimate)}</td>
              <td className="num">{e.capacity_value != null ? formatNumber(e.capacity_value) : "–"}</td>
              <td className="num">{e.utilization_pct != null ? `${e.utilization_pct}%` : "–"}</td>
            </tr>
          ))}
          {data.unassigned_count > 0 && (
            <tr><td><em>Unassigned</em></td><td className="num">{data.unassigned_count}</td><td className="num">{formatNumber(data.unassigned_estimate)}</td><td /><td /></tr>
          )}
        </tbody>
      </table>
    );
  }

  return (
    <div className="ag-reports">
      <nav className="ag-report-nav" aria-label="Reports">
        {REPORTS.map((r) => (
          <button key={r.key} type="button" className={`ag-report-link${r.key === reportKey ? " active" : ""}`}
            aria-current={r.key === reportKey ? "page" : undefined} onClick={() => setReportKey(r.key)}>
            <span className="ag-report-name">{r.label}</span>
            <span className="muted small">{r.help}</span>
          </button>
        ))}
      </nav>
      <section className="ag-report-body" aria-labelledby="ag-report-title">
        <header className="ag-report-head">
          <h3 id="ag-report-title">{meta.label}</h3>
          {meta.scope === "sprint" && reportable.length > 0 && (
            <BhSelect ariaLabel="Sprint" value={sprintId ? String(sprintId) : ""} onChange={(v) => setSprintId(Number(v))}
              options={reportable.map((s) => ({ value: String(s.id), label: `${s.name}${s.state === "active" ? " (active)" : ""}` }))} />
          )}
          {meta.scope === "epic" && (epics.data || []).length > 0 && (
            <BhSelect ariaLabel="Epic" value={epicId ? String(epicId) : ""} onChange={(v) => setEpicId(Number(v))}
              options={(epics.data || []).map((e) => ({ value: String(e.id), label: `${e.display_id} ${e.title}` }))} />
          )}
          {meta.scope === "range" && (
            <>
              <label className="muted small" htmlFor="ag-range-from">From</label>
              <BhDateInput id="ag-range-from" value={range.from} onChange={(v) => setRange((r) => ({ ...r, from: v }))} />
              <label className="muted small" htmlFor="ag-range-to">To</label>
              <BhDateInput id="ag-range-to" value={range.to} onChange={(v) => setRange((r) => ({ ...r, to: v }))} />
            </>
          )}
          {reportKey === "daily" && (
            <>
              <label className="muted small" htmlFor="ag-daily-date">Day</label>
              <BhDateInput id="ag-daily-date" value={day} onChange={setDay} />
            </>
          )}
          <span className="ag-spacer" />
          {exportKey && data && (
            <button type="button" className="btn ghost btn-sm" onClick={() => download(exportPath(), `${exportKey}.csv`, "POST")}>⬇ CSV</button>
          )}
          {reportKey === "sprint" && sprintId && (
            <button type="button" className="btn ghost btn-sm"
              onClick={() => download(`/agile/reports/sprint/${sprintId}/export.xlsx`, `sprint-report-${sprintId}.xlsx`)}>⬇ Excel</button>
          )}
        </header>
        {meta.scope === "sprint" && sprint && (
          <p className="muted small">{sprint.name} · {formatDateRange(sprint.start_date, sprint.end_date)} · {sprint.state}</p>
        )}
        {body}
      </section>
    </div>
  );
}
