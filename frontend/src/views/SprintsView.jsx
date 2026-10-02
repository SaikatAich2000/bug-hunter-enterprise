/** Sprints: the Jira Software (Scrum) workspace of a project. */
import { useEffect, useState } from "react";
import { useApp } from "../state/AppContext";
import BhSelect from "../components/BhSelect";
import { toast, toastError } from "../lib/toast";
import BacklogPanel from "./agile/BacklogPanel";
import BoardPanel from "./agile/BoardPanel";
import HierarchyPanel from "./agile/HierarchyPanel";
import PlanningPanel from "./agile/PlanningPanel";
import ReportsPanel from "./agile/ReportsPanel";
import TaxonomyPanel from "./agile/TaxonomyPanel";
import BoardSettingsPanel from "./agile/BoardSettingsPanel";
import { agileApi, notifyAgileChanged } from "./agile/agileApi";
import { useAgileProject } from "./agile/useAgileProject";
import "../styles/agile.css";

const SPRINT_TABS = [
  { key: "backlog", icon: "📋", label: "Backlog" },
  { key: "board", icon: "📌", label: "Active sprint" },
  { key: "hierarchy", icon: "🌳", label: "Epics" },
  { key: "reports", icon: "📈", label: "Reports" },
  { key: "taxonomy", icon: "🏷", label: "Releases & labels" },
  { key: "planning", icon: "🧮", label: "Capacity" },
  { key: "settings", icon: "⚙", label: "Board settings" },
];

function EnableAgile({ projectName, canManage, onEnabled }) {
  const [busy, setBusy] = useState(false);
  const enable = async () => {
    setBusy(true);
    try {
      await onEnabled();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="empty-state ag-enable">
      <h2>Scrum isn&apos;t set up for {projectName} yet</h2>
      <p className="muted">
        Turning it on creates the project&apos;s board (To Do, In Progress, Testing, Done) and a
        backlog ranked from the project&apos;s open issues. Nothing is deleted or changed.
      </p>
      {canManage ? (
        <button type="button" className="btn primary" disabled={busy} onClick={enable}>
          {busy ? "Setting up…" : "Set up Scrum board"}
        </button>
      ) : (
        <p className="muted small">Ask an admin or manager to set it up.</p>
      )}
    </div>
  );
}

export default function SprintsView() {
  const { projects, currentUser, sprintsDeepLinkTab, setSprintsDeepLinkTab } = useApp();
  const canManage = currentUser.role === "admin" || currentUser.role === "manager";
  const [projectId, setProjectId] = useState(null);
  const [tab, setTab] = useState(() => sprintsDeepLinkTab || "board");
  const agile = useAgileProject(projectId);

  useEffect(() => {
    if (sprintsDeepLinkTab) {
      setTab(sprintsDeepLinkTab);
      setSprintsDeepLinkTab(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!projects.length) return;
    if (!projectId || !projects.some((p) => p.id === projectId)) setProjectId(projects[0].id);
  }, [projects, projectId]);

  if (!projects.length) {
    return (
      <section className="view" id="viewSprints">
        <div className="empty-state">Create a project first to use Sprints.</div>
      </section>
    );
  }

  const project = projects.find((p) => p.id === projectId);
  const enabled = Boolean(agile.settings?.agile_enabled && agile.board);

  const enable = async () => {
    try {
      await agileApi.enable(projectId);
      toast("Scrum board created", "success");
      notifyAgileChanged();
    } catch (err) {
      toastError(err);
    }
  };

  const panelProps = { projectId, canManage, board: agile.board, onOpenTab: setTab };

  return (
    <section className="view" id="viewSprints">
      <div className="sprints-toolbar">
        <div className="sprints-controls">
          <label htmlFor="sprints-project-select">Project:</label>
          <BhSelect
            id="sprints-project-select"
            value={projectId ? String(projectId) : ""}
            onChange={(v) => setProjectId(Number(v))}
            options={projects.map((p) => ({ value: String(p.id), label: p.name }))}
          />
        </div>
        {enabled && (
          <div className="agile-tabs" role="tablist" aria-label="Sprint views">
            {SPRINT_TABS.filter((t) => t.key !== "settings" || canManage).map((t) => (
              <button
                key={t.key}
                type="button"
                role="tab"
                id={`sprints-tab-${t.key}`}
                aria-selected={tab === t.key}
                className={`agile-tab${tab === t.key ? " active" : ""}`}
                onClick={() => setTab(t.key)}
              >
                <span className="tab-icon" aria-hidden="true">{t.icon}</span> {t.label}
              </button>
            ))}
          </div>
        )}
      </div>

      {agile.loading && !agile.settings && <div className="sprints-loading">Loading…</div>}
      {!agile.loading && agile.settings && !enabled && (
        <EnableAgile projectName={project?.name ?? "this project"} canManage={canManage} onEnabled={enable} />
      )}
      {enabled && (
        <div role="tabpanel" aria-labelledby={`sprints-tab-${tab}`}>
          {tab === "backlog" && <BacklogPanel {...panelProps} />}
          {tab === "board" && <BoardPanel {...panelProps} />}
          {tab === "hierarchy" && <HierarchyPanel {...panelProps} />}
          {tab === "reports" && <ReportsPanel {...panelProps} />}
          {tab === "taxonomy" && <TaxonomyPanel projectId={projectId} canManage={canManage} />}
          {tab === "planning" && <PlanningPanel projectId={projectId} canManage={canManage} />}
          {tab === "settings" && canManage && <BoardSettingsPanel {...panelProps} onSaved={agile.refresh} />}
        </div>
      )}
    </section>
  );
}
