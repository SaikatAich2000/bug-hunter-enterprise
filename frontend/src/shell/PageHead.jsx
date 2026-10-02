/** Header strip: view title, contextual subtitle, and the New Item button. */
import { useApp } from "../state/AppContext";
import { apiBlob } from "../lib/api";
import { getAppName } from "../lib/branding";
import { hideLoader, showLoader } from "../lib/loader";
import { toastError } from "../lib/toast";
const VIEW_TITLES = {
  list: "All Work Items",
  sprints: "Sprints",
  events: "Events",
  analytics: "Analytics",
  audit: "Audit Trail",
  sessions: "Active Sessions",
  reports: "Reports",
  organization: "Organization",
};

// "list" builds its own live summary below.
const VIEW_SUBTITLES = {
  sprints: "Backlog, active sprint board, epics and reports for a project, the Jira Scrum way",
  events:
    "Group work items into an event — a standup, a sprint meeting, a release — and track them together",
  analytics: "Visual breakdown of your work by status, priority, environment, project and assignee",
  audit:
    "Every create, update, delete, login and session event across __APP_NAME__ — for incident review and compliance",
  sessions:
    "Every device currently signed in. Revoke a session to log that device out without touching anything else",
  reports:
    "Build a report from any combination of filters, preview it inline, then download the full data as Excel",
  organization:
    "Your organization’s details and branding, the people you invite, and the webhooks that receive its events",
};

export default function PageHead() {
  const appName = getAppName();
  const {
    view,
    activeTab,
    defaultNewType,
    openBugForm,
    projects,
    stats,
    canManage,
    setBulkImportOpen,
  } = useApp();

  const downloadTemplate = async ()=> {
    showLoader("Preparing template…");
    try {
      const { blob, filename } = await apiBlob("/bugs/import/template.xlsx");
      const fname = filename || "bug-hunter-bulk-import-template.xlsx";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fname;
      document.body.append(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      toastError(err);
    } finally {
      hideLoader();
    }
  };

  const showListChrome = view === "list";

  // List view gets a live portfolio summary; all other views use the static subtitle.
  let sub = (VIEW_SUBTITLES[view] ?? null)?.replace("__APP_NAME__", appName);
  if (view === "list") {
    const open = stats?.open ?? 0;
    const n = projects.length;
    sub = (
      <>
        Across {n} {n === 1 ? "project" : "projects"} · <b>{open} open</b>
      </>
    );
  }

  return (
    <div className="pagehead">
      {/* key={view} re-mounts on navigation to replay the entrance animation */}
      <div className="pagehead-titles" key={view}>
        <h1 id="pageTitle">{VIEW_TITLES[view]}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      <div className="actions">
        {showListChrome && canManage && (
          <>
            <button
              className="btn ghost"
              id="downloadTemplateBtn"
              type="button"
              onClick={() => { void downloadTemplate(); }}
            >
              Download Template
            </button>
            <button
              className="btn ghost"
              id="bulkUploadBtn"
              type="button"
              onClick={() => setBulkImportOpen(true)}
            >
              Bulk Upload
            </button>
          </>
        )}
        {showListChrome && (
          <div className="new-item-wrap new-item-single">
            <button
              className="btn primary"
              id="newBugBtn"
              type="button"
              onClick={() => openBugForm({ defaultType: activeTab !== "all" ? activeTab : defaultNewType })}
            >
              + New Item
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
